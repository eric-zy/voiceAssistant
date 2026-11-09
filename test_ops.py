import threading
import time

from fastapi.testclient import TestClient
import pytest

import app as backend
import ops

ORIGIN = {"origin": "http://127.0.0.1:8765"}


def test_a_window_allows_its_count_then_says_how_long_to_wait():
    now = [100.0]
    limiter = ops.RateLimiter(clock=lambda: now[0])
    ops.LIMITS["test"] = (2, 10)
    try:
        assert limiter.hit("test", "a") is None and limiter.hit("test", "a") is None
        assert limiter.hit("test", "a") == 10
        assert limiter.hit("test", "b") is None            # another key has its own window
        now[0] += 6
        assert limiter.hit("test", "a") == 4
        now[0] += 4.5
        assert limiter.hit("test", "a") is None            # the first hit has left the window
    finally:
        del ops.LIMITS["test"]


@pytest.mark.parametrize("raw,expected", [("5/30", (5, 30)), ("0/30", (9, 9)), ("abc", (9, 9)), ("", (9, 9)), ("5/0", (9, 9))])
def test_limits_can_be_set_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("VOICE_RATE_SAMPLE", raw)
    assert ops._limit("sample", (9, 9)) == expected


def test_metrics_keep_counts_and_recent_latencies():
    metrics = ops.Metrics(keep=100)
    for value in range(1, 201):
        metrics.observe("asr", value / 100)
    metrics.count("http_2xx", 3)
    snapshot = metrics.snapshot()
    assert snapshot["counts"] == {"http_2xx": 3}
    assert snapshot["latency"]["asr"] == {"n": 100, "p50": 1.51, "p95": 1.96, "max": 2.0}  # only the last 100


def test_the_gate_runs_at_most_its_size_and_counts_who_waits():
    gate = ops.RecognitionGate(2)
    inside, peak, release = [0], [0], threading.Event()
    lock = threading.Lock()

    def work():
        with gate:
            with lock:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            release.wait(2)
            with lock:
                inside[0] -= 1
    threads = [threading.Thread(target=work) for _ in range(5)]
    for thread in threads:
        thread.start()
    for _ in range(200):
        if gate.queue_length() == 3:
            break
        time.sleep(.01)
    assert gate.busy() and gate.queue_length() == 3 and gate.active == 2
    release.set()
    for thread in threads:
        thread.join()
    assert peak[0] == 2 and gate.active == 0 and gate.queue_length() == 0 and not gate.busy()


@pytest.mark.parametrize("raw,expected", [("4", 4), ("0", 1), ("x", 2), (None, 2)])
def test_recognition_slots_come_from_the_environment(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("VOICE_ASR_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("VOICE_ASR_CONCURRENCY", raw)
    assert ops.recognition_slots() == expected


# Wired into the app

def test_the_health_check_needs_no_sign_in_and_says_nothing_else(monkeypatch):
    monkeypatch.setattr(backend, "session_user", lambda request: None)
    with TestClient(backend.app) as client:
        assert client.get("/healthz").json() == {"ok": True}


def test_only_an_admin_sees_the_numbers(user, monkeypatch):
    with TestClient(backend.app) as client:
        client.get("/api/status")
        data = client.get("/api/admin/metrics").json()
        assert data["counts"]["http_2xx"] >= 1 and data["recognition"] == {"slots": backend.recognition_gate.size, "running": 0, "waiting": 0}
        monkeypatch.setattr(user, "is_admin", False)
        assert client.get("/api/admin/metrics").status_code == 403


def test_signing_in_too_often_from_one_address_is_slowed_down(user, monkeypatch):
    monkeypatch.setitem(ops.LIMITS, "auth", (2, 60))
    with TestClient(backend.app) as client:
        attempt = lambda: client.post("/api/login", json={"email": "a@b.test", "password": "wrong-password"}, headers=ORIGIN)
        assert attempt().status_code == 401 and attempt().status_code == 401
        refused = attempt()
        assert refused.status_code == 429 and "尝试太频繁" in refused.json()["detail"] and refused.headers["retry-after"] == "60"
        assert client.get("/api/admin/metrics").json()["counts"]["limited_auth"] == 1


def test_each_account_has_its_own_lookup_allowance(user, monkeypatch):
    monkeypatch.setitem(ops.LIMITS, "lookup", (1, 60))
    monkeypatch.setattr(backend, "API_KEY", "")
    with TestClient(backend.app) as client:
        assert client.post("/api/english/translate", json={"text": "Hello"}, headers=ORIGIN).status_code == 400  # counted, then no key
        assert client.post("/api/english/translate", json={"text": "Hello"}, headers=ORIGIN).status_code == 429


def test_asking_too_fast_over_the_socket_says_how_long_to_wait(user, monkeypatch):
    monkeypatch.setitem(ops.LIMITS, "turn", (1, 60))
    monkeypatch.setattr(backend, "API_KEY", "")
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "text", "request_id": 1, "text": "你好"})
        while (message := socket.receive_json())["type"] != "error":
            pass
        assert "DEEPSEEK_API_KEY" in message["text"]   # the first question went through
        socket.send_json({"type": "text", "request_id": 2, "text": "再问一次"})
        while (message := socket.receive_json())["type"] != "error":
            pass
        assert message["request_id"] == 2 and message["text"].startswith("说得太快了")


def test_a_spoken_question_while_recognition_is_busy_is_told_it_is_queued(user, monkeypatch):
    class Full:
        size, active, waiting = 1, 1, 2

        def busy(self):
            return True

        def queue_length(self):
            return 2
    monkeypatch.setattr(backend, "recognition_gate", Full())
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "")
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "audio_start", "request_id": 1})
        socket.send_bytes(b"\0" * 16000)
        socket.send_json({"type": "audio_end", "request_id": 1})
        while (message := socket.receive_json())["type"] != "status":
            pass
        assert message["text"] == "识别排队中，前面还有 3 句…"
