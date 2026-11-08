import json
import time

import httpx
from fastapi.testclient import TestClient
import pytest

import app as backend
import practice
from practice import parse_note, read_aloud, split_english
import usage

ORIGIN = {"origin": "http://127.0.0.1:8765"}


# Pieces

def test_an_english_sentence_is_spoken_once_the_next_one_starts():
    assert split_english("Hello there.") == (None, "Hello there.")
    assert split_english("Hello there. What") == ("Hello there. ", "What")
    assert split_english("It costs 3.5 dollars") == (None, "It costs 3.5 dollars")
    assert split_english("Sure! Anything else?") == ("Sure! ", "Anything else?")
    assert split_english("a" * 170)[0] == "a" * 160


def test_the_partner_follows_level_and_topic():
    beginner = practice.partner_prompt("beginner")
    assert "A2" in beginner and "suggest an everyday topic" in beginner
    with_topic = practice.partner_prompt("advanced", "my trip to Japan")
    assert "C1" in with_topic and "my trip to Japan" in with_topic and "Chinese" in with_topic


def test_a_correction_is_kept_only_when_it_says_how_to_fix_it():
    good = '好的 {"ok": false, "type": "语法", "original": "I want", "corrected": "I\'d like", "better": "", "explain": "更礼貌"}'
    assert parse_note(good, "I want a salad") == {"heard": "I want a salad", "category": "语法", "original": "I want",
                                                  "corrected": "I'd like", "better": "", "explain": "更礼貌"}
    assert parse_note('{"ok": false, "type": "怪", "better": "Could I"}', "x")["category"] == "表达"
    for text in ['{"ok": true}', '{"ok": false, "explain": "只有说明"}', "不是 JSON", '{"ok": false', "[1]"]:
        assert parse_note(text, "x") is None


def test_read_aloud_marks_each_word_in_order():
    result = read_aloud("Could I see the menu, please?", "could i see menu please")
    assert [(item["word"], item["ok"]) for item in result["words"]] == [
        ("could", True), ("i", True), ("see", True), ("the", False), ("menu", True), ("please", True)]
    assert result["score"] == .83
    assert read_aloud("I'd like the salad.", "I’d like the salad uh")["score"] == 1
    assert read_aloud("I'd like the salad.", "salad the like I'd")["score"] == .25
    assert read_aloud("Hello", "")["score"] == 0


# The learning store

def test_the_same_mistake_twice_is_one_item_seen_twice_and_due_again(user):
    store = user.english
    first = store.add_item("mistake", "I want a salad.", "I'd like a salad.", note="更礼貌", session_id=1)
    store.set_mastered(first)
    again = store.add_item("mistake", "i want a  salad", "I'd like a salad, please.", session_id=2)
    assert again == first
    [item] = store.items("mistake")
    assert item["seen_count"] == 2 and item["meaning"] == "I'd like a salad, please." and item["mastered"] == 0
    assert item["box"] == 0 and item["due_at"] and item["session_id"] == 2


def test_items_can_be_counted_mastered_and_deleted(user):
    store = user.english
    a = store.add_item("mistake", "He go to school", "He goes to school")
    store.add_item("word", "budget", "预算")
    assert store.counts() == {"mistake": 1, "word": 1}
    assert store.set_mastered(a) and store.counts() == {"word": 1}
    assert [item["text"] for item in store.items(include_mastered=True)] == ["budget", "He go to school"]
    assert store.delete_item(a) and not store.delete_item(a) and not store.set_mastered(999)
    with pytest.raises(ValueError):
        store.add_item("idiom", "x")
    with pytest.raises(ValueError):
        store.add_item("word", " ?! ")


def test_sessions_are_listed_once_finished(user):
    store = user.english
    first, second = store.start("beginner"), store.start("advanced", "movies")
    assert store.sessions() == []
    store.finish(first, [{"who": "learner", "text": "Hi"}], [])
    store.set_report(first, "总结")
    assert [item["id"] for item in store.sessions()] == [first]
    assert store.session(second)["topic"] == "movies" and store.session(first)["report"] == "总结"


# The whole conversation over the socket, with a scripted model

def sse(text):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False) + \
           '\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":50,"completion_tokens":10,' \
           '"prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":50}}\n\ndata: [DONE]\n\n'


NOTE = '{"ok": false, "type": "语法", "original": "I go", "corrected": "I went", "better": "I went to the cinema yesterday.", "explain": "说过去的事用过去式"}'


