"""Local ASR + DeepSeek text chat + configurable TTS. Run on loopback only."""
import asyncio
import base64
import contextlib
from difflib import SequenceMatcher
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import httpx
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from voice_clone import VoiceStore, synthesize_clone, stream_clone
from turn_detector import TurnDetector, measure_speech
from zhconv import convert as to_chinese_variant
from fastapi.staticfiles import StaticFiles
from api_config import normalize_key, fingerprint
from agent_loop import build_system_prompt, run_turn
from conversation import Conversation
from fillers import FillerCache, PHRASE as FILLER_PHRASE
from llm_client import ToolCallStarted, stream_chat
from tools import default_registry
from urllib.parse import urlsplit
from windows_asyncio import disconnect_logging

ROOT = Path(__file__).resolve().parent
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
API_KEY = normalize_key(os.getenv("DEEPSEEK_API_KEY", ""))
DEFAULT_CITY = os.getenv("VOICE_DEFAULT_CITY", "").strip()
fillers = FillerCache()
ASR_MODEL = os.getenv("VOICE_ASR_MODEL", str(ROOT / "models" / "whisper-base"))
voice_store = VoiceStore(ROOT / "voices")
saved_voice = voice_store.settings()
TTS_PROVIDER = os.getenv("VOICE_TTS_PROVIDER", saved_voice.get("provider", "edge")).lower()
default_voice = "Microsoft Huihui Desktop" if TTS_PROVIDER == "windows" else "zh-CN-XiaoxiaoNeural"
VOICE = os.getenv("VOICE_TTS_VOICE", saved_voice.get("voice", default_voice) if saved_voice.get("provider") == TTS_PROVIDER else default_voice)
CLONE_ID = saved_voice.get("profile_id", "")
COSYVOICE_URL = os.getenv("COSYVOICE_URL", "http://127.0.0.1:50001").rstrip("/")
CLONE_PLAYBACK = os.getenv("VOICE_CLONE_PLAYBACK", "balanced").lower()
if CLONE_PLAYBACK not in {"balanced", "smooth", "stream"}:
    CLONE_PLAYBACK = "balanced"
TTS_RATE = os.getenv("VOICE_TTS_RATE", "-5%")
TTS_PITCH = os.getenv("VOICE_TTS_PITCH", "+0Hz")
MAX_BYTES = 16000 * 2 * 30
SYSTEM = "你是中文语音助手。请用自然口语回答，直接说重点，不要用‘好的’‘当然’‘没问题’等应答语开场。首句不超过15字，再按需补充一两句，通常总字数不超过80字，不使用Markdown。用户要求详细解释时可以展开。"
# Steers Whisper toward clean Mandarin; it still emits traditional characters at times.
SPEECH_PROMPT = "以下是普通话的句子。"
# Whisper often hears a short 停 as 挺.
STOP_COMMAND = re.compile(r"(?:停|挺|停下|停一下|暂停|别说了|不要说了|闭嘴|安静|安静一下)[\W_]*")
OWN_VOICE_MATCH = .6
# A stray word in the room should not end a reply; the Pipecat docs use the same idea.
MIN_INTERRUPT_CHARS = 2
BARGE_IN_MAX_SECONDS = 8
ASR_TIMEOUT_SECONDS = 8
# Measured: speech, even a lone 对, stays under .4; invented text from noise is over .5.
NO_SPEECH_LIMIT = .45
latency_log = logging.getLogger("uvicorn.error")
@contextlib.asynccontextmanager
async def lifespan(application):
    with disconnect_logging(asyncio.get_running_loop()):
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
asr_model = None
# Cancellation cannot stop CPU inference. A real thread lock prevents overlapping inference.
import threading
asr_lock = threading.Lock()


def is_own_voice(heard: str, playing: str) -> bool:
    """The speakers leak into the mic; compare with the segments just played, not the whole reply."""
    heard, playing = re.sub(r"[\W_]+", "", heard), re.sub(r"[\W_]+", "", playing)
    if not heard or not playing:
        return False
    blocks = SequenceMatcher(None, heard, playing, autojunk=False).get_matching_blocks()
    return sum(block.size for block in blocks if block.size >= 2) >= len(heard) * OWN_VOICE_MATCH


