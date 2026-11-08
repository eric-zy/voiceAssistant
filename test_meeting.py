import asyncio
from datetime import datetime, timedelta
import io
import time
import wave

import av
from fastapi.testclient import TestClient
import numpy as np
import pytest

import agents
import app as backend
import meeting
from llm_client import TextDelta
import usage

RATE = 16000
ORIGIN = {"origin": "http://127.0.0.1:8765"}


class LoudIsSpeech:
    """Stands in for Silero: a frame is speech when it is loud. Real speech detection is tested elsewhere."""

    def __call__(self, frame):
        samples = np.frombuffer(frame, "<i2").astype(float)
        return .9 if np.abs(samples).mean() > 1000 else .01


@pytest.fixture(autouse=True)
def fake_vad(monkeypatch):
    monkeypatch.setattr(meeting, "SileroStream", LoudIsSpeech)


def audio(*parts):
    """parts: (seconds, loud?) in order -> PCM16 bytes at 16 kHz."""
    rng = np.random.default_rng(0)
    chunks = [(rng.integers(-8000, 8000, int(RATE * seconds)) if loud else np.zeros(int(RATE * seconds))).astype("<i2")
              for seconds, loud in parts]
    return np.concatenate(chunks).tobytes()


def wav_file(pcm, rate=RATE, channels=1):
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)
    return data.getvalue()


