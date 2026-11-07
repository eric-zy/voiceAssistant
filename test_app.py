import dataclasses
from datetime import datetime, timedelta
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



def tool_then_answer(monkeypatch):
    """Fake DeepSeek: a weather call first, then a plain answer once the tool result is back."""
    import httpx, json
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["messages"][-1]["role"] == "tool":
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"明天有雨。"}}]}\n\ndata: [DONE]\n\n')
        call = {"index": 0, "id": f"c{len(requests)}", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}
        return httpx.Response(200, text='data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}) + '\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(backend.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(backend, "TTS_PROVIDER", "edge")
    monkeypatch.setattr(backend, "fillers", backend.FillerCache())
    spoken = []

    async def fake_synthesize(text):
        spoken.append(text)
        return b"fake-wav"
    monkeypatch.setattr(backend, "synthesize", fake_synthesize)

    async def fake_weather(city, date=""):
        return {"place": city, "weather": "中雨"}
    tool = backend.default_registry.get("get_weather")
    monkeypatch.setitem(backend.default_registry.tools, "get_weather", dataclasses.replace(tool, run=fake_weather))
    return requests, spoken


def ask(socket, rid, text="北京明天下雨吗"):
    socket.send_json({"type": "text", "request_id": rid, "text": text})
    messages = []
    while not messages or messages[-1]["type"] != "done":
        messages.append(socket.receive_json())
        assert messages[-1]["type"] != "error", messages[-1]
    return messages


def test_a_tool_turn_shows_status_speaks_the_answer_and_stays_in_history(monkeypatch):
    requests, spoken = tool_then_answer(monkeypatch)
    with TestClient(backend.app) as client:
        with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as socket:
            first = ask(socket, 1)
            ask(socket, 2)
    assert {"type": "status", "request_id": 1, "text": "正在查询天气…"} in first
    assert "".join(m["text"] for m in first if m["type"] == "token") == "明天有雨。"
    assert "明天有雨。" in spoken
    assert requests[0]["tools"][0]["function"]["name"] == "get_weather"
    assert [m["role"] for m in requests[1]["messages"]] == ["system", "user", "assistant", "tool"]
    # The second question sees the whole first turn, including the tool messages.
    assert [m["role"] for m in requests[2]["messages"]] == ["system", "user", "assistant", "tool", "assistant", "user"]


def test_filler_plays_only_once_cached_and_is_marked_for_the_echo_filter(monkeypatch):
    _, spoken = tool_then_answer(monkeypatch)
    with TestClient(backend.app) as client:
        with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as socket:
            first = ask(socket, 1)
            second = ask(socket, 2)
    assert [m["text"] for m in first if m["type"] == "audio"] == ["明天有雨。"]
    assert [m["text"] for m in second if m["type"] == "audio"] == [backend.FILLER_PHRASE, "明天有雨。"]
    assert spoken.count(backend.FILLER_PHRASE) == 1


def test_the_prompt_carries_the_clock_and_the_default_city(monkeypatch):
    requests, _ = tool_then_answer(monkeypatch)
    monkeypatch.setattr(backend, "DEFAULT_CITY", "苏州")
    with TestClient(backend.app) as client:
        with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as socket:
            ask(socket, 1)
    system = requests[0]["messages"][0]["content"]
    assert system.startswith(backend.SYSTEM) and "用户默认城市：苏州" in system and "当前时间：" in system


PROMPT = "要执行演示操作吗？请说确定执行或取消。"


@pytest.mark.parametrize("heard,action", [
    ("确认", "approve"), ("确认。", "approve"), ("确定执行", "approve"),
    ("取消", "deny"), ("不要", "deny"), ("算了", "deny"), ("停", "deny"), ("别说了", "deny"),
    ("好", "question"), ("嗯", "question"), ("是", "question"), ("确认吗", "question"), ("不确认", "question"), ("今天天气怎么样", "question")])
def test_only_a_full_yes_approves(heard, action):
    assert backend.confirmation_action(heard, PROMPT, True)[0] == action


@pytest.mark.parametrize("heard,reason", [("确定执行", "echo"), ("请说确定执行或取消", "echo"), ("取消", "echo"), ("演示操作", "echo"), ("", "noise")])
def test_the_question_cannot_answer_itself_while_it_plays(heard, reason):
    assert backend.confirmation_action(heard, PROMPT, False) == ("ignored", reason)


def test_a_yes_before_the_question_finished_does_not_count():
    assert backend.confirmation_action("确认", "别的内容", False) == ("ignored", "early")


def test_the_prompt_still_in_recent_text_does_not_block_a_real_yes():
    assert backend.confirmation_action("确定执行", PROMPT, True)[0] == "approve"


def confirm_session(monkeypatch, tmp_path, script=()):
    """Fake DeepSeek calling a confirm tool, with the action log in tmp_path; returns (ran, log, requests)."""
    import httpx, json
    requests, ran = [], []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["messages"][-1]["role"] == "tool":
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"处理好了。"}}]}\n\ndata: [DONE]\n\n')
        if "聊天" in body["messages"][-1]["content"]:
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"聊聊吧。"}}]}\n\ndata: [DONE]\n\n')
        call = {"index": 0, "id": "c1", "type": "function", "function": {"name": "risky", "arguments": "{}"}}
        return httpx.Response(200, text='data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}) + '\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(backend.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(backend, "TTS_PROVIDER", "edge")

    async def fake_synthesize(text):
        return b"fake-wav"
    monkeypatch.setattr(backend, "synthesize", fake_synthesize)

    async def risky():
        ran.append(True)
        return {"done": True}
    tool = backend.default_registry.get("get_weather")
    monkeypatch.setitem(backend.default_registry.tools, "risky", dataclasses.replace(
        tool, name="risky", parameters={"type": "object", "properties": {}}, run=risky, filler=False, level="confirm", confirm_text="要执行演示操作吗？"))
    log = backend.ActionLog(tmp_path / "agent.db")
    monkeypatch.setattr(backend, "action_log", log)
    monkeypatch.setattr(backend, "DEMO_AUDIO_DIR", tmp_path / "heard")
    monkeypatch.setattr(backend, "TurnDetector", ScriptedDetector)
    monkeypatch.setattr(ScriptedDetector, "script", list(script))
    return ran, log, requests


def receive_until(socket, *kinds):
    messages = []
    while True:
        messages.append(socket.receive_json())
        assert messages[-1]["type"] != "error", messages[-1]
        if messages[-1]["type"] in kinds:
            return messages


def next_voice(socket, event):
    while True:
        message = socket.receive_json()
        if message.get("event") == event:
            return message


class ConfirmTalk:
    """Open a socket, ask, and stop at the confirmation question."""
    def __init__(self, ready=True, source="listen", text="清空吧"):
        self.ready, self.source, self.text = ready, source, text

    def __enter__(self):
        self.client = TestClient(backend.app)
        self.client.__enter__()
        self.context = self.client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"})
        self.socket = self.context.__enter__()
        self.socket.send_json({"type": "listen_start"})
        self.socket.send_json({"type": "text", "request_id": 1, "text": self.text, "source": self.source})
        self.before = receive_until(self.socket, "confirm_request")
        if self.ready:
            self.socket.send_json({"type": "confirm_ready", "request_id": 1})
            # Speech must begin after arming, and a scripted utterance claims to be 0.2 seconds long.
            time.sleep(.3)
        return self.socket

    def __exit__(self, *error):
        self.context.__exit__(*error)
        self.client.__exit__(*error)


SAID = [[("end", b"\0" * 6400)]]


def test_saying_confirm_runs_the_tool_and_logs_who_said_it(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "确认")
    ran, log, _ = confirm_session(monkeypatch, tmp_path, SAID)
    talk = ConfirmTalk()
    with talk as socket:
        socket.send_bytes(b"\0\0")
        messages = receive_until(socket, "done")
    types = [m["type"] for m in talk.before]
    assert [m["text"] for m in talk.before if m["type"] == "audio"] == [PROMPT] and types.index("audio") < types.index("confirm_request")
    assert {"type": "confirm_done", "request_id": 1, "approved": True} in messages
    assert ran == [True]
    assert log.recent() == [{"tool": "risky", "arguments": {}, "source": "listen", "outcome": "executed", "detail": "voice:确认"}]


def test_saying_cancel_runs_nothing_and_the_model_is_told(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "取消")
    ran, log, requests = confirm_session(monkeypatch, tmp_path, SAID)
    with ConfirmTalk() as socket:
        socket.send_bytes(b"\0\0")
        messages = receive_until(socket, "done")
    assert {"type": "confirm_done", "request_id": 1, "approved": False} in messages
    assert not ran and '"cancelled": true' in requests[-1]["messages"][-1]["content"]
    assert log.recent()[0]["outcome"] == "denied"


def test_an_utterance_that_began_during_the_question_cannot_approve(monkeypatch, tmp_path):
    # Transcribed after arming, but five seconds long: it started while the prompt was playing.
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "确认")
    ran, _, _ = confirm_session(monkeypatch, tmp_path, [[("end", b"\0" * 160000)]])
    with ConfirmTalk() as socket:
        socket.send_bytes(b"\0\0")
        event = next_voice(socket, "ignored")
        socket.send_json({"type": "interrupt", "request_id": 2})
    assert event["reason"] == "early" and not ran


def test_echo_during_the_question_is_ignored_and_it_stays_open(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "请说确定执行或取消")
    ran, _, _ = confirm_session(monkeypatch, tmp_path, SAID)
    with ConfirmTalk(ready=False) as socket:
        socket.send_json({"type": "playing", "text": PROMPT, "recent": PROMPT})
        socket.send_bytes(b"\0\0")
        event = next_voice(socket, "ignored")
        socket.send_json({"type": "confirm_reply", "request_id": 1, "approved": True})
        receive_until(socket, "done")
    assert event["reason"] == "echo" and ran == [True]


def test_speech_never_pauses_the_question(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "确认")
    ran, _, _ = confirm_session(monkeypatch, tmp_path, [[("sustained", b"\0" * 6400)], *SAID])
    with ConfirmTalk() as socket:
        socket.send_json({"type": "playing", "text": PROMPT, "recent": PROMPT})
        socket.send_bytes(b"\0\0")
        socket.send_bytes(b"\0\0")
        messages = receive_until(socket, "done")
    assert not any(m.get("event") == "pause" for m in messages) and ran == [True]


def test_silence_times_out_and_runs_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "CONFIRM_SECONDS", .05)
    ran, log, _ = confirm_session(monkeypatch, tmp_path)
    with ConfirmTalk() as socket:
        messages = receive_until(socket, "done")
    assert {"type": "confirm_done", "request_id": 1, "approved": False} in messages
    assert not ran and log.recent()[0]["detail"] == "timeout"