def hearing_action(heard: str, playing: str = "", active: bool = False, partial: bool = False):
    """Decide what an utterance does; returns (action, reason for ignoring it).

    active means a reply is being generated or played, so speech has to earn an interruption.
    """
    words = re.sub(r"[\W_]+", "", heard)
    if not words:
        return "ignored", "noise"
    if is_own_voice(heard, playing):
        return "ignored", "echo"
    stop = active and STOP_COMMAND.fullmatch(heard.strip())
    if active and not stop and len(words) < MIN_INTERRUPT_CHARS:
        return "ignored", "short"
    if partial:
        return "interrupt", ""
    return ("stop" if stop else "question"), ""


def transcribe(pcm: bytes) -> str:
    global asr_model
    if len(pcm) % 2 or not 6400 <= len(pcm) <= MAX_BYTES:
        raise ValueError("录音应为 0.2～30 秒的 16kHz 单声道 PCM16")
    with asr_lock:
        if not Path(ASR_MODEL).is_dir():
            raise ValueError("尚未准备识别模型，请先运行 prepare_asr.py")
        if asr_model is None:
            from faster_whisper import WhisperModel
            asr_model = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8", local_files_only=True)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        # Default retries at rising temperatures took 12 s on a 0.8 s clip of leaked playback,
        # holding asr_lock so every later sentence queued behind it. Decode once, bounded.
        segments, _ = asr_model.transcribe(samples, language="zh", beam_size=3, vad_filter=True,
                                           condition_on_previous_text=False, initial_prompt=SPEECH_PROMPT,
                                           temperature=0, max_new_tokens=300, no_repeat_ngram_size=3)
        text = "".join(segment.text for segment in segments if segment.no_speech_prob < NO_SPEECH_LIMIT).strip()
        # Traditional output would never match the simplified reply when filtering out the speakers.
        text = to_chinese_variant(text, "zh-cn")
        # On unclear audio Whisper repeats all or part of its prompt instead of returning nothing.
        words, prompt = re.sub(r"[\W_]+", "", text), re.sub(r"[\W_]+", "", SPEECH_PROMPT)
        return "" if words in prompt or prompt in words else text


def analyse_microphone(pcm: bytes):
    """Measure one test recording; nothing is stored or sent anywhere."""
    peak, share = measure_speech(pcm)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    try:
        heard = transcribe(pcm)
    except ValueError:
        heard = ""
    return {"peak":round(peak, 2), "speech_share":round(share, 2), "heard":heard,
            "rms":round(float(np.sqrt(np.mean(samples ** 2))), 4) if samples.size else 0.0}


def split_sentence(pending: str):
    match = re.search(r"[。！？!?；;\n]", pending)
    if match:
        end = match.end()
    elif len(pending) >= 90:
        end = 90
    else:
        return None, pending
    return pending[:end], pending[end:]


def split_lead(pending: str):
    """Bound every speech segment; do not synthesize a tiny acknowledgment alone."""
    for match in re.finditer(r"[。！？!?；;\n，,：:]", pending[:20]):
        words = re.sub(r"[\W_]+", "", pending[:match.end()])
        filler = words in {"好的", "好", "可以", "当然", "没问题", "好的没问题", "当然可以", "你好", "是的"}
        if len(words) >= 6 and not filler:
            return pending[:match.end()], pending[match.end():]
    if len(pending) >= 20:
        return pending[:20], pending[20:]
    return None, pending


async def synthesize(text: str) -> bytes:
    if TTS_PROVIDER == "cosyvoice":
        try:
            profile = voice_store.get(CLONE_ID)
            return await synthesize_clone(profile, text, COSYVOICE_URL)
        except (httpx.HTTPError, OSError) as error:
            raise RuntimeError("无法连接本地 CosyVoice 服务，请先运行 ./start_cosyvoice.ps1") from error
    if TTS_PROVIDER == "windows":
        return await synthesize_windows(text)
    if TTS_PROVIDER != "edge":
        raise RuntimeError("VOICE_TTS_PROVIDER 仅支持 edge、windows 或 cosyvoice")
    import edge_tts
    try:
        async with asyncio.timeout(45):
            audio = bytearray()
            communicate = edge_tts.Communicate(text, VOICE, rate=TTS_RATE, pitch=TTS_PITCH,
                                              connect_timeout=10, receive_timeout=20)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
            if not audio:
                raise RuntimeError("empty audio")
            return bytes(audio)
    except Exception as error:
        # Cancellation propagates; network errors never trigger a hidden voice fallback.
        raise RuntimeError("edge-tts 合成失败，请检查网络、音色名称和语速配置；文字回复仍可使用") from error