def mp3_file(seconds=2.0, rate=44100):
    data = io.BytesIO()
    samples = (np.sin(np.arange(int(rate * seconds)) / rate * 2 * np.pi * 300) * 8000).astype("<i2")
    with av.open(data, "w", format="mp3") as output:
        stream = output.add_stream("mp3", rate=rate)
        stream.layout = "stereo"
        frame = av.AudioFrame.from_ndarray(np.stack([samples, samples]).reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = rate
        for packet in [*stream.encode(frame), *stream.encode(None)]:
            output.mux(packet)
    return data.getvalue()


# Decoding

def test_any_audio_becomes_16khz_mono_of_the_same_length(tmp_path):
    (tmp_path / "a.wav").write_bytes(wav_file(audio((1.5, True))))
    assert len(meeting.decode(tmp_path / "a.wav")) == int(1.5 * RATE) * 2
    (tmp_path / "b.mp3").write_bytes(mp3_file(2.0))
    assert abs(len(meeting.decode(tmp_path / "b.mp3")) / 2 / RATE - 2.0) < .1


def test_a_file_that_is_not_audio_or_too_long_is_refused(tmp_path, monkeypatch):
    (tmp_path / "x.mp3").write_bytes(b"not audio at all" * 100)
    with pytest.raises(ValueError, match="无法读取"):
        meeting.decode(tmp_path / "x.mp3")
    monkeypatch.setattr(meeting, "MAX_SECONDS", 1)
    (tmp_path / "long.wav").write_bytes(wav_file(audio((3, True))))
    with pytest.raises(ValueError, match="3 小时"):
        meeting.decode(tmp_path / "long.wav")


# Finding speech

def test_short_pauses_stay_inside_a_segment_and_long_ones_split_it():
    pcm = audio((1, False), (2, True), (.5, False), (2, True), (2, False), (1, True), (1, False))
    segments = meeting.find_speech(pcm)
    assert len(segments) == 2
    (a_start, a_end), (b_start, b_end) = segments
    assert a_start == pytest.approx(1 - meeting.PAD_SECONDS, abs=.05) and a_end == pytest.approx(5.5 + meeting.PAD_SECONDS, abs=.05)
    assert b_start == pytest.approx(7.5 - meeting.PAD_SECONDS, abs=.05)


def test_blips_are_dropped_and_long_speech_is_cut_to_fit_the_recognizer():
    assert meeting.find_speech(audio((1, False), (.1, True), (2, False))) == []
    segments = meeting.find_speech(audio((60, True)))
    assert all(end - start <= meeting.MAX_SEGMENT_SECONDS + 1e-6 for start, end in segments)
    assert segments[0][0] == 0 and segments[-1][1] == pytest.approx(60, abs=.05)
    assert all(a[1] == pytest.approx(b[0]) for a, b in zip(segments, segments[1:]))


def test_each_segment_is_transcribed_with_its_time_and_silent_ones_are_left_out():
    pcm = audio((1, False), (2, True), (2, False), (2, True), (2, False), (2, True), (1, False))
    heard, progress = iter(["第一句", "", "第三句"]), []

    def transcribe(clip):
        assert 1.5 * RATE * 2 < len(clip) <= meeting.MAX_SEGMENT_SECONDS * RATE * 2
        return next(heard)
    lines = meeting.transcribe_pcm(pcm, transcribe, lambda done, total: progress.append((done, total)))
    assert [line["text"] for line in lines] == ["第一句", "第三句"]
    assert lines[1]["start"] == pytest.approx(9 - meeting.PAD_SECONDS, abs=.1)
    assert progress[-1] == (3, 3)
    assert meeting.transcript_text(lines).splitlines()[1].startswith("[00:08] 第三句")


def test_a_clip_the_recognizer_refuses_is_skipped_and_cancelling_stops_between_segments():
    pcm = audio((2, True), (2, False), (2, True))

    def refuse(clip):
        raise ValueError("录音应为 0.2～30 秒")
    assert meeting.transcribe_pcm(pcm, refuse) == []
    with pytest.raises(asyncio.CancelledError):
        meeting.transcribe_pcm(pcm, lambda clip: "字", stopped=lambda: True)


def test_long_meetings_get_hour_clocks():
    assert meeting.clock(65) == "01:05" and meeting.clock(3725) == "01:02:05"


# To-dos from the agent

def test_todos_are_read_from_json_even_with_words_around_it():
    text = '好的：[{"task": "写发布说明", "owner": "小王", "due_text": "周四之前", "due_at": "2026-11-12T18:00:00+08:00"},' \
           ' {"task": "", "owner": "x"}, "垃圾", {"task": "找财务", "due_at": "下周一"}]'
    assert meeting.parse_todos(text) == [
        {"task": "写发布说明", "owner": "小王", "due_text": "周四之前", "due_at": "2026-11-12T18:00:00"},
        {"task": "找财务", "owner": "", "due_text": "", "due_at": ""}]


@pytest.mark.parametrize("text", ["没有待办", "[{broken", "{}", '{"task": "x"}'])
def test_anything_that_is_not_a_clean_list_gives_no_todos(text):
    assert meeting.parse_todos(text) == []


# The whole task

def scripted_chat(replies):
    calls = []

    def chat(model, messages, on_usage=None, **options):
        calls.append(messages)
        reply = replies[messages[0]["content"][:6]]

        async def stream():
            try:
                yield TextDelta(reply)
            finally:
                if on_usage:
                    on_usage({"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100})
        return stream()
    chat.calls = calls
    return chat


NOTES = "议题：发布时间。结论：周五发布。"
TODOS = '[{"task": "写发布说明", "owner": "小王", "due_text": "周四之前", "due_at": "2099-01-01T18:00:00"}]'
REPLIES = {meeting.NOTES_AGENT.instructions[:6]: NOTES, meeting.TODO_AGENT.instructions[:6]: TODOS}


def test_a_recording_becomes_notes_and_proposed_todos_from_two_agents(user, monkeypatch):
    folder = user.root / "meetings"
    folder.mkdir(parents=True)
    (folder / "m.wav").write_bytes(wav_file(audio((1, False), (2, True), (2, False), (2, True), (1, False))))
    monkeypatch.setattr(meeting, "TRANSCRIBE", lambda clip: "周五发布，小王周四前写好说明")
    chat = scripted_chat(REPLIES)

    async def go():
        runner = agents.TaskRunner(chat, lambda: "deepseek-flash")
        task_id = runner.submit(user, "meeting_notes", "周会", {"file": "m.wav", "date": "2026-11-09"})
        while runner.running:
            await asyncio.sleep(.01)
        return task_id
    task = user.tasks.get(asyncio.run(go()))
    assert task["status"] == "done", task["error"]
    result = task["result"]
    assert result["text"] == NOTES and len(result["transcript"]) == 2 and result["seconds"] == 8
    assert result["proposals"] == [{"task": "写发布说明", "owner": "小王", "due_text": "周四之前", "due_at": "2099-01-01T18:00:00"}]
    assert len(chat.calls) == 2 and all("会议日期：2026-11-09" in call[1]["content"] for call in chat.calls)
    assert {row["agent"] for row in usage.LEDGER.summary(user.user_id)["agents"]} == {"meeting_notes", "meeting_todo"}
    assert user.reminders.pending() == []  # proposals only; nothing written until the user ticks them


def test_a_silent_recording_says_so_without_calling_the_model(user, monkeypatch):
    folder = user.root / "meetings"
    folder.mkdir(parents=True)
    (folder / "q.wav").write_bytes(wav_file(audio((3, False))))
    chat = scripted_chat(REPLIES)

    async def go():
        runner = agents.TaskRunner(chat, lambda: "deepseek-flash")
        task_id = runner.submit(user, "meeting_notes", "空", {"file": "q.wav"})
        while runner.running:
            await asyncio.sleep(.01)
        return task_id
    task = user.tasks.get(asyncio.run(go()))
    assert task["status"] == "done" and "没有识别到" in task["result"]["text"] and chat.calls == []


# The page's endpoints

@pytest.fixture
def client(user, monkeypatch):
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(meeting, "TRANSCRIBE", lambda clip: "周五发布")
    monkeypatch.setattr(backend, "task_runner", agents.TaskRunner(scripted_chat(REPLIES), lambda: "deepseek-flash"))
    with TestClient(backend.app) as test_client:
        yield test_client


def upload(client, name="周会.wav", data=None, **extra):
    data = data if data is not None else wav_file(audio((1, False), (2, True), (1, False)))
    return client.post("/api/meetings", files={"file": (name, data)}, data=extra, headers=ORIGIN)


def wait_done(client, task_id):
    for _ in range(300):
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(.01)
    raise AssertionError(task)


def recordings(user):
    folder = user.root / "meetings"
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


def test_uploading_a_recording_starts_the_notes_task(client, user):
    response = upload(client, date="2026-11-09")
    assert response.status_code == 200
    task = wait_done(client, response.json()["id"])
    assert task["status"] == "done" and task["title"] == "周会" and task["kind"] == "meeting_notes"
    assert recordings(user) == [task["result"]["file"]]
    assert user.action_log.recent()[0]["tool"] == "upload_meeting"


@pytest.mark.parametrize("name,data,extra,status,message", [
    ("周会.exe", b"x", {}, 400, "支持"),
    ("周会.mp3", b"not audio" * 50, {}, 400, "无法读取"),
    ("周会.wav", None, {"date": "下周一"}, 400, "日期格式")])
def test_bad_uploads_are_refused_and_leave_nothing_behind(client, user, name, data, extra, status, message):
    response = upload(client, name, data, **extra)
    assert response.status_code == status and message in response.json()["detail"]
    assert recordings(user) == []


def test_an_upload_over_the_limit_is_refused_and_removed(client, user, monkeypatch):
    monkeypatch.setattr(meeting, "MAX_UPLOAD_BYTES", 1000)
    assert upload(client).status_code == 413
    assert recordings(user) == []


def test_a_second_upload_while_one_runs_is_refused_and_removed(client, user, monkeypatch):
    async def slow(job):
        await asyncio.sleep(5)
    monkeypatch.setitem(agents.FEATURES, "meeting_notes", agents.Feature("meeting_notes", slow, 30))
    first = upload(client).json()["id"]
    assert upload(client).status_code == 429
    assert len(recordings(user)) == 1
    client.post(f"/api/tasks/{first}/cancel", headers=ORIGIN)


def test_uploads_need_a_key_and_the_page_origin(client, user, monkeypatch):
    assert client.post("/api/meetings", files={"file": ("a.wav", b"x")}).status_code == 403
    monkeypatch.setattr(backend, "API_KEY", "")
    assert upload(client).status_code == 400
    assert recordings(user) == []


def test_a_recording_can_be_deleted_but_only_by_its_own_name(client, user):
    task = wait_done(client, upload(client).json()["id"])
    assert client.delete("/api/meetings/..%2Faccounts.db", headers=ORIGIN).status_code == 404
    assert client.delete("/api/meetings/notahexname.wav", headers=ORIGIN).status_code == 404
    assert client.delete(f"/api/meetings/{task['result']['file']}", headers=ORIGIN).json() == {"ok": True}
    assert recordings(user) == []
    assert client.delete(f"/api/meetings/{task['result']['file']}", headers=ORIGIN).status_code == 404


def test_ticked_todos_become_reminders_and_bad_ones_are_explained(client, user):
    later = (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()
    earlier = (datetime.now() - timedelta(days=1)).replace(microsecond=0).isoformat()
    response = client.post("/api/meetings/reminders", headers=ORIGIN, json={"items": [
        {"task": "写发布说明", "due_at": later}, {"task": "找财务", "due_at": earlier}, {"task": "没时间", "due_at": ""}]})
    body = response.json()
    assert len(body["created"]) == 1 and len(body["problems"]) == 2
    assert [item["content"] for item in user.reminders.pending()] == ["写发布说明"]
    assert user.action_log.recent()[0]["source"] == "meeting"


def test_finished_notes_can_be_archived_into_the_knowledge_base(client, user):
    task = wait_done(client, upload(client, "周会/../x.wav", date="2026-11-09").json()["id"])
    saved = client.post("/api/meetings/archive", json={"task_id": task["id"]}, headers=ORIGIN).json()
    assert saved["name"] == "会议纪要-x-2026-11-09.md"
    text = (user.root / "knowledge" / saved["name"]).read_text(encoding="utf-8")
    assert NOTES in text and "写发布说明（小王） 截止：周四之前" in text and "[00:00] 周五发布" in text
    assert client.post("/api/meetings/archive", json={"task_id": 999}, headers=ORIGIN).status_code == 404
