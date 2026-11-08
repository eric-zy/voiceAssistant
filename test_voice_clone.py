import asyncio
from io import BytesIO
import wave

from fastapi.testclient import TestClient
import httpx
import numpy as np
import pytest

import app
from voice_clone import VoiceStore, synthesize_clone, stream_clone

ORIGIN = {"origin":"http://127.0.0.1:8765"}


def make_wav(seconds=4, rate=22050):
    audio = (.1 * np.sin(2 * np.pi * 220 * np.arange(int(rate * seconds)) / rate) * 32767).astype("<i2")
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(audio.tobytes())
    return output.getvalue()


def test_upload_select_preview_without_deepseek(tmp_path, monkeypatch):
    store = VoiceStore(tmp_path / "voices")
    monkeypatch.setattr(app, "voice_store", store)
    monkeypatch.setattr(app, "TTS_PROVIDER", "edge")
    monkeypatch.setattr(app, "VOICE", "zh-CN-XiaoxiaoNeural")
    monkeypatch.setattr(app, "CLONE_ID", "")
    async def fake_clone(profile, text, url):
        assert profile["prompt_text"] == "参考录音的文字"
        with wave.open(str(profile["wav_path"]), "rb") as wav:
            assert wav.getframerate() == 16000 and wav.getnchannels() == 1
        return make_wav(1, rate=24000)
    monkeypatch.setattr(app, "synthesize_clone", fake_clone)
    with TestClient(app.app) as client:
        result = client.post("/api/voices", headers=ORIGIN, data={"name":"我的声音", "prompt_text":"参考录音的文字"},
                             files={"audio":("test.wav", make_wav(), "audio/wav")})
        assert result.status_code == 200
        profile = result.json()
        assert profile["duration"] == 4
        result = client.post("/api/voice/select", headers=ORIGIN,
                             json={"provider":"cosyvoice", "profile_id":profile["id"]})
        assert result.status_code == 200
        # The choice is the signed-in user's own setting, not a server-wide one.
        assert app.user_context.current().settings()["profile_id"] == profile["id"]
        assert store.settings() == {}
        assert client.get("/api/status").json()["tts_voice"] == "我的声音"
        result = client.post("/api/voice/preview", headers=ORIGIN)
        assert result.status_code == 200 and result.headers["content-type"] == "audio/wav"
        with wave.open(BytesIO(result.content), "rb") as wav:
            assert wav.getframerate() == 24000


@pytest.mark.parametrize("duration", [1,31])
def test_reference_duration_is_bounded(tmp_path, duration):
    store = VoiceStore(tmp_path)
    with pytest.raises(ValueError):
        store.add("test", "文字", make_wav(duration))
    assert store.list() == []


def test_profile_id_cannot_escape_directory(tmp_path):
    with pytest.raises(ValueError):
        VoiceStore(tmp_path).get("../../anything")


def test_cross_origin_upload_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "voice_store", VoiceStore(tmp_path))
    with TestClient(app.app) as client:
        result = client.post("/api/voices", headers={"origin":"https://example.com"},
                             data={"name":"test", "prompt_text":"text"}, files={"audio":("test.wav",make_wav())})
        assert result.status_code == 403
    assert not list(tmp_path.iterdir())


def test_clone_transport_preserves_model_sample_rate(tmp_path):
    store = VoiceStore(tmp_path)
    created = store.add("test", "prompt words", make_wav())
    def handler(request):
        assert request.url.path == "/inference_zero_shot"
        assert b'prompt_wav' in request.content and b'prompt words' in request.content
        return httpx.Response(200, content=make_wav(1, 24000), headers={"content-type":"audio/wav"})
    result = asyncio.run(synthesize_clone(store.get(created["id"]), "hello", "http://127.0.0.1:50001", transport=httpx.MockTransport(handler)))
    with wave.open(BytesIO(result), "rb") as wav:
        assert wav.getframerate() == 24000