async def synthesize_parts(text):
    # Short buffered segments keep up with playback on the GTX 1060; streaming
    # re-runs flow for every chunk, so it stays opt-in.
    if TTS_PROVIDER != "cosyvoice" or CLONE_PLAYBACK != "stream":
        yield await synthesize(text)
        return
    try:
        async with contextlib.aclosing(stream_clone(voice_store.get(CLONE_ID), text, COSYVOICE_URL)) as chunks:
            async for audio in chunks:
                yield audio
    except httpx.HTTPError as error:
        raise RuntimeError("无法连接流式克隆服务，请检查 CosyVoice 服务窗口") from error


async def synthesize_windows(text: str) -> bytes:
    if os.name != "nt":
        raise RuntimeError("当前语音合成使用 Windows System.Speech")
    runtime_temp = ROOT / "work" / "runtime-temp"
    runtime_temp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voice-chat-", dir=runtime_temp) as folder:
        input_path = Path(folder) / "request.json"
        output_path = Path(folder) / "speech.wav"
        input_path.write_text(json.dumps({"text": text, "voice": VOICE}, ensure_ascii=False), encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "speak.ps1"),
            "-InputPath", str(input_path), "-OutputPath", str(output_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            _, error = await asyncio.wait_for(process.communicate(), 45)
            if process.returncode:
                raise RuntimeError("Windows 语音合成失败，请检查 VOICE_TTS_VOICE 和已安装的语音")
            return output_path.read_bytes()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
async def status():
    # Do not return credentials or claim key validity without calling the provider.
    display_voice = VOICE
    if TTS_PROVIDER == "cosyvoice":
        try:
            display_voice = voice_store.get(CLONE_ID)["name"]
        except (ValueError, OSError, KeyError):
            display_voice = "参考音色缺失，请重新选择"
    return {"asr_ready": (Path(ASR_MODEL) / "model.bin").is_file(), "llm_model": LLM_MODEL,
            "tts_voice": display_voice,
            "tts_provider": TTS_PROVIDER,
            "clone_playback": CLONE_PLAYBACK,
            "tts_rate": TTS_RATE, "tts_pitch": TTS_PITCH, "api_key_configured": bool(API_KEY),
            "key_fingerprint": fingerprint(API_KEY),
            "api_host": urlsplit(DEEPSEEK_URL).hostname, "server_pid": os.getpid(), "voice_protocol": 4}


def require_local_page(request):
    if request.headers.get("origin") not in {"http://127.0.0.1:8765", "http://localhost:8765"}:
        raise HTTPException(403, "请通过本机项目页面操作")


@app.get("/api/voices")
async def voices():
    ready = False
    service = {}
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            result = await client.get(COSYVOICE_URL + "/health")
            service = result.json() if result.status_code == 200 else {}
            ready = service.get("ready", False)
    except (httpx.HTTPError, ValueError):
        pass
    return {"profiles":voice_store.list(), "provider":TTS_PROVIDER, "voice":VOICE,
            "profile_id":CLONE_ID, "clone_service_ready":ready,
            "clone_service_busy":service.get("busy", False), "clone_device":service.get("device", "unknown")}


@app.post("/api/voices")
async def upload_voice(request: Request, name: str = Form(), prompt_text: str = Form(), audio: UploadFile = File()):
    require_local_page(request)
    data = await audio.read(10 * 1024 * 1024 + 1)
    await audio.close()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "参考录音不可超过10MB")
    try:
        return await asyncio.to_thread(voice_store.add, name, prompt_text, data)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


class VoiceSelection(BaseModel):
    provider: str
    voice: str = "zh-CN-XiaoxiaoNeural"
    profile_id: str = ""


@app.post("/api/voice/select")
async def select_voice(request: Request, selection: VoiceSelection):
    require_local_page(request)
    if selection.provider not in {"edge", "windows", "cosyvoice"}:
        raise HTTPException(400, "未知语音引擎")
    if not 1 <= len(selection.voice) <= 120:
        raise HTTPException(400, "音色名称长度无效")
    if selection.provider == "cosyvoice":
        try:
            voice_store.get(selection.profile_id)
        except (ValueError, OSError) as error:
            raise HTTPException(400, "请先上传并选择参考音色") from error
    global TTS_PROVIDER, VOICE, CLONE_ID
    settings = selection.model_dump()
    voice_store.save_settings(settings)
    TTS_PROVIDER, VOICE, CLONE_ID = selection.provider, selection.voice, selection.profile_id
    return {"ok":True}


@app.post("/api/mic-test")
async def mic_test(request: Request):
    require_local_page(request)
    pcm = await request.body()
    if len(pcm) > MAX_BYTES or len(pcm) % 2:
        raise HTTPException(400, "录音长度无效")
    return await asyncio.to_thread(analyse_microphone, pcm)


