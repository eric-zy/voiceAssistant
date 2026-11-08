import asyncio
import io
import time

import av
from fastapi.testclient import TestClient
import numpy as np
import pytest

import agents
import app as backend
import audio_content
from audio_content import AI_LABEL, MAX_SEGMENT_CHARS, clean_script, segments, to_samples, wav_bytes
from llm_client import TextDelta

ORIGIN = {"origin": "http://127.0.0.1:8765"}


# Making a script readable aloud

def test_markdown_links_and_list_marks_are_removed():
    text = "# 标题\n\n- 第一点 **重要**\n1. 第二点 https://example.com/x 看这里\n\n> 引用`代码`"
    assert clean_script(text, 1000) == "标题\n\n第一点 重要 第二点 看这里\n\n引用代码"


def test_a_long_script_is_cut_at_a_paragraph_boundary():
    script = clean_script("一" * 100 + "\n\n" + "二" * 100 + "\n\n" + "三" * 100, 250)
    assert script == "一" * 100 + "\n\n" + "二" * 100


def test_sentences_are_grouped_up_to_the_limit_and_paragraph_ends_are_marked():
    script = "第一句。第二句！第三句？\n\n第二段只有一句。"
    assert segments(script) == [("第一句。第二句！第三句？", True), ("第二段只有一句。", True)]
    many = "。".join(["这是一句二十个字左右的话用来测试分组效果"] * 12) + "。"
    parts = segments(many)
    assert len(parts) > 1 and all(len(text) <= MAX_SEGMENT_CHARS for text, _ in parts)
    assert [end for _, end in parts] == [False] * (len(parts) - 1) + [True]
    assert "".join(text for text, _ in parts) == many


def test_a_sentence_too_long_for_one_go_is_cut_at_a_comma_or_hard():
    sentence = "，".join(["逗号之间的一小段话"] * 30) + "。"
    parts = segments(sentence)
    assert all(len(text) <= MAX_SEGMENT_CHARS for text, _ in parts) and "".join(t for t, _ in parts) == sentence
    assert all(text.endswith(("，", "。")) for text, _ in parts)
    assert all(len(text) <= MAX_SEGMENT_CHARS for text, _ in segments("无标点" * 100))


def tone(seconds, rate=16000):
    return (np.sin(np.arange(int(rate * seconds)) / rate * 2 * np.pi * 300) * 8000).astype("<i2")


def wav_at(samples, rate):
    import wave
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())
    return data.getvalue()


def test_wav_from_any_voice_comes_back_at_one_rate_and_length():
    assert abs(len(to_samples(wav_at(tone(1.0), 16000))) - audio_content.RATE) < 50


# Rendering

@pytest.fixture
def voice(monkeypatch):
    """A voice that speaks 0.01 s per character and records what it was asked to say."""
    said, failures = [], []

    async def synthesize(text):
        said.append(text)
        if failures and failures[0] == len(said):
            failures.pop(0)
            raise RuntimeError("edge-tts 合成失败")
        return wav_at(tone(len(text) * .01, 24000), 24000)
    monkeypatch.setattr(audio_content, "SYNTHESIZE", synthesize)
    monkeypatch.setattr(audio_content, "RETRY_SECONDS", 0)
    synthesize.said, synthesize.failures = said, failures
    return synthesize


def model(replies):
    calls = []

    def chat(model_name, messages, on_usage=None, **options):
        calls.append({"messages": messages, **options})
        reply = replies[messages[0]["content"][:4]]

        async def stream():
            yield TextDelta(reply)
        return stream()
    chat.calls = calls
    return chat


WRITER, EDITOR = "你是播客", "你是播音"
DRAFT = "## 勇气\n\n从前有一只小兔子。它很怕黑。"
EDITED = "从前有一只小兔子，它很怕黑。\n\n后来它学会了点一盏灯。"


def run_task(user, kind, payload, chat=None):
    async def go():
        runner = agents.TaskRunner(chat or model({WRITER: DRAFT, EDITOR: EDITED}), lambda: "deepseek-flash")
        task_id = runner.submit(user, kind, "测试", payload)
        while runner.running:
            await asyncio.sleep(.01)
        return task_id
    return user.tasks.get(asyncio.run(go()))


def test_a_script_is_written_then_edited_and_waits_for_the_user(user, voice):
    chat = model({WRITER: DRAFT, EDITOR: EDITED})
    task = run_task(user, "audio_script", {"topic": "讲一个关于勇气的故事", "minutes": 1}, chat)
    assert task["status"] == "done", task["error"]
    assert task["result"] == {"text": EDITED, "stage": "script", "minutes": 1, "chars": len(EDITED)}
    writer_request, editor_request = chat.calls[0]["messages"][1]["content"], chat.calls[1]["messages"][1]["content"]
    assert "讲一个关于勇气的故事" in writer_request and agents.DATA_NOTE not in writer_request  # the user's own request
    assert agents.DATA_NOTE in editor_request and DRAFT in editor_request                     # the draft is material
    assert all(call["max_tokens"] == 4000 for call in chat.calls) and chat.calls[0]["tools"] == []
    assert voice.said == []


