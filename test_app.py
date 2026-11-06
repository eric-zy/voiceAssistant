import asyncio
import threading
from types import SimpleNamespace
import time
import pytest

from fastapi.testclient import TestClient
import app as backend


def test_sentence_boundary():
    assert backend.split_sentence("你好！后面") == ("你好！", "后面")
    assert backend.split_sentence("没有结束") == (None, "没有结束")


def test_reject_cross_origin():
    from starlette.websockets import WebSocketDisconnect
    import pytest
    with TestClient(backend.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin":"https://example.com"}):
                pass


def test_cancel_asr_discards_old_result(monkeypatch):
    def delayed_asr(pcm):
        time.sleep(.15)
        return "不应发出的旧识别结果"
    monkeypatch.setattr(backend, "transcribe", delayed_asr)
    with TestClient(backend.app) as client:
        with client.websocket_connect("/ws", headers={"origin":"http://127.0.0.1:8765"}) as socket:
            socket.send_json({"type":"audio_start", "request_id":1})
            socket.send_bytes(b"\x00" * 6400)
            socket.send_json({"type":"audio_end", "request_id":1})
            assert socket.receive_json()["type"] == "status"
            socket.send_json({"type":"interrupt", "request_id":2})
            time.sleep(.25)
            socket.send_json({"type":"text", "request_id":2, "text":""})
            message = socket.receive_json()
            assert message["request_id"] == 2
            assert message["type"] == "error"


def test_sapi_generates_real_wave(monkeypatch):
    import os
    import pytest
    if os.name != "nt":
        pytest.skip("Windows only")
    monkeypatch.setattr(backend, "VOICE", "Microsoft Huihui Desktop")
    wav = asyncio.run(backend.synthesize_windows("你好，这是本地语音测试。"))
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) > 1000