@app.post("/api/voice/preview")
async def preview_voice(request: Request):
    require_local_page(request)
    task = asyncio.create_task(synthesize("你好，今天过得怎么样？"))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.25)
            if await request.is_disconnected():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return Response(status_code=499)
        audio = await task
    except (ValueError, RuntimeError) as error:
        raise HTTPException(503, str(error)) from error
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return Response(audio, media_type="audio/wav" if audio.startswith(b"RIFF") else "audio/mpeg")


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    # Loopback service, no cross-origin microphone/audio ingestion.
    if ws.headers.get("origin") not in {"http://127.0.0.1:8765", "http://localhost:8765"}:
        await ws.close(code=1008)
        return
    await ws.accept()
    history = Conversation()
    task = None
    pcm = bytearray()
    recording = False
    detector = None
    hear_queue = asyncio.Queue()
    hear_task = None
    playing_text = recent_text = ""
    paused = interrupted = False
    request_id = 0
    send_lock = asyncio.Lock()

    async def send(kind, rid, **data):
        async with send_lock:
            await ws.send_json({"type": kind, "request_id": rid, **data})

    async def send_audio(rid, audio, text):
        await send("audio", rid, data=base64.b64encode(audio).decode("ascii"),
                   mime="audio/wav" if audio.startswith(b"RIFF") else "audio/mpeg", text=text)

    async def stop():
        nonlocal task
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            task = None

    def reply_active():
        return bool(playing_text) or interrupted or (task is not None and not task.done())

    async def voice(event, **data):
        await send("voice", None, event=event, **data)

    async def on_turn(event, audio):
        nonlocal paused
        if event == "meter":
            await voice("meter", chance=round(audio, 2))
            return
        if event == "sustained":
            # Confirm with Whisper while paused, so a cough costs a moment, not the reply.
            if playing_text:
                paused = True
                await voice("pause")
                hear_queue.put_nowait((audio, True))
            return
        if event == "end":
            hear_queue.put_nowait((audio, False))
            event = "transcribing"
        await voice(event)

    async def hear_worker():
        # One worker, and every failure counts as hearing nothing: listening cannot get stuck.
        nonlocal paused, interrupted
        while True:
            audio, partial = await hear_queue.get()
            if partial and not hear_queue.empty():
                continue
            try:
                heard = await asyncio.wait_for(asyncio.to_thread(transcribe, audio), ASR_TIMEOUT_SECONDS)
            except ValueError:
                heard = ""
            except Exception:
                latency_log.exception("hands-free transcription failed")
                heard = ""
            active = reply_active()
            action, reason = hearing_action(heard, recent_text if active else "", active, partial)
            if partial:
                if action == "interrupt":
                    paused, interrupted = False, True
                    await voice("interrupt")
                continue
            was_paused, paused, interrupted = paused, False, False
            if action == "ignored":
                if was_paused:
                    await voice("resume")
                await voice("ignored", reason=reason)
            else:
                await voice(action, text=heard if action == "question" else "")

    async def reply(rid, text=None, audio=None):
        worker = None
        tts_failed = False
        started = time.perf_counter()
        first_audio = True
        first_token = True

        def mark(stage):
            latency_log.info("voice_latency request=%s stage=%s seconds=%.2f", rid, stage, time.perf_counter() - started)

        try:
            if audio is not None:
                await send("status", rid, text="正在识别…")
                text = await asyncio.to_thread(transcribe, audio)
                mark("asr_done")
                if not text:
                    await send("error", rid, text="未识别到语音，请靠近麦克风重试")
                    return
                await send("transcript", rid, text=text)
            if not API_KEY:
                raise ValueError("请设置 DEEPSEEK_API_KEY 环境变量后重启后端")
            await send("status", rid, text="正在生成回复…")
            # LLM output is capped at 512 tokens. Keep text delivery independent
            # of slow GPU consumption even when it produces many short segments.
            queue = asyncio.Queue()

            async def speak_worker():
                nonlocal tts_failed, first_audio
                failed = False
                while True:
                    sentence = await queue.get()
                    if sentence is None:
                        return
                    if failed:
                        continue
                    try:
                        label = "正在合成语音…"
                        if TTS_PROVIDER == "cosyvoice":
                            label = {"stream":"正在流式合成语音…", "smooth":"正在合成完整回复，完成后连续播放；文字可先阅读…",
                                     "balanced":"正在合成首段（最多20字）…" if first_audio else "正在合成下一小段（最多20字），文字可先阅读…"}[CLONE_PLAYBACK]
                        await send("status", rid, text=label)
                        mark("tts_start")
                        async with contextlib.aclosing(synthesize_parts(sentence)) as parts:
                            async for audio in parts:
                                if first_audio:
                                    mark("first_audio")
                                    first_audio = False
                                await send_audio(rid, audio, sentence)
                    except Exception as error:
                        failed = True
                        tts_failed = True
                        reason = str(error) if isinstance(error, (ValueError, RuntimeError)) else "请检查网络及音色配置"
                        await send("warning", rid, text=f"{TTS_PROVIDER} 播报失败：{reason}；文字回复仍可使用")

            worker = asyncio.create_task(speak_worker())
            # Bound all balanced-mode chunks, including the remainder after the lead.
            clone_full_reply = TTS_PROVIDER == "cosyvoice"
            pending = ""
            filler_played = False

            async def on_llm_event(event):
                nonlocal pending, first_token, filler_played
                if isinstance(event, ToolCallStarted):
                    mark("tool_start")
                    tool = default_registry.get(event.name)
                    await send("status", rid, text=tool.status if tool else "正在处理…")
                    # Text already spoken covers the wait, and two parallel calls need one filler.
                    if tool and tool.filler and first_token and not filler_played:
                        filler_played = True
                        audio = fillers.take((TTS_PROVIDER, VOICE, CLONE_ID), synthesize)
                        if audio:
                            await send_audio(rid, audio, FILLER_PHRASE)
                    return
                token = event.text
                pending += token
                if first_token:
                    mark("first_text")
                    first_token = False
                await send("token", rid, text=token)
                if clone_full_reply and CLONE_PLAYBACK == "balanced":
                    while True:
                        segment, pending = split_lead(pending)
                        if segment is None:
                            break
                        await queue.put(segment)
                while not clone_full_reply:
                    sentence, pending = split_sentence(pending)
                    if sentence is None:
                        break
                    await queue.put(sentence)

            def chat(messages, **options):
                return stream_chat(DEEPSEEK_URL, API_KEY, LLM_MODEL, messages, **options)

            user_message = {"role": "user", "content": text}
            system_message = {"role": "system", "content": build_system_prompt(SYSTEM, DEFAULT_CITY)}
            added = await run_turn([system_message, *history.messages(), user_message], default_registry, chat, on_llm_event)
            mark("llm_done")
            if pending.strip():
                await queue.put(pending)
            await queue.put(None)
            await worker
            mark("all_audio_sent")
            history.add_turn([user_message, *added])
            await send("done", rid, tts_failed=tts_failed)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "处理失败，请检查模型、服务连接和终端日志"
            await send("error", rid, text=message)
        finally:
            if worker:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if detector is not None and not recording:
                    # Leaked playback may never fall silent, so give up on it sooner.
                    detector.max_seconds = BARGE_IN_MAX_SECONDS if playing_text else TurnDetector.MAX_SECONDS
                    for event, audio in detector.feed(message["bytes"]):
                        await on_turn(event, audio)
                    continue
                if not recording or len(pcm) + len(message["bytes"]) > MAX_BYTES:
                    await ws.close(code=1009)
                    break
                pcm.extend(message["bytes"])
                continue
            data = json.loads(message.get("text", "{}"))
            kind = data.get("type")
            if kind == "listen_start":
                detector = await asyncio.to_thread(TurnDetector)
                hear_task = hear_task or asyncio.create_task(hear_worker())
                continue
            if kind == "listen_stop":
                detector = None
                paused = interrupted = False
                continue
            if kind == "playing":
                playing_text, recent_text = str(data.get("text", ""))[:500], str(data.get("recent", ""))[:1000]
                continue
            rid = data.get("request_id")
            if not isinstance(rid, int) or rid < request_id:
                continue
            if kind in {"interrupt", "text", "audio_start"}:
                await stop()
                request_id = rid
                pcm.clear()
                recording = kind == "audio_start"
                if recording and detector is not None:
                    detector.reset()
                if kind == "text":
                    text = str(data.get("text", "")).strip()
                    if not 1 <= len(text) <= 2000:
                        await send("error", rid, text="请输入 1～2000 字")
                    else:
                        task = asyncio.create_task(reply(rid, text=text))
            elif kind == "audio_end" and recording and rid == request_id:
                recording = False
                task = asyncio.create_task(reply(rid, audio=bytes(pcm)))
                pcm.clear()
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        if hear_task:
            hear_task.cancel()
        await stop()