class Recorded(list):
    spoken = None


@pytest.fixture
def model(monkeypatch):
    """Answers as whichever agent the system prompt belongs to, and records every request."""
    requests = Recorded()

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        system = body["messages"][0]["content"]
        if system.startswith("You are a friendly English conversation partner"):
            text = "Hi! What did you do this weekend?" if len(body["messages"]) == 2 else "Nice! What did you watch?"
        elif system.startswith(practice.CORRECTOR.instructions[:12]):
            text = NOTE
        elif system.startswith(practice.REPORTER.instructions[:12]):
            text = "说得好：敢开口。问题：时态。"
        else:
            text = "你好。"
        return httpx.Response(200, text=sse(text))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(backend, "LLM_MODEL", "deepseek-flash")
    spoken = []

    async def synthesize(text, choice=None):
        spoken.append((text, choice.voice if choice else None))
        return b"ID3fake"
    monkeypatch.setattr(backend, "synthesize", synthesize)
    requests.spoken = spoken
    return requests


def receive_until(socket, kind):
    seen = []
    while True:
        message = socket.receive_json()
        seen.append(message)
        if message["type"] == kind:
            return message, seen
        assert message["type"] != "error", message  # fail at once rather than wait forever


def wait_for_report(user):
    for _ in range(300):
        tasks = [task for task in user.tasks.recent() if task["kind"] == "practice_report"]
        if tasks and tasks[0]["status"] not in ("queued", "running"):
            return tasks[0]
        time.sleep(.01)
    raise AssertionError(user.tasks.recent())


def test_a_free_conversation_from_greeting_to_report(user, model):
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "practice_start", "request_id": 1, "level": "beginner", "topic": "  my  weekend "})
        started, _ = receive_until(socket, "practice_started")
        assert started["level"] == "初级" and started["topic"] == "my weekend"
        receive_until(socket, "done")
        opening = model[0]
        assert "A2" in opening["messages"][0]["content"] and "my weekend" in opening["messages"][0]["content"]
        assert "tools" not in opening
        assert model.spoken == [("Hi! ", practice.ENGLISH_VOICE), ("What did you do this weekend?", practice.ENGLISH_VOICE)]

        socket.send_json({"type": "text", "request_id": 2, "text": "I go to cinema yesterday"})
        _, seen = receive_until(socket, "done")
        note = next((m for m in seen if m["type"] == "practice_note"), None) or receive_until(socket, "practice_note")[0]
        assert note["request_id"] == 2  # the page shows it under this turn's sentence
        assert note["category"] == "语法" and note["corrected"] == "I went" and note["heard"] == "I go to cinema yesterday"
        partner = [r for r in model if r["messages"][0]["content"].startswith("You are a friendly")][-1]
        assert [m["role"] for m in partner["messages"]] == ["system", "user", "assistant", "user"]

        socket.send_json({"type": "practice_stop"})
        stopped, _ = receive_until(socket, "practice_stopped")
        assert stopped["task_id"] and stopped["notes"] == 1
        report = wait_for_report(user)
        assert report["status"] == "done" and report["result"]["text"].startswith("说得好") and report["result"]["mistakes"] == 1

        socket.send_json({"type": "text", "request_id": 3, "text": "你好"})
        receive_until(socket, "done")
        normal = model[-1]
        assert not normal["messages"][0]["content"].startswith("You are a friendly")
        assert all("cinema" not in (m.get("content") or "") for m in normal["messages"])

    [mistake] = user.english.items("mistake")
    assert mistake["text"] == "I go to cinema yesterday" and mistake["meaning"] == "I went" and mistake["note"] == "说过去的事用过去式"
    assert mistake["context"] == "I go"
    session = user.english.sessions()[0]
    assert [turn["who"] for turn in session["transcript"]] == ["tutor", "learner", "tutor"] and session["topic"] == "my weekend"
    assert user.settings()["english_level"] == "beginner"
    billed = {row["agent"] for row in usage.LEDGER.summary(user.user_id)["agents"]}
    assert {"tutor", "corrector", "practice_report", "main"} <= billed