@pytest.mark.parametrize("tts_error", [False, True])
@pytest.mark.parametrize("provider,mode", [("edge","smooth"),("cosyvoice","smooth"),("cosyvoice","balanced")])
def test_deepseek_stream_and_history(monkeypatch, tts_error, provider, mode):
    monkeypatch.setattr(backend, "TTS_PROVIDER", provider)
    monkeypatch.setattr(backend, "CLONE_PLAYBACK", mode)
    import httpx
    requests = []
    def handler(request):
        import json
        requests.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.url.path == "/chat/completions"
        return httpx.Response(200, text=(
            ': keepalive\n\n'
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"。"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"我们开始吧。"}}]}\n\n'
            'data: {"choices":[]}\n\n'
            'data: [DONE]\n\n'), headers={"content-type":"text/event-stream"})
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(backend.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    spoken = []
    async def fake_synthesize(text):
        spoken.append(text)
        if tts_error:
            raise RuntimeError("voice unavailable")
        return b"fake-wav"
    monkeypatch.setattr(backend, "synthesize", fake_synthesize)
    with TestClient(backend.app) as client:
        with client.websocket_connect("/ws", headers={"origin":"http://127.0.0.1:8765"}) as socket:
            for rid in (1,2):
                socket.send_json({"type":"text", "request_id":rid, "text":"你好"})
                tokens, audios = [], 0
                while True:
                    message = socket.receive_json()
                    assert message["request_id"] == rid
                    assert message["type"] != "error"
                    if message["type"] == "token":
                        tokens.append(message["text"])
                    if message["type"] == "audio":
                        audios += 1
                    if message["type"] == "done":
                        assert message["tts_failed"] == tts_error
                        break
                assert "".join(tokens) == "你好。我们开始吧。"
                assert audios == (0 if tts_error else (1 if provider == "cosyvoice" else 2))
    if provider == "cosyvoice" and mode == "smooth":
        assert spoken == ["你好。我们开始吧。"] * 2
    if provider == "cosyvoice" and mode == "balanced" and not tts_error:
        assert spoken == ["你好。我们开始吧。"] * 2
    assert len(requests[0]["messages"]) == 2
    assert len(requests[1]["messages"]) == 4


def test_short_lead_has_a_bound_and_keeps_all_text():
    text = "我们可以先检查你的网络连接，然后再处理其他的问题。"
    lead, rest = backend.split_lead(text)
    assert 1 <= len(lead) <= 20
    assert lead + rest == text
    assert backend.split_lead("可以，先这样检查网络。后面") == ("可以，先这样检查网络。", "后面")
    assert backend.split_lead("还没说完") == (None, "还没说完")
    assert backend.split_lead("好的。") == (None, "好的。")


def test_balanced_bounds_all_segments_after_acknowledgment(monkeypatch):
    import json
    import httpx
    text = "好的。文章开头写家境惨淡，祖母去世，为背影铺垫悲凉氛围；结尾父亲来信说大去之期不远，作者泪中再忆背影。"
    spoken = []
    original = httpx.AsyncClient
    payload = 'data: '+json.dumps({'choices':[{'delta':{'content':text}}]},ensure_ascii=False)+'\n\ndata: [DONE]\n\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200,text=payload))
    monkeypatch.setattr(backend.httpx,'AsyncClient',lambda **kw:original(transport=transport,**kw))
    monkeypatch.setattr(backend,'API_KEY','test-key')
    monkeypatch.setattr(backend,'TTS_PROVIDER','cosyvoice')
    monkeypatch.setattr(backend,'CLONE_PLAYBACK','balanced')
    async def synthesize(text):
        spoken.append(text)
        return b'fake-wave'
    monkeypatch.setattr(backend,'synthesize',synthesize)
    with TestClient(backend.app) as client:
        with client.websocket_connect('/ws',headers={'origin':'http://127.0.0.1:8765'}) as socket:
            socket.send_json({'type':'text','request_id':1,'text':'分析背影'})
            while True:
                message = socket.receive_json()
                assert message['type'] not in {'error','warning'}
                if message['type'] == 'done':
                    break
    assert spoken == ['好的。文章开头写家境惨淡，','祖母去世，为背影铺垫悲凉氛围；','结尾父亲来信说大去之期不远，','作者泪中再忆背影。']
    assert ''.join(spoken) == text
    assert all(len(segment) <= 20 for segment in spoken)


def test_balanced_starts_synthesis_before_llm_finishes(monkeypatch):
    import httpx
    gate = None
    spoken = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal gate
            gate = asyncio.Event()
            yield 'data: {"choices":[{"delta":{"content":"可以，我们先检查网络。"}}]}\n\n'.encode()
            await asyncio.wait_for(gate.wait(), 2)
            yield 'data: {"choices":[{"delta":{"content":"然后再检查配置。"}}]}\n\ndata: [DONE]\n\n'.encode()
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
    monkeypatch.setattr(backend.httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    monkeypatch.setattr(backend, 'API_KEY', 'test-key')
    monkeypatch.setattr(backend, 'TTS_PROVIDER', 'cosyvoice')
    monkeypatch.setattr(backend, 'CLONE_PLAYBACK', 'balanced')
    async def synthesize(text):
        spoken.append(text)
        gate.set()
        return b'fake-wave'
    monkeypatch.setattr(backend, 'synthesize', synthesize)
    with TestClient(backend.app) as client:
        with client.websocket_connect('/ws', headers={'origin':'http://127.0.0.1:8765'}) as socket:
            socket.send_json({'type':'text','request_id':1,'text':'怎么排查？'})
            while True:
                message = socket.receive_json()
                assert message['type'] != 'error'
                if message['type'] == 'done':
                    break
    assert spoken == ['可以，我们先检查网络。', '然后再检查配置。']


def test_missing_key_is_actionable(monkeypatch):
    monkeypatch.setattr(backend, "API_KEY", "")
    with TestClient(backend.app) as client:
        assert client.get("/api/status").json()["api_key_configured"] is False
        with client.websocket_connect("/ws", headers={"origin":"http://127.0.0.1:8765"}) as socket:
            socket.send_json({"type":"text", "request_id":1, "text":"你好"})
            message = socket.receive_json()
            assert message["type"] == "error" and "DEEPSEEK_API_KEY" in message["text"]


def fake_whisper(monkeypatch, *pieces):
    segments = [SimpleNamespace(text=text, no_speech_prob=doubt) for text, doubt in pieces]
    class Model:
        def transcribe(self, *args, **kwargs):
            return segments, None
    monkeypatch.setattr(backend, "asr_model", Model())
    monkeypatch.setattr(backend, "ASR_MODEL", str(backend.ROOT))


def test_whisper_repeating_its_prompt_counts_as_hearing_nothing(monkeypatch):
    fake_whisper(monkeypatch, (backend.SPEECH_PROMPT, .1))
    assert backend.transcribe(b"\0" * 6400) == ""
    fake_whisper(monkeypatch, ("以下是普通话。", .1))
    assert backend.transcribe(b"\0" * 6400) == ""


def test_text_whisper_doubts_is_speech_is_dropped(monkeypatch):
    fake_whisper(monkeypatch, ("请保佑我们。", .85), ("今天天气怎么样", .02))
    assert backend.transcribe(b"\0" * 6400) == "今天天气怎么样"


def test_traditional_output_becomes_simplified(monkeypatch):
    fake_whisper(monkeypatch, ("這個季節花開得正好", .02))
    assert backend.transcribe(b"\0" * 6400) == "这个季节花开得正好"


def test_decoding_is_bounded_so_one_clip_cannot_block_the_rest(monkeypatch):
    seen = {}
    class Model:
        def transcribe(self, samples, **options):
            seen.update(options)
            return [], None
    monkeypatch.setattr(backend, "asr_model", Model())
    monkeypatch.setattr(backend, "ASR_MODEL", str(backend.ROOT))
    backend.transcribe(b"\0" * 6400)
    assert seen["temperature"] == 0
    assert seen["max_new_tokens"] <= 300


def test_short_real_answers_survive_the_filter(monkeypatch):
    fake_whisper(monkeypatch, ("好的。", .38))
    assert backend.transcribe(b"\0" * 6400) == "好的。"


class ScriptedDetector:
    """Stands in for Silero: each binary frame plays the next scripted events."""
    MAX_SECONDS = 15
    script = []

    def __init__(self):
        self.steps = iter(type(self).script)
        self.max_seconds = 15

    def reset(self):
        pass

    def feed(self, pcm):
        return next(self.steps)


def listen_session(monkeypatch, script, heard, playing=False, reply=None):
    """Run scripted turns through /ws and return every voice event the page receives."""
    lines = iter(heard)
    monkeypatch.setattr(backend, "transcribe", lambda pcm: next(lines))
    monkeypatch.setattr(backend, "TurnDetector", ScriptedDetector)
    monkeypatch.setattr(ScriptedDetector, "script", script)
    events = []
    with TestClient(backend.app) as client:
        with client.websocket_connect("/ws", headers={"origin":"http://127.0.0.1:8765"}) as socket:
            socket.send_json({"type":"listen_start"})
            if playing:
                socket.send_json({"type":"playing", "text":SPOKEN, "recent":SPOKEN})
            for step in script:
                socket.send_bytes(b"\0\0")
            done = {"question", "stop", "ignored"}
            while not events or events[-1]["event"] not in done:
                message = socket.receive_json()
                assert message["type"] == "voice"
                events.append(message)
    return [(event["event"], event.get("text", event.get("reason", ""))) for event in events]


SPOKEN = "周末去公园走走挺不错的。这个季节花开得正好，空气也清新，可以带上相机拍拍照。"
SENTENCE = [[("start", None)], [("sustained", b"\0" * 6400)], [("end", b"\0" * 6400)]]


def test_a_spoken_sentence_becomes_a_question(monkeypatch):
    assert listen_session(monkeypatch, SENTENCE, ["今天天气怎么样"]) == [
        ("start", ""), ("transcribing", ""), ("question", "今天天气怎么样")]


def test_hearing_nothing_says_why_and_returns_to_waiting(monkeypatch):
    assert listen_session(monkeypatch, SENTENCE, [""])[-1] == ("ignored", "noise")


def test_failed_transcription_cannot_jam_listening(monkeypatch):
    def broken(pcm):
        raise RuntimeError("model crashed")
    monkeypatch.setattr(backend, "TurnDetector", ScriptedDetector)
    monkeypatch.setattr(ScriptedDetector, "script", SENTENCE)
    monkeypatch.setattr(backend, "transcribe", broken)
    with TestClient(backend.app) as client:
        with client.websocket_connect("/ws", headers={"origin":"http://127.0.0.1:8765"}) as socket:
            socket.send_json({"type":"listen_start"})
            for step in SENTENCE:
                socket.send_bytes(b"\0\0")
            while (message := socket.receive_json())["event"] != "ignored":
                pass
    assert message["reason"] == "noise"


def test_speaking_over_playback_pauses_then_interrupts(monkeypatch):
    assert listen_session(monkeypatch, SENTENCE, ["换个话题吧", "换个话题吧"], playing=True) == [
        ("start", ""), ("pause", ""), ("interrupt", ""), ("transcribing", ""), ("question", "换个话题吧")]


def test_a_noise_over_playback_pauses_then_resumes_where_it_was(monkeypatch):
    assert listen_session(monkeypatch, SENTENCE, ["", ""], playing=True) == [
        ("start", ""), ("pause", ""), ("transcribing", ""), ("resume", ""), ("ignored", "noise")]


def test_leaked_playback_resumes_and_is_named_as_echo(monkeypatch):
    leak = "空气也清新,可以带上相机。"
    assert listen_session(monkeypatch, SENTENCE, [leak, leak], playing=True)[-2:] == [("resume", ""), ("ignored", "echo")]


def test_stop_phrase_interrupts_and_stays_out_of_the_llm(monkeypatch):
    assert listen_session(monkeypatch, SENTENCE, ["别说了。", "别说了。"], playing=True)[-1] == ("stop", "")


def test_quick_speech_never_sustained_during_playback_still_counts(monkeypatch):
    script = [[("start", None)], [("end", b"\0" * 6400)]]
    assert listen_session(monkeypatch, script, ["等一下"], playing=True)[-1] == ("question", "等一下")


@pytest.mark.parametrize("heard", ["人走走,挺不错的。这个季节花开的。", "空气也清新,可以带上相机。", "你拍拍照", "解花开的正好。"])
def test_speakers_leaking_the_reply_do_not_interrupt_it(heard):
    assert backend.hearing_action(heard, SPOKEN, True) == ("ignored", "echo")
    assert backend.hearing_action(heard, SPOKEN, True, True) == ("ignored", "echo")


@pytest.mark.parametrize("heard", ["等一下", "换个话题吧", "明天会下雨吗", "你好"])
def test_a_person_talking_over_the_reply_is_not_mistaken_for_it(heard):
    assert backend.hearing_action(heard, SPOKEN, True) == ("question", "")
    assert backend.hearing_action(heard, SPOKEN, True, True) == ("interrupt", "")


def test_a_follow_up_sharing_words_with_the_reply_is_not_echo():
    playing = "你问的是今天天气吧？告诉我城市，我帮你查。"
    assert backend.hearing_action("今天天气怎么样", playing, True) == ("question", "")


def test_a_single_stray_word_does_not_interrupt():
    assert backend.hearing_action("嗯", SPOKEN, True) == ("ignored", "short")
    assert backend.hearing_action("对", "", False) == ("question", "")


@pytest.mark.parametrize("heard", ["停", "挺。", "别说了。", "安静一下"])
def test_stop_phrases_during_a_reply_do_not_reach_the_llm(heard):
    assert backend.hearing_action(heard, SPOKEN, True) == ("stop", "")


def test_stop_word_inside_a_question_stays_a_question():
    assert backend.hearing_action("停车场在哪", SPOKEN, True) == ("question", "")


def test_the_same_words_are_a_normal_question_when_nothing_is_playing():
    assert backend.hearing_action("别说了", "", False) == ("question", "")


def test_audio_messages_say_which_text_they_speak(monkeypatch):
    import httpx
    payload = 'data: {"choices":[{"delta":{"content":"今天天气真不错。"}}]}\n\ndata: [DONE]\n\n'
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=payload))
    monkeypatch.setattr(backend.httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    async def fake_synthesize(text):
        return b"fake-wav"
    monkeypatch.setattr(backend, "synthesize", fake_synthesize)
    with TestClient(backend.app) as client:
        with client.websocket_connect("/ws", headers={"origin":"http://127.0.0.1:8765"}) as socket:
            socket.send_json({"type":"text", "request_id":1, "text":"你好"})
            while (message := socket.receive_json())["type"] != "audio":
                pass
    assert message["text"] == "今天天气真不错。"


def test_mic_test_reports_what_was_heard_without_keeping_it(monkeypatch):
    monkeypatch.setattr(backend, "measure_speech", lambda pcm: (.93, .4))
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "你好呀")
    with TestClient(backend.app) as client:
        response = client.post("/api/mic-test", content=b"\0\0" * 8000, headers={"origin":"http://127.0.0.1:8765"})
    assert response.json() == {"peak":.93, "speech_share":.4, "heard":"你好呀", "rms":0.0}


def test_mic_test_is_only_for_the_local_page():
    with TestClient(backend.app) as client:
        assert client.post("/api/mic-test", content=b"\0\0", headers={"origin":"https://example.com"}).status_code == 403