def test_asking_for_research_gives_the_writer_web_search(user, voice):
    chat = model({WRITER: DRAFT, EDITOR: EDITED})
    run_task(user, "audio_script", {"topic": "今年的新能源车", "minutes": 3, "search": True}, chat)
    assert [tool["function"]["name"] for tool in chat.calls[0]["tools"]] == ["web_search"]


def test_voicing_starts_with_the_ai_label_and_keeps_the_order(user, voice):
    task = run_task(user, "audio_render", {"text": EDITED, "title": "兔子"})
    assert task["status"] == "done", task["error"]
    assert voice.said == [AI_LABEL, "从前有一只小兔子，它很怕黑。", "后来它学会了点一盏灯。"]
    path = user.root / "audio" / task["result"]["audio"]
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        seconds = sum(frame.samples / frame.sample_rate for frame in container.decode(audio=0))
        assert stream.metadata.get("comment") == "AI 合成内容" and stream.metadata.get("title") == "兔子"
    speech = sum(len(text) for text in voice.said) * .01
    assert abs(seconds - (speech + 3 * audio_content.PARAGRAPH_GAP)) < .1
    assert task["result"]["seconds"] == round(seconds) and task["result"]["bytes"] == path.stat().st_size


def test_skipping_the_review_goes_straight_to_audio(user, voice):
    task = run_task(user, "audio_script", {"topic": "勇气", "minutes": 1, "render": True})
    assert task["result"]["stage"] == "audio" and voice.said[0] == AI_LABEL


def test_a_segment_that_fails_once_is_retried_and_twice_fails_the_task_saying_which(user, voice):
    voice.failures.append(2)
    assert run_task(user, "audio_render", {"text": EDITED})["status"] == "done"
    voice.said.clear()
    voice.failures.extend([2, 3])
    task = run_task(user, "audio_render", {"text": EDITED})
    assert task["status"] == "failed" and task["error"].startswith("第 1 段合成失败")


def test_a_full_quota_stops_before_any_synthesis(user, voice, monkeypatch):
    monkeypatch.setattr(audio_content, "QUOTA_BYTES", 10)
    (user.root / "audio").mkdir(parents=True)
    (user.root / "audio" / ("0" * 32 + ".ogg")).write_bytes(b"x" * 20)
    task = run_task(user, "audio_render", {"text": EDITED})
    assert task["status"] == "failed" and "500 MB" in task["error"] and voice.said == []


# The page's endpoints

@pytest.fixture
def client(user, voice, monkeypatch):
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(backend, "task_runner", agents.TaskRunner(model({WRITER: DRAFT, EDITOR: EDITED}), lambda: "deepseek-flash"))
    with TestClient(backend.app) as test_client:
        yield test_client


def wait_done(client, task_id):
    for _ in range(300):
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(.01)
    raise AssertionError(task)


def test_the_page_asks_for_a_script_then_voices_it_then_plays_and_deletes_it(client, user):
    first = client.post("/api/audio/scripts", json={"topic": "勇气的故事", "minutes": 1}, headers=ORIGIN).json()["id"]
    script = wait_done(client, first)["result"]["text"]
    second = client.post("/api/audio/render", json={"text": script + "\n\n我改了结尾。", "title": "兔子"}, headers=ORIGIN).json()["id"]
    task = wait_done(client, second)
    assert task["title"] == "音频：兔子" and task["result"]["text"].endswith("我改了结尾。")
    url = f"/api/audio/{task['result']['audio']}"
    played = client.get(url)
    assert played.status_code == 200 and played.headers["content-type"] == "audio/ogg" and played.content.startswith(b"OggS")
    assert client.delete(url, headers=ORIGIN).json() == {"ok": True}
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("body,message", [
    ({"topic": "x"}, "2 到 500 字"), ({"topic": "勇气", "minutes": 4}, "1、3、5、10")])
def test_bad_script_requests_are_refused(client, body, message):
    response = client.post("/api/audio/scripts", json=body, headers=ORIGIN)
    assert response.status_code == 400 and message in response.json()["detail"]


def test_voicing_needs_no_key_but_writing_does(client, monkeypatch):
    monkeypatch.setattr(backend, "API_KEY", "")
    assert client.post("/api/audio/scripts", json={"topic": "勇气"}, headers=ORIGIN).status_code == 400
    task_id = client.post("/api/audio/render", json={"text": "你好。"}, headers=ORIGIN).json()["id"]
    assert wait_done(client, task_id)["status"] == "done"


def test_scripts_too_long_or_empty_are_refused(client):
    assert client.post("/api/audio/render", json={"text": "  "}, headers=ORIGIN).status_code == 400
    too_long = "字" * (audio_content.MAX_CHARS + 1)
    assert client.post("/api/audio/render", json={"text": too_long}, headers=ORIGIN).status_code == 400


def test_only_well_formed_names_reach_the_disk(client):
    for name in ["..%2Faccounts.db", "abc.ogg", "0" * 32 + ".wav"]:
        assert client.get(f"/api/audio/{name}").status_code == 404
        assert client.delete(f"/api/audio/{name}", headers=ORIGIN).status_code == 404
    assert client.post("/api/audio/render", json={"text": "你好。"}).status_code == 403