def test_spoken_turns_are_heard_as_english(user, model, monkeypatch):
    languages = []

    def transcribe(pcm, language="zh"):
        languages.append(language)
        return "Hello"
    monkeypatch.setattr(backend, "transcribe", transcribe)
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "practice_start", "request_id": 1, "level": "intermediate"})
        receive_until(socket, "done")
        socket.send_json({"type": "audio_start", "request_id": 2})
        socket.send_bytes(b"\0" * 16000)
        socket.send_json({"type": "audio_end", "request_id": 2})
        receive_until(socket, "done")
    assert languages == ["en"]


@pytest.mark.parametrize("start,message", [
    ({"level": "expert"}, "请选择英语水平"), ({"level": "beginner", "topic": "x" * 101}, "话题最多 100 字")])
def test_a_bad_start_is_refused(user, model, start, message):
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "practice_start", "request_id": 1, **start})
        assert receive_until(socket, "error")[0]["text"] == message
    assert user.english.session(1) is None


def test_an_empty_conversation_gets_no_report(user, model):
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "practice_start", "request_id": 1, "level": "advanced"})
        receive_until(socket, "done")
        socket.send_json({"type": "practice_stop"})
        stopped, _ = receive_until(socket, "practice_stopped")
    assert stopped["task_id"] is None and "没有对话" in stopped["error"]


def test_leaving_the_page_mid_conversation_still_saves_it(user, model):
    with TestClient(backend.app) as client:
        with client.websocket_connect("/ws", headers=ORIGIN) as socket:
            socket.send_json({"type": "practice_start", "request_id": 1, "level": "intermediate"})
            receive_until(socket, "done")
            socket.send_json({"type": "text", "request_id": 2, "text": "Hi I am Li"})
            receive_until(socket, "done")
        for _ in range(300):
            if user.english.sessions():
                break
            time.sleep(.01)
    assert user.english.sessions()[0]["transcript"][1] == {"who": "learner", "text": "Hi I am Li"}


# The page's endpoints

def test_the_overview_has_levels_the_last_level_sessions_and_counts(user):
    user.save_settings({"english_level": "advanced"})
    session = user.english.start("beginner", "food")
    user.english.finish(session, [{"who": "learner", "text": "Hello"}, {"who": "tutor", "text": "Hi"}], [{"category": "语法"}])
    user.english.add_item("mistake", "He go", "He goes")
    with TestClient(backend.app) as client:
        data = client.get("/api/practice").json()
    assert [item["id"] for item in data["levels"]] == ["beginner", "intermediate", "advanced"] and data["level"] == "advanced"
    assert data["sessions"] == [{"id": session, "level": "初级", "topic": "food", "started_at": data["sessions"][0]["started_at"],
                                 "turns": 1, "notes": 1, "has_report": False, "ended": True}]
    assert data["counts"] == {"mistake": 1}


def test_the_mistake_book_lists_masters_and_deletes(user):
    keep = user.english.add_item("mistake", "He go", "He goes")
    drop = user.english.add_item("mistake", "She have", "She has")
    user.english.add_item("word", "budget", "预算")
    with TestClient(backend.app) as client:
        assert [item["text"] for item in client.get("/api/english/items?kind=mistake").json()["items"]] == ["She have", "He go"]
        assert client.get("/api/english/items?kind=idiom").status_code == 400
        assert client.post(f"/api/english/items/{keep}/mastered", headers=ORIGIN).json() == {"ok": True}
        assert client.delete(f"/api/english/items/{drop}", headers=ORIGIN).json() == {"ok": True}
        assert client.get("/api/english/items?kind=mistake").json()["items"] == []
        assert len(client.get("/api/english/items?kind=mistake&mastered=true").json()["items"]) == 1
        assert client.post(f"/api/english/items/{keep}/mastered?undo=true", headers=ORIGIN).json() == {"ok": True}
        assert client.delete("/api/english/items/999", headers=ORIGIN).status_code == 404
        assert client.delete(f"/api/english/items/{keep}").status_code == 403


@pytest.fixture
def client_with_asr(user, monkeypatch):
    languages = []

    def transcribe(pcm, language="zh"):
        languages.append(language)
        return "could I see the menu please"
    monkeypatch.setattr(backend, "transcribe", transcribe)
    with TestClient(backend.app) as client:
        yield client, languages


def test_read_aloud_runs_locally_on_english(client_with_asr):
    client, languages = client_with_asr
    response = client.post("/api/practice/read", headers=ORIGIN, data={"target": "Could I see the menu, please?"},
                           files={"audio": ("read.pcm", b"\0" * 32000)})
    assert response.json()["score"] == 1 and languages == ["en"]