def test_a_page_that_never_reports_playback_also_times_out(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "CONFIRM_READY_SECONDS", .05)
    ran, log, _ = confirm_session(monkeypatch, tmp_path)
    with ConfirmTalk(ready=False) as socket:
        receive_until(socket, "done")
    assert not ran and log.recent()[0]["detail"] == "timeout"


@pytest.mark.parametrize("approved,expected", [(True, [True]), (False, [])])
def test_the_buttons_answer_too(monkeypatch, tmp_path, approved, expected):
    ran, log, _ = confirm_session(monkeypatch, tmp_path)
    with ConfirmTalk(source="text") as socket:
        socket.send_json({"type": "confirm_reply", "request_id": 1, "approved": approved})
        receive_until(socket, "done")
    assert ran == expected
    assert log.recent()[0]["source"] == "text" and log.recent()[0]["detail"] == "button"


def test_only_a_literal_true_approves_from_the_page(monkeypatch, tmp_path):
    ran, _, _ = confirm_session(monkeypatch, tmp_path)
    with ConfirmTalk() as socket:
        socket.send_json({"type": "confirm_reply", "request_id": 1, "approved": "false"})
        receive_until(socket, "done")
    assert not ran


def test_an_answer_for_an_older_turn_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "CONFIRM_SECONDS", .2)
    ran, _, _ = confirm_session(monkeypatch, tmp_path)
    with ConfirmTalk() as socket:
        socket.send_json({"type": "confirm_reply", "request_id": 0, "approved": True})
        receive_until(socket, "done")
    assert not ran