def test_reject_raw_pcm_with_no_sample_rate(tmp_path):
    store = VoiceStore(tmp_path)
    created = store.add("test", "words", make_wav())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b'\x00\x00'*16000))
    with pytest.raises(RuntimeError, match="WAV"):
        asyncio.run(synthesize_clone(store.get(created["id"]), "hello", "http://127.0.0.1:50001", transport=transport))


def test_busy_clone_is_actionable(tmp_path):
    store = VoiceStore(tmp_path)
    created = store.add("test", "words", make_wav())
    transport = httpx.MockTransport(lambda request: httpx.Response(429))
    with pytest.raises(RuntimeError, match="上一条克隆合成"):
        asyncio.run(synthesize_clone(store.get(created["id"]), "hello", "http://127.0.0.1:50001", transport=transport))


def test_cancel_closes_clone_response(tmp_path):
    store = VoiceStore(tmp_path)
    created = store.add("test", "words", make_wav())
    state = {"closed":False}
    async def run():
        entered = asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""
            async def aclose(self):
                state["closed"] = True
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
        task = asyncio.create_task(synthesize_clone(store.get(created["id"]), "hello", "http://127.0.0.1:50001", transport=transport))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert state["closed"]


def test_preview_disconnect_cancels_upstream(monkeypatch):
    state = {"closed":False}
    async def run():
        entered = asyncio.Event()
        async def fake_synthesize(text):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                state['closed'] = True
        monkeypatch.setattr(app,'synthesize',fake_synthesize)
        class Request:
            headers = {**ORIGIN, "host": "127.0.0.1:8765"}
            cookies = {}
            async def is_disconnected(self):
                await entered.wait()
                return True
        response = await app.preview_voice(Request())
        assert response.status_code == 499
    asyncio.run(run())
    assert state['closed']


def test_clone_stream_yields_before_upstream_finishes(tmp_path):
    import base64
    import json
    store = VoiceStore(tmp_path)
    created = store.add('test', 'words', make_wav())
    audio = make_wav(1, 24000)
    state = {'closed':False}
    async def run():
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (json.dumps({'audio':base64.b64encode(audio).decode()})+'\n').encode()
                await asyncio.Event().wait()
            async def aclose(self):
                state['closed'] = True
        transport = httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream()))
        chunks = stream_clone(store.get(created['id']), 'hello', 'http://127.0.0.1:50001', transport=transport)
        assert await asyncio.wait_for(anext(chunks), 1) == audio
        await chunks.aclose()
        assert state['closed']
    asyncio.run(run())


def upload(client, prompt_text):
    return client.post("/api/voices", headers=ORIGIN, data={"name": "我的声音", "prompt_text": prompt_text},
                       files={"audio": ("test.wav", make_wav(), "audio/wav")})


def test_a_transcript_that_does_not_match_the_recording_is_refused(monkeypatch):
    monkeypatch.setattr(app, "transcribe", lambda pcm: "今天天气很好，我们一起去公园散步吧")
    with TestClient(app.app) as client:
        response = upload(client, "测试一下")
        assert response.status_code == 400 and "对不上" in response.json()["detail"] and "公园散步" in response.json()["detail"]
        assert client.get("/api/voices").json()["profiles"] == []


def test_a_matching_transcript_is_kept(monkeypatch):
    monkeypatch.setattr(app, "transcribe", lambda pcm: "今天天气很好，我们一起去公园散步吧。")
    with TestClient(app.app) as client:
        assert upload(client, "今天天气很好我们一起去公园散步吧").status_code == 200
        assert len(client.get("/api/voices").json()["profiles"]) == 1


def test_a_recording_that_cannot_be_checked_is_not_blocked(monkeypatch):
    monkeypatch.setattr(app, "transcribe", lambda pcm: "")
    with TestClient(app.app) as client:
        assert upload(client, "任何文字").status_code == 200