@pytest.mark.parametrize("target,audio,message", [
    ("你好", b"\0" * 32000, "英文"), ("Hello", b"\0" * 1000, "0.3～15 秒"), ("Hello", b"\0" * 32001, "0.3～15 秒")])
def test_bad_read_aloud_requests_are_refused(client_with_asr, target, audio, message):
    client, _ = client_with_asr
    response = client.post("/api/practice/read", headers=ORIGIN, data={"target": target}, files={"audio": ("r.pcm", audio)})
    assert response.status_code == 400 and message in response.json()["detail"]


def test_a_report_can_be_asked_for_again_only_for_a_finished_conversation(user, model):
    unfinished = user.english.start("beginner")
    finished = user.english.start("beginner")
    user.english.finish(finished, [{"who": "learner", "text": "Hi"}], [])
    with TestClient(backend.app) as client:
        assert client.post(f"/api/practice/{unfinished}/report", headers=ORIGIN).status_code == 404
        assert client.post("/api/practice/999/report", headers=ORIGIN).status_code == 404
        assert client.post(f"/api/practice/{finished}/report", headers=ORIGIN).status_code == 200
        assert wait_for_report(user)["status"] == "done"


# Saving as it goes, taking a sentence back, and starting again from an earlier conversation

def test_history_is_rebuilt_from_turns_and_only_learner_turns_can_be_redone():
    turns = [{"who": "tutor", "text": "Hi"}, {"who": "learner", "text": "Hello"}]
    assert practice.history_for(turns) == [{"role": "user", "content": practice.OPENING}, {"role": "assistant", "content": "Hi"},
                                           {"role": "user", "content": "Hello"}]
    assert practice.is_learner_turn(turns, 1)
    for bad in (0, 2, -1, True, "1", None):
        assert not practice.is_learner_turn(turns, bad)


def start_talk(socket, level="intermediate"):
    socket.send_json({"type": "practice_start", "request_id": 1, "level": level})
    started, _ = receive_until(socket, "practice_started")
    receive_until(socket, "done")
    return started["session_id"]


def say(socket, rid, text):
    socket.send_json({"type": "text", "request_id": rid, "text": text})
    return receive_until(socket, "done")[1]


def test_every_turn_is_saved_while_talking_and_the_page_learns_each_turn_number(user, model):
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        session_id = start_talk(socket)
        seen = say(socket, 2, "I go to cinema yesterday")
        turns = [(m["request_id"], m["turn"], m["who"]) for m in seen if m["type"] == "practice_turn"]
        assert turns == [(2, 1, "learner"), (2, 2, "tutor")]
        live = client.get(f"/api/practice/sessions/{session_id}").json()
        assert [turn["who"] for turn in live["transcript"]] == ["tutor", "learner", "tutor"] and live["ended_at"] is None
        assert client.get("/api/practice").json()["sessions"][0]["ended"] is False
        for _ in range(300):
            if client.get(f"/api/practice/sessions/{session_id}").json()["notes"]:
                break
            time.sleep(.01)
        assert client.get(f"/api/practice/sessions/{session_id}").json()["notes"][0]["turn"] == 1
    assert client.get("/api/practice/sessions/999").status_code == 404


def test_taking_back_a_sentence_drops_it_and_everything_after(user, model):
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        session_id = start_talk(socket)
        say(socket, 2, "I go to cinema yesterday")
        say(socket, 3, "It was good")
        socket.send_json({"type": "practice_rewind", "request_id": 4, "turn": 1})
        rewound, _ = receive_until(socket, "practice_rewound")
        assert rewound == {"type": "practice_rewound", "request_id": 4, "turn": 1, "text": "I go to cinema yesterday"}
        saved = client.get(f"/api/practice/sessions/{session_id}").json()
        assert [turn["who"] for turn in saved["transcript"]] == ["tutor"] and saved["notes"] == []
        say(socket, 5, "I went to the cinema yesterday")
        partner = [r for r in model if r["messages"][0]["content"].startswith("You are a friendly")][-1]
        said = [m["content"] for m in partner["messages"][1:]]
        assert said[-1] == "I went to the cinema yesterday" and not any("I go to" in text or "good" in text for text in said)
        for bad in (0, 9, True):
            socket.send_json({"type": "practice_rewind", "request_id": 6, "turn": bad})
            assert receive_until(socket, "error")[0]["text"] == "找不到要重说的那一句"