def test_interrupting_cancels_and_the_next_turn_works(monkeypatch, tmp_path):
    ran, log, _ = confirm_session(monkeypatch, tmp_path)
    with ConfirmTalk() as socket:
        socket.send_json({"type": "interrupt", "request_id": 2})
        socket.send_json({"type": "text", "request_id": 3, "text": "聊天"})
        messages = receive_until(socket, "done")
    assert "聊聊吧。" in [m.get("text") for m in messages]
    assert not ran and log.recent()[0]["outcome"] == "interrupted"


def test_saying_something_else_becomes_a_new_question(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "今天天气怎么样")
    ran, _, _ = confirm_session(monkeypatch, tmp_path, SAID)
    with ConfirmTalk() as socket:
        socket.send_bytes(b"\0\0")
        question = next_voice(socket, "question")
        socket.send_json({"type": "interrupt", "request_id": 2})
    assert question["text"] == "今天天气怎么样" and not ran


@pytest.mark.parametrize("demo,expected", [(True, 1), (False, 0)])
def test_what_was_heard_is_kept_only_in_demo_mode(monkeypatch, tmp_path, demo, expected):
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "确认")
    _, log, _ = confirm_session(monkeypatch, tmp_path, SAID)
    if demo:
        monkeypatch.setitem(backend.default_registry.tools, "demo_confirm", backend.default_registry.get("risky"))
    with ConfirmTalk() as socket:
        socket.send_bytes(b"\0\0")
        receive_until(socket, "done")
    assert sum(row["tool"] == "confirm_heard" for row in log.recent()) == expected


