"""Two real accounts with real cookies: nothing one user owns may be reached by the other, by any route."""
from datetime import datetime, timedelta
from io import BytesIO
import json
import time
import wave

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

import app as backend

LOCAL = {"origin": "http://127.0.0.1:8765", "host": "127.0.0.1:8765"}


@pytest.fixture(autouse=True)
def real_sessions(monkeypatch):
    monkeypatch.setattr(backend, "session_user", backend.default_session_user)


class Account:
    def __init__(self, email):
        self.client = TestClient(backend.app)
        self.client.__enter__()
        response = self.client.post("/api/register", headers=LOCAL, json={"email": email, "password": "password123"})
        assert response.status_code == 200, response.text
        self.token = response.cookies["session"]
        self.user = backend.accounts.find(email)
        self.context = backend.contexts.get(self.user)

    def get(self, path):
        return self.client.get(path, headers=LOCAL)

    def post(self, path, **kwargs):
        return self.client.post(path, headers=LOCAL, **kwargs)

    def delete(self, path):
        return self.client.delete(path, headers=LOCAL)

    def socket(self):
        return self.client.websocket_connect("/ws", headers={"origin": LOCAL["origin"], "cookie": f"session={self.token}"})

    def close(self):
        self.client.__exit__(None, None, None)


@pytest.fixture
def alice():
    account = Account("alice@example.com")
    yield account
    account.close()


@pytest.fixture
def bob():
    account = Account("bob@example.com")
    yield account
    account.close()


def test_each_user_has_their_own_directory(alice, bob):
    assert alice.context.root != bob.context.root
    assert alice.context.root.name == str(alice.user["id"]) and bob.context.root.name == str(bob.user["id"])


def test_memories_cannot_be_listed_deleted_or_restored_across_users(alice, bob):
    memory_id = alice.context.memories.add("alice 的密码提示")
    assert bob.get("/api/memories").json()["memories"] == []
    assert bob.delete(f"/api/memories/{memory_id}").status_code == 404
    assert bob.post(f"/api/memories/{memory_id}/restore").status_code == 404
    assert [m["content"] for m in alice.get("/api/memories").json()["memories"]] == ["alice 的密码提示"]


def test_reminders_cannot_be_listed_or_cancelled_across_users(alice, bob):
    reminder_id = alice.context.reminders.add("alice 的约会", datetime(2099, 1, 1))
    assert bob.get("/api/reminders").json()["reminders"] == []
    assert bob.delete(f"/api/reminders/{reminder_id}").status_code == 404
    assert len(alice.get("/api/reminders").json()["reminders"]) == 1


def test_knowledge_files_cannot_be_listed_searched_or_deleted_across_users(alice, bob):
    assert alice.post("/api/knowledge", files={"file": ("机密.md", "alice 的工资单".encode())}).status_code == 200
    assert bob.get("/api/knowledge").json()["files"] == []
    assert bob.delete("/api/knowledge/机密.md").status_code == 404
    assert bob.delete("/api/knowledge/..%2F" + str(alice.user["id"]) + "%2Fknowledge%2F机密.md").status_code == 404
    assert bob.context.knowledge.search("工资单") == (0, [])
    assert alice.get("/api/knowledge").json()["files"][0]["name"] == "机密.md"


def make_wav(seconds=4):
    data = BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes((np.sin(np.arange(16000 * seconds) * .05) * 8000).astype("<i2").tobytes())
    return data.getvalue()


def test_a_voice_sample_cannot_be_used_by_another_user(alice, bob):
    profile = alice.post("/api/voices", data={"name": "alice 的声音", "prompt_text": "参考文字"},
                         files={"audio": ("a.wav", make_wav(), "audio/wav")}).json()
    assert bob.get("/api/voices").json()["profiles"] == []
    assert bob.post("/api/voice/select", json={"provider": "cosyvoice", "profile_id": profile["id"]}).status_code == 400
    assert alice.post("/api/voice/select", json={"provider": "cosyvoice", "profile_id": profile["id"]}).status_code == 200


def test_choosing_a_voice_or_city_changes_it_for_that_user_only(alice, bob):
    alice.post("/api/voice/select", json={"provider": "windows", "voice": "Microsoft Huihui Desktop"})
    alice.post("/api/settings", json={"default_city": "苏州"})
    assert alice.get("/api/status").json()["tts_provider"] == "windows"
    assert alice.get("/api/status").json()["default_city"] == "苏州"
    assert bob.get("/api/status").json()["tts_provider"] == backend.TTS_PROVIDER
    assert bob.get("/api/status").json()["default_city"] == backend.DEFAULT_CITY


def fake_model(monkeypatch):
    """Records every prompt the model receives; always answers in plain text."""
    prompts = []

    def handler(request):
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"好的。"}}]}\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(backend.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")

    async def fake_synthesize(text):
        return b"fake-wav"
    monkeypatch.setattr(backend, "synthesize", fake_synthesize)
    return prompts


def ask(socket, text):
    socket.send_json({"type": "text", "request_id": 1, "text": text})
    while socket.receive_json()["type"] != "done":
        pass


def test_one_users_memories_and_reminders_never_reach_anothers_prompt(alice, bob, monkeypatch):
    prompts = fake_model(monkeypatch)
    alice.context.memories.add("alice 不吃香菜")
    alice.context.reminders.add("alice 的约会", datetime(2099, 1, 1))
    with bob.socket() as socket:
        ask(socket, "你好")
    with alice.socket() as socket:
        ask(socket, "你好")
    assert "alice" not in prompts[0]
    assert "alice 不吃香菜" in prompts[1] and "alice 的约会" in prompts[1]


def test_a_due_reminder_goes_only_to_its_owner(alice, bob, monkeypatch):
    monkeypatch.setattr(backend, "REMINDER_POLL_SECONDS", .02)
    alice.context.reminders.add("alice 的约会", datetime.now() - timedelta(seconds=1))
    with bob.socket() as socket:
        time.sleep(.3)
        socket.send_json({"type": "playing", "text": "", "recent": ""})
    assert len(alice.context.reminders.pending()) == 1
    with alice.socket() as socket:
        while socket.receive_json()["type"] != "reminder_due":
            pass


def test_a_session_cookie_from_one_user_only_ever_means_that_user(alice, bob):
    assert alice.get("/api/me").json()["email"] == "alice@example.com"
    assert bob.get("/api/me").json()["email"] == "bob@example.com"
    stranger = TestClient(backend.app)
    assert stranger.get("/api/me", headers={**LOCAL, "cookie": "session=forged-token"}).status_code == 401