def test_a_correction_that_arrives_after_its_sentence_was_taken_back_is_dropped(user, model, monkeypatch):
    import asyncio
    from httpx._client import AsyncClient as original  # the real class; the model fixture already replaced httpx.AsyncClient

    async def handler(request):
        body = json.loads(request.content)
        model.append(body)
        system = body["messages"][0]["content"]
        if system.startswith(practice.CORRECTOR.instructions[:12]):
            await asyncio.sleep(.3)
            return httpx.Response(200, text=sse(NOTE))
        return httpx.Response(200, text=sse("Hi! What did you do?" if len(body["messages"]) == 2 else "Nice."))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        session_id = start_talk(socket)
        say(socket, 2, "I go to cinema yesterday")
        socket.send_json({"type": "practice_rewind", "request_id": 3, "turn": 1})
        receive_until(socket, "practice_rewound")
        time.sleep(.5)
        assert client.get(f"/api/practice/sessions/{session_id}").json()["notes"] == []


def test_an_earlier_conversation_can_start_again_from_one_of_its_sentences(user, model):
    earlier = user.english.start("beginner", "food")
    user.english.finish(earlier, [{"who": "tutor", "text": "Hi! What do you like to eat?"}, {"who": "learner", "text": "I like noodle"},
                                  {"who": "tutor", "text": "Nice!"}, {"who": "learner", "text": "I eat it every days"}],
                        [{"turn": 1, "category": "语法"}, {"turn": 3, "category": "语法"}])
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "practice_resume", "request_id": 1, "session_id": earlier, "turn": 3})
        started, _ = receive_until(socket, "practice_started")
        assert started["redo"] == "I eat it every days" and started["level"] == "初级" and started["topic"] == "food"
        assert [turn["text"] for turn in started["turns"]] == ["Hi! What do you like to eat?", "I like noodle", "Nice!"]
        assert started["notes"] == [{"turn": 1, "category": "语法"}]
        assert started["session_id"] != earlier
        say(socket, 2, "I eat them every day")
        partner = [r for r in model if r["messages"][0]["content"].startswith("You are a friendly")][-1]
        assert [m["content"] for m in partner["messages"][2:]] == ["Hi! What do you like to eat?", "I like noodle", "Nice!", "I eat them every day"]
        for bad in ({"session_id": earlier, "turn": 0}, {"session_id": 999, "turn": 1}, {"session_id": "1", "turn": 1}):
            socket.send_json({"type": "practice_resume", "request_id": 3, **bad})
            assert receive_until(socket, "error")[0]["text"] == "找不到要重说的那一句"
    assert len(user.english.session(earlier)["transcript"]) == 4  # the earlier one is left as it was


# Recordings, replay and the review of each spoken sentence

import io
import wave

import numpy as np

import pronunciation


def tone_wav(seconds=1.0, rate=24000):
    samples = (np.sin(np.arange(int(rate * seconds)) / rate * 2 * np.pi * 300) * 8000).astype("<i2")
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())
    return data.getvalue()


def test_the_fluency_reference_counts_speaking_time_rate_and_pauses():
    from turn_detector import FRAME_BYTES, FRAME_SECONDS
    pattern = [0] * 5 + [1] * 30 + [0] * 20 + [1] * 30 + [0] * 5   # speech, a 0.64 s pause, speech
    pcm = b"".join(bytes([value]) * FRAME_BYTES for value in pattern)
    stats = practice.speech_stats(pcm, "I went to the cinema with my friends", lambda frame: .9 if frame[0] else .1)
    assert stats["pauses"] == 1 and stats["longest_pause"] == round(20 * FRAME_SECONDS, 1)
    assert stats["speaking"] == round(80 * FRAME_SECONDS, 1) and stats["wpm"] == round(8 / (80 * FRAME_SECONDS) * 60)
    assert practice.speech_stats(b"\0" * FRAME_BYTES * 10, "x", lambda frame: .1) is None


def test_the_content_judgement_needs_a_score_from_one_to_five():
    assert practice.parse_logic('{"ok": true, "logic": {"score": 4, "comment": "回答切题"}}') == {"score": 4, "comment": "回答切题"}
    for text in ['{"ok": true}', '{"logic": {"score": 9}}', '{"logic": {"score": "很好"}}', "不是 JSON"]:
        assert practice.parse_logic(text) is None