def test_status_lists_the_tools_so_demo_mode_can_be_seen(monkeypatch):
    monkeypatch.setitem(backend.default_registry.tools, "demo_confirm", backend.default_registry.get("get_weather"))
    with TestClient(backend.app) as client:
        assert {"get_weather", "demo_confirm"} <= set(client.get("/api/status").json()["tools"])


def demo_session(monkeypatch, tmp_path, enabled):
    ran, log, requests = confirm_session(monkeypatch, tmp_path)
    if enabled:
        monkeypatch.setitem(backend.default_registry.tools, "demo_confirm", backend.default_registry.get("risky"))
    return ran, requests


@pytest.mark.parametrize("text", ["测试确认", "测试确认。"])
def test_the_demo_phrase_reaches_confirmation_without_the_model(monkeypatch, tmp_path, text):
    ran, requests = demo_session(monkeypatch, tmp_path, True)
    with ConfirmTalk(text=text) as socket:
        socket.send_json({"type": "confirm_reply", "request_id": 1, "approved": True})
        messages = receive_until(socket, "done")
    assert ran == [True] and requests == []
    assert "演示结束，没有改动任何内容。" in [m.get("text") for m in messages]


def test_the_demo_phrase_means_nothing_when_the_demo_is_off(monkeypatch, tmp_path):
    ran, requests = demo_session(monkeypatch, tmp_path, False)
    with ConfirmTalk(text="测试确认") as socket:
        socket.send_json({"type": "interrupt", "request_id": 2})
    assert len(requests) == 1 and not ran


def test_the_rules_allow_tools_that_act_as_well_as_ones_that_look_up():
    from agent_loop import TOOL_RULES
    assert "要求执行操作" in TOOL_RULES


def busy_then_free(monkeypatch, busy_times):
    calls = []

    async def clone(profile, text, url):
        calls.append(text)
        if len(calls) <= busy_times:
            raise backend.CloneBusy("上一条克隆合成尚未结束")
        return b"RIFFaudio"
    monkeypatch.setattr(backend, "synthesize_clone", clone)
    monkeypatch.setattr(backend, "TTS_PROVIDER", "cosyvoice")
    monkeypatch.setattr(backend.voice_store, "get", lambda profile: {})
    return calls


def test_a_cancelled_segment_on_the_gpu_only_delays_the_next_one(monkeypatch):
    calls = busy_then_free(monkeypatch, 2)
    assert asyncio.run(backend.synthesize("你好")) == b"RIFFaudio"
    assert calls == ["你好"] * 3


def test_a_clone_service_that_stays_busy_still_fails(monkeypatch):
    busy_then_free(monkeypatch, 10 ** 6)
    monkeypatch.setattr(backend, "CLONE_BUSY_WAIT_SECONDS", .01)
    with pytest.raises(backend.CloneBusy):
        asyncio.run(backend.synthesize("你好"))


@pytest.mark.parametrize("demo,clips", [(True, 1), (False, 0)])
def test_recordings_are_kept_only_in_demo_mode(monkeypatch, tmp_path, demo, clips):
    monkeypatch.setattr(backend, "transcribe", lambda pcm: "确认")
    confirm_session(monkeypatch, tmp_path, SAID)
    if demo:
        monkeypatch.setitem(backend.default_registry.tools, "demo_confirm", backend.default_registry.get("risky"))
    with ConfirmTalk() as socket:
        socket.send_bytes(b"\0\0")
        receive_until(socket, "done")
    assert len(list((tmp_path / "heard").glob("*.wav"))) == clips


def test_only_the_newest_recordings_stay(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "DEMO_AUDIO_DIR", tmp_path)
    monkeypatch.setattr(backend, "DEMO_AUDIO_KEPT", 2)
    for _ in range(4):
        backend.keep_demo_audio(b"\0\0" * 100)
        time.sleep(1.01)
    assert len(list(tmp_path.glob("*.wav"))) == 2