def test_without_a_provider_pronunciation_is_not_scored(monkeypatch):
    monkeypatch.delenv("VOICE_PRON_PROVIDER", raising=False)
    assert pronunciation.assess(b"\0" * 100, "hello") is None and "未配置" in pronunciation.status()
    monkeypatch.setenv("VOICE_PRON_PROVIDER", "nobody")
    assert pronunciation.assess(b"\0" * 100, "hello") is None and "不支持" in pronunciation.status()


def test_a_provider_scores_are_rounded_and_its_errors_become_a_reason(monkeypatch):
    monkeypatch.setenv("VOICE_PRON_PROVIDER", "fake")
    monkeypatch.setitem(pronunciation.PROVIDERS, "fake", lambda pcm, text: {
        "overall": 81.6, "accuracy": 78.2, "fluency": 90, "completeness": 100,
        "words": [{"word": "cinema", "score": 42.4}, {"word": "went", "score": 95}, {"word": "Cinema.", "score": 50}]})
    scores = pronunciation.assess(b"\0" * 100, "I went to the cinema")
    assert scores["overall"] == 82 and scores["words"][0] == {"word": "cinema", "score": 42}
    assert [item["word"] for item in pronunciation.weak_words(scores)] == ["cinema"]

    def broken(pcm, text):
        raise pronunciation.ProviderError("Key 无效")
    monkeypatch.setitem(pronunciation.PROVIDERS, "fake", broken)
    assert pronunciation.assess(b"\0" * 100, "hi") == {"error": "Key 无效"}


@pytest.fixture
def spoken(user, model, monkeypatch):
    """The learner speaks (a fake recognizer), the partner's speech is a real WAV, and Silero is replaced by
    "loud is speech", so recordings, replay and the fluency reference all run for real."""
    monkeypatch.setattr(backend, "transcribe", lambda pcm, language="zh": "I went to the cinema")

    async def synthesize(text, choice=None):
        return tone_wav(.5)
    monkeypatch.setattr(backend, "synthesize", synthesize)

    class Loud:
        def __call__(self, frame):
            return .9 if any(frame) else .1
    monkeypatch.setattr(backend, "TurnDetectorChance", Loud)
    return model


def speech(seconds=1.0):
    return (np.ones(int(16000 * seconds), "<i2") * 3000).tobytes()


def test_a_spoken_sentence_is_recorded_reviewed_and_can_be_replayed(user, spoken, monkeypatch):
    monkeypatch.setenv("VOICE_PRON_PROVIDER", "fake")
    monkeypatch.setitem(pronunciation.PROVIDERS, "fake", lambda pcm, text: {"overall": 70, "accuracy": 65, "fluency": 80, "completeness": 100,
                                                                            "words": [{"word": "cinema", "score": 40}]})
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        session_id = start_talk(socket)
        socket.send_json({"type": "audio_start", "request_id": 2})
        socket.send_bytes(speech(1.0))
        socket.send_json({"type": "audio_end", "request_id": 2})
        _, seen = receive_until(socket, "done")
        turns = {(m["turn"], m["who"]): m["audio"] for m in seen if m["type"] == "practice_turn"}
        assert turns == {(1, "learner"): True, (2, "tutor"): True}
        review = next((m for m in seen if m["type"] == "practice_review"), None) or receive_until(socket, "practice_review")[0]
        assert review["turn"] == 1 and review["request_id"] == 2
        assert review["fluency"]["speaking"] > 0 and review["pron"]["overall"] == 70
        for turn in (0, 1, 2):
            played = client.get(f"/api/practice/sessions/{session_id}/audio/{turn}")
            assert played.status_code == 200 and played.content.startswith(b"OggS"), turn
        assert client.get(f"/api/practice/sessions/{session_id}/audio/9").status_code == 404
        saved = client.get(f"/api/practice/sessions/{session_id}").json()["transcript"][1]
        assert saved["audio"] == f"{session_id}-1.ogg" and saved["seconds"] == 1.0 and saved["review"]["pron"]["overall"] == 70
    assert [item["text"] for item in user.english.items("word")] == ["cinema"]


def test_a_typed_sentence_has_no_recording(user, spoken):
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        session_id = start_talk(socket)
        seen = say(socket, 2, "I go to cinema yesterday")
        learner = next(m for m in seen if m["type"] == "practice_turn" and m["who"] == "learner")
        assert learner["audio"] is False
        assert client.get(f"/api/practice/sessions/{session_id}/audio/1").status_code == 404