def memory_llm(monkeypatch, tmp_path, call_name="remember", arguments='{"content":"不吃香菜"}', slow_tool=False):
    """Fake DeepSeek that always asks for one memory tool, unless the user is chatting."""
    import httpx, json
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if "聊天" in body["messages"][-1]["content"]:
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"聊聊吧。"}}]}\n\ndata: [DONE]\n\n')
        call = {"index": 0, "id": "c1", "type": "function", "function": {"name": call_name, "arguments": arguments}}
        return httpx.Response(200, text='data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}) + '\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(backend.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(backend, "TTS_PROVIDER", "edge")
    monkeypatch.setattr(backend, "action_log", backend.ActionLog(tmp_path / "actions.db"))

    async def fake_synthesize(text):
        return b"fake-wav"
    monkeypatch.setattr(backend, "synthesize", fake_synthesize)
    return requests


def talk(socket, rid, text):
    socket.send_json({"type": "text", "request_id": rid, "text": text})
    return receive_until(socket, "done")


def open_socket(client):
    return client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"})


def test_remembering_speaks_fixed_words_and_later_turns_can_use_it(monkeypatch, tmp_path, memories):
    requests = memory_llm(monkeypatch, tmp_path)
    with TestClient(backend.app) as client, open_socket(client) as socket:
        first = talk(socket, 1, "记住我不吃香菜")
        talk(socket, 2, "聊天")
    assert len(requests) == 2  # one per turn: the fixed wording needs no second round
    assert [m["text"] for m in first if m["type"] == "audio"] == ["记住了：不吃香菜"]
    changed = next(m for m in first if m["type"] == "memory_changed")
    assert changed["undo"] == {"action": "delete", "id": memories.recent()[0]["id"]}
    assert f"#{memories.recent()[0]['id']} 不吃香菜" in requests[1]["messages"][0]["content"]


def test_a_memory_survives_a_new_connection(monkeypatch, tmp_path, memories):
    requests = memory_llm(monkeypatch, tmp_path)
    memories.add("喜欢美式")
    with TestClient(backend.app) as client, open_socket(client) as socket:
        talk(socket, 1, "聊天")
    assert "喜欢美式" in requests[0]["messages"][0]["content"]


def test_the_write_is_logged_with_its_source(monkeypatch, tmp_path):
    memory_llm(monkeypatch, tmp_path)
    with TestClient(backend.app) as client, open_socket(client) as socket:
        talk(socket, 1, "记住")
    assert backend.action_log.recent()[0] == {"tool": "remember", "arguments": {"content": "不吃香菜"}, "source": "text", "outcome": "executed", "detail": ""}


def test_clearing_needs_a_yes_and_tells_the_page(monkeypatch, tmp_path, memories):
    memory_llm(monkeypatch, tmp_path, "clear_memories", "{}")
    memories.add("不吃香菜")
    with TestClient(backend.app) as client, open_socket(client) as socket:
        socket.send_json({"type": "text", "request_id": 1, "text": "清空"})
        before = receive_until(socket, "confirm_request")
        assert memories.recent() and "无法恢复" in before[-1]["text"]
        socket.send_json({"type": "confirm_reply", "request_id": 1, "approved": True})
        messages = receive_until(socket, "done")
    assert memories.recent() == []
    changed = next(m for m in messages if m["type"] == "memory_changed")
    assert changed["undo"] is None and "已清空" in changed["text"]


def test_a_write_cut_off_by_interruption_is_told_to_the_next_turn(monkeypatch, tmp_path, memories):
    requests = memory_llm(monkeypatch, tmp_path)
    release = threading.Event()

    async def slow_speech(text):
        await asyncio.to_thread(release.wait, 5)
        return b"fake-wav"
    with TestClient(backend.app) as client, open_socket(client) as socket:
        monkeypatch.setattr(backend, "synthesize", slow_speech)
        socket.send_json({"type": "text", "request_id": 1, "text": "记住"})
        receive_until(socket, "memory_changed")
        socket.send_json({"type": "interrupt", "request_id": 2})
        release.set()
        talk(socket, 3, "聊天")
        talk(socket, 4, "聊天")
    assert "被打断前" in requests[1]["messages"][0]["content"] and "remember" in requests[1]["messages"][0]["content"]
    assert "被打断前" not in requests[2]["messages"][0]["content"]


LOCAL = {"origin": "http://127.0.0.1:8765", "host": "127.0.0.1:8765"}


def test_the_page_can_list_delete_and_restore(memories):
    memory_id = memories.add("不吃香菜")
    with TestClient(backend.app) as client:
        assert client.get("/api/memories", headers=LOCAL).json()["memories"][0]["content"] == "不吃香菜"
        assert client.delete(f"/api/memories/{memory_id}", headers=LOCAL).status_code == 200
        assert memories.recent() == []
        assert client.post(f"/api/memories/{memory_id}/restore", headers=LOCAL).status_code == 200
    assert len(memories.recent()) == 1


def test_changing_a_missing_memory_is_a_404(memories):
    with TestClient(backend.app) as client:
        assert client.delete("/api/memories/999", headers=LOCAL).status_code == 404
        assert client.post("/api/memories/999/restore", headers=LOCAL).status_code == 404


def test_other_sites_cannot_read_or_change_memories(memories):
    memory_id = memories.add("不吃香菜")
    foreign = {"origin": "https://example.com", "host": "evil.example.com"}
    with TestClient(backend.app) as client:
        assert client.get("/api/memories", headers=foreign).status_code == 403
        assert client.delete(f"/api/memories/{memory_id}", headers=foreign).status_code == 403
        assert client.post(f"/api/memories/{memory_id}/restore", headers=foreign).status_code == 403
    assert len(memories.recent()) == 1


def reminder_session(monkeypatch, tmp_path, script=()):
    requests = memory_llm(monkeypatch, tmp_path)
    monkeypatch.setattr(backend, "REMINDER_POLL_SECONDS", .02)
    monkeypatch.setattr(backend, "TurnDetector", ScriptedDetector)
    monkeypatch.setattr(ScriptedDetector, "script", list(script))
    return requests


def due_now(reminders, content="开会", seconds_ago=1):
    return reminders.add(content, datetime.now() - timedelta(seconds=seconds_ago))


def wait_for_due(socket):
    while True:
        message = socket.receive_json()
        if message["type"] == "reminder_due":
            return message


def play_reminder(socket, rid, reminder_id):
    socket.send_json({"type": "interrupt", "request_id": rid})
    socket.send_json({"type": "announce", "request_id": rid, "reminder_id": reminder_id})
    return receive_until(socket, "done")


def test_a_due_reminder_is_spoken_and_delivered_once(monkeypatch, tmp_path, reminders):
    reminder_session(monkeypatch, tmp_path)
    reminder_id = due_now(reminders)
    with TestClient(backend.app) as client, open_socket(client) as socket:
        due = wait_for_due(socket)
        messages = play_reminder(socket, 1, due["reminder_id"])
    assert due["reminder_id"] == reminder_id
    assert [m["text"] for m in messages if m["type"] == "audio"] == ["提醒你：开会"]
    assert "提醒你：开会" in [m.get("text") for m in messages if m["type"] == "token"]
    assert reminders.pending() == []


def test_a_missed_reminder_says_it_is_late(monkeypatch, tmp_path, reminders):
    reminder_session(monkeypatch, tmp_path)
    due_now(reminders, seconds_ago=3600)
    with TestClient(backend.app) as client, open_socket(client) as socket:
        messages = play_reminder(socket, 1, wait_for_due(socket)["reminder_id"])
    assert any("你错过了一条提醒" in m.get("text", "") for m in messages if m["type"] == "token")


def test_two_pages_cannot_both_get_a_reminder(monkeypatch, tmp_path, reminders):
    reminder_session(monkeypatch, tmp_path)
    due_now(reminders)
    with TestClient(backend.app) as client, open_socket(client) as first, open_socket(client) as second:
        winner = wait_for_due(first)
        time.sleep(.2)
        second.send_json({"type": "text", "request_id": 1, "text": "聊天"})
        messages = receive_until(second, "done")
    assert winner and not any(m["type"] == "reminder_due" for m in messages)


def test_a_reminder_waits_while_a_reply_is_running(monkeypatch, tmp_path, reminders):
    reminder_session(monkeypatch, tmp_path)
    release = threading.Event()

    async def slow_speech(text):
        await asyncio.to_thread(release.wait, 5)
        return b"fake-wav"
    with TestClient(backend.app) as client, open_socket(client) as socket:
        monkeypatch.setattr(backend, "synthesize", slow_speech)
        socket.send_json({"type": "text", "request_id": 1, "text": "聊天"})
        receive_until(socket, "token")
        due_now(reminders)
        time.sleep(.2)
        assert len(reminders.pending()) == 1 and reminders.claim_due(datetime.now() - timedelta(hours=1)) is None
        release.set()
        messages = receive_until(socket, "done")
        assert not any(m["type"] == "reminder_due" for m in messages)
        assert wait_for_due(socket)


def test_a_reminder_waits_while_the_page_is_still_playing(monkeypatch, tmp_path, reminders):
    reminder_session(monkeypatch, tmp_path)
    with TestClient(backend.app) as client, open_socket(client) as socket:
        socket.send_json({"type": "playing", "text": SPOKEN, "recent": SPOKEN})
        time.sleep(.05)
        due_now(reminders)
        time.sleep(.2)
        socket.send_json({"type": "playing", "text": "", "recent": ""})
        assert wait_for_due(socket)


def test_a_reminder_waits_while_someone_is_speaking(monkeypatch, tmp_path, reminders):
    reminder_session(monkeypatch, tmp_path)

    class Speaking(ScriptedDetector):
        audio = bytearray(b"speech")
    monkeypatch.setattr(backend, "TurnDetector", Speaking)
    with TestClient(backend.app) as client, open_socket(client) as socket:
        socket.send_json({"type": "listen_start"})
        time.sleep(.05)
        due_now(reminders)
        time.sleep(.2)
        Speaking.audio = None
        try:
            assert wait_for_due(socket)
        finally:
            Speaking.audio = bytearray(b"speech")


def test_a_page_cannot_announce_what_it_did_not_claim(monkeypatch, tmp_path, reminders):
    reminder_session(monkeypatch, tmp_path)
    reminder_id = reminders.add("开会", datetime(2099, 1, 1))
    with TestClient(backend.app) as client, open_socket(client) as socket:
        socket.send_json({"type": "interrupt", "request_id": 1})
        socket.send_json({"type": "announce", "request_id": 1, "reminder_id": reminder_id})
        messages = talk(socket, 2, "聊天")
    assert not any("提醒你" in m.get("text", "") for m in messages) and len(reminders.pending()) == 1


def test_failed_speech_still_delivers_the_words(monkeypatch, tmp_path, reminders):
    reminder_session(monkeypatch, tmp_path)
    due_now(reminders)

    async def broken(text):
        raise RuntimeError("voice unavailable")
    monkeypatch.setattr(backend, "synthesize", broken)
    with TestClient(backend.app) as client, open_socket(client) as socket:
        messages = play_reminder(socket, 1, wait_for_due(socket)["reminder_id"])
    assert messages[-1]["tts_failed"] is True and any(m["type"] == "warning" for m in messages)
    assert reminders.pending() == []


def test_pending_reminders_reach_the_model(monkeypatch, tmp_path, reminders):
    requests = memory_llm(monkeypatch, tmp_path)
    reminders.add("开会", datetime(2099, 1, 1))
    with TestClient(backend.app) as client, open_socket(client) as socket:
        talk(socket, 1, "聊天")
    assert "开会" in requests[0]["messages"][0]["content"]


def test_the_page_can_list_cancel_and_restore_reminders(reminders):
    reminder_id = reminders.add("开会", datetime(2099, 1, 1))
    with TestClient(backend.app) as client:
        assert client.get("/api/reminders", headers=LOCAL).json()["reminders"][0]["content"] == "开会"
        assert client.delete(f"/api/reminders/{reminder_id}", headers=LOCAL).status_code == 200
        assert reminders.pending() == []
        assert client.post(f"/api/reminders/{reminder_id}/restore", headers=LOCAL).status_code == 200
        assert client.delete("/api/reminders/999", headers=LOCAL).status_code == 404
    assert len(reminders.pending()) == 1


def test_other_sites_cannot_read_or_change_reminders(reminders):
    reminder_id = reminders.add("开会", datetime(2099, 1, 1))
    foreign = {"origin": "https://example.com", "host": "evil.example.com"}
    with TestClient(backend.app) as client:
        assert client.get("/api/reminders", headers=foreign).status_code == 403
        assert client.delete(f"/api/reminders/{reminder_id}", headers=foreign).status_code == 403
    assert len(reminders.pending()) == 1