def test_a_sentence_heard_while_listening_keeps_its_recording(user, spoken, monkeypatch):
    class Detector:
        MAX_SECONDS = 15

        def __init__(self):
            self.max_seconds, self.audio, self.done = 15, None, False

        def feed(self, pcm):
            if self.done:
                return []
            self.done = True
            return [("end", speech(1.5))]

        def reset(self):
            pass
    monkeypatch.setattr(backend, "TurnDetector", Detector)
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        session_id = start_talk(socket)
        socket.send_json({"type": "listen_start"})
        socket.send_bytes(b"\0" * 3200)
        heard = None
        while heard is None:
            message = socket.receive_json()
            if message["type"] == "voice" and message.get("event") == "question":
                heard = message["text"]
        socket.send_json({"type": "text", "request_id": 2, "text": heard, "source": "listen"})
        _, seen = receive_until(socket, "done")
        assert next(m for m in seen if m["type"] == "practice_turn" and m["who"] == "learner")["audio"] is True
        saved = client.get(f"/api/practice/sessions/{session_id}").json()["transcript"][1]
        assert saved["seconds"] == 1.5


# Hearing the right way to say it, and why a recording gave no text

def test_the_standard_reading_uses_the_english_voice_and_is_made_once(user, monkeypatch):
    voices = []

    async def synthesize(text, choice=None):
        voices.append((text, choice.voice if choice else None))
        return tone_wav(.5)
    monkeypatch.setattr(backend, "synthesize", synthesize)
    with TestClient(backend.app) as client:
        for _ in range(2):
            response = client.post("/api/practice/say", json={"text": "I went  to the cinema.", "voice": "standard"}, headers=ORIGIN)
            assert response.status_code == 200 and response.content.startswith(b"OggS")
    assert voices == [("I went to the cinema.", practice.ENGLISH_VOICE)]  # the second time comes from the saved file


def test_my_own_voice_uses_my_clone(user, monkeypatch):
    used = []

    async def clone(profile, text):
        used.append((profile["id"], text))
        return tone_wav(.5, 22050)
    monkeypatch.setattr(backend, "synthesize_clone_when_free", clone)
    monkeypatch.setattr(backend, "own_clone", lambda context: "a" * 32)
    monkeypatch.setattr(type(user.voices), "get", lambda self, profile_id: {"id": profile_id, "prompt_text": "x"})
    with TestClient(backend.app) as client:
        response = client.post("/api/practice/say", json={"text": "I went to the cinema.", "voice": "mine"}, headers=ORIGIN)
    assert response.status_code == 200 and response.content.startswith(b"OggS")
    assert used == [("a" * 32, "I went to the cinema.")]


def test_reading_aloud_explains_what_went_wrong(user, monkeypatch):
    async def busy(profile, text):
        raise backend.CloneBusy("busy")

    async def offline(profile, text):
        raise backend.httpx.ConnectError("refused")
    monkeypatch.setattr(type(user.voices), "get", lambda self, profile_id: {"id": profile_id, "prompt_text": "x"})
    with TestClient(backend.app) as client:
        say = lambda **body: client.post("/api/practice/say", json=body, headers=ORIGIN)
        assert say(text="你好", voice="standard").status_code == 400
        assert say(text="Hello", voice="robot").status_code == 400
        assert "还没有自己的音色" in say(text="Hello", voice="mine").json()["detail"]
        monkeypatch.setattr(backend, "own_clone", lambda context: "a" * 32)
        monkeypatch.setattr(backend, "synthesize_clone_when_free", busy)
        assert say(text="Hello there", voice="mine").status_code == 429
        monkeypatch.setattr(backend, "synthesize_clone_when_free", offline)
        assert "CosyVoice" in say(text="Hello again", voice="mine").json()["detail"]
        assert client.post("/api/practice/say", json={"text": "Hello", "voice": "standard"}).status_code == 403


def test_my_own_voice_is_the_clone_in_use_or_else_the_first_uploaded(user, monkeypatch):
    monkeypatch.setattr(type(user.voices), "list", lambda self: [{"id": "b" * 32, "name": "我", "duration": 9}])
    assert backend.own_clone(user) == "b" * 32
    user.save_settings({"provider": "cosyvoice", "profile_id": "c" * 32})
    assert backend.own_clone(user) == "c" * 32
    user.save_settings({"provider": "edge", "profile_id": ""})
    monkeypatch.setattr(type(user.voices), "list", lambda self: [])
    assert backend.own_clone(user) is None


def test_a_recording_with_no_text_says_why_in_numbers(monkeypatch):
    assert "只有 0.1 秒" in backend.no_speech_message(b"\0" * 3200)
    silent = backend.no_speech_message(b"\0" * 32000)
    assert "语音概率 0." in silent and "检查选的麦克风" in silent
    noise = (np.random.default_rng(0).integers(-3000, 3000, 32000).astype("<i2")).tobytes()
    assert "不像人声" in backend.no_speech_message(noise)
    monkeypatch.setattr(backend, "measure_speech", lambda pcm: (.9, .5))
    assert "听到了人声但没识别出文字" in backend.no_speech_message(noise)


# Translations and looking things up

def test_lookups_are_sorted_into_words_phrases_and_sentences():
    assert practice.lookup_kind("cinema") == "word"
    assert practice.lookup_kind("sounds rough") == "phrase"
    assert practice.lookup_kind("Tell me what's going on now") == "sentence"
    assert practice.parse_explanation('{"phonetic": "/rʌf/", "meaning": "艰难的", "note": "", "example": ""}')["meaning"] == "艰难的"
    assert practice.parse_explanation('{"phonetic": "/rʌf/"}') is None and practice.parse_explanation("不是 JSON") is None


@pytest.fixture
def lookup_model(user, monkeypatch):
    """A model that translates and explains, counting how often it is asked."""
    asked = []

    def handler(request):
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        asked.append(system[:6])
        if system.startswith(practice.TRANSLATOR.instructions[:8]):
            text = "哦不，听起来挺难受的。"
        elif system.startswith(practice.EXPLAINER.instructions[:8]):
            text = '{"phonetic": "/rʌf/", "meaning": "（日子）难熬的", "note": "sounds rough 意思是听起来很糟", "example": "I had a rough day."}'
        else:
            text = "?"
        return httpx.Response(200, text=sse(text))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(backend, "LLM_MODEL", "deepseek-flash")
    return asked


def test_a_sentence_is_translated_once_and_then_comes_from_the_cache(user, lookup_model):
    with TestClient(backend.app) as client:
        for _ in range(2):
            response = client.post("/api/english/translate", json={"text": "Oh no,  that sounds rough."}, headers=ORIGIN)
            assert response.json() == {"text": "Oh no, that sounds rough.", "zh": "哦不，听起来挺难受的。"}
        assert client.post("/api/english/translate", json={"text": "你好"}, headers=ORIGIN).status_code == 400
        assert client.post("/api/english/translate", json={"text": "Hello"}).status_code == 403
    assert len(lookup_model) == 1
    billed = {row["agent"] for row in usage.LEDGER.summary(user.user_id)["agents"]}
    assert billed == {"translator"}


def test_a_looked_up_phrase_is_explained_in_its_sentence_and_saved_to_the_vocabulary_book(user, lookup_model):
    with TestClient(backend.app) as client:
        body = {"text": "rough", "context": "Oh no, that sounds rough. Do you mean you're feeling terrible?"}
        first = client.post("/api/english/explain", json=body, headers=ORIGIN).json()
        assert first["phonetic"] == "/rʌf/" and first["meaning"] == "（日子）难熬的" and first["kind"] == "word"
        again = client.post("/api/english/explain", json=body, headers=ORIGIN).json()
        assert again["item_id"] == first["item_id"]
    assert len(lookup_model) == 1  # the second time came from the cache
    [item] = user.english.items("word")
    assert item["text"] == "rough" and item["phonetic"] == "/rʌf/" and item["seen_count"] == 2 and "sounds rough" in item["context"]


def test_looking_up_needs_english_and_a_key_and_says_when_the_answer_was_unusable(user, lookup_model, monkeypatch):
    with TestClient(backend.app) as client:
        assert client.post("/api/english/explain", json={"text": "难受"}, headers=ORIGIN).status_code == 400
        assert client.post("/api/english/explain", json={"text": "x" * 201}, headers=ORIGIN).status_code == 400
        monkeypatch.setattr(practice, "parse_explanation", lambda text: None)
        assert client.post("/api/english/explain", json={"text": "rough"}, headers=ORIGIN).status_code == 502
        monkeypatch.setattr(backend, "API_KEY", "")
        assert client.post("/api/english/explain", json={"text": "terrible"}, headers=ORIGIN).status_code == 400
    assert user.english.items("word") == []
