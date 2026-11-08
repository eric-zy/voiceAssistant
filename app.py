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
import wave

import httpx
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from voice_clone import CloneBusy, VoiceStore, synthesize_clone, stream_clone
from turn_detector import TurnDetector, measure_speech
from zhconv import convert as to_chinese_variant
from fastapi.staticfiles import StaticFiles
from api_config import normalize_key, fingerprint
from agent_loop import ToolResult, build_system_prompt, demo_chat, run_turn, without_outside_results
from conversation import Conversation
from knowledge import MAX_DOCUMENT_BYTES
from fillers import FillerCache, PHRASE as FILLER_PHRASE
from llm_client import ToolCallStarted, stream_chat
from accounts import AccountStore
from memory_store import ActionLog  # noqa: F401  (tests build per-user logs through app)
import tools as agent_tools
from tools import default_registry
import user_context
from urllib.parse import urlsplit
from windows_asyncio import disconnect_logging
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parent
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
API_KEY = normalize_key(os.getenv("DEEPSEEK_API_KEY", ""))
DEFAULT_CITY = os.getenv("VOICE_DEFAULT_CITY", "合肥").strip()
fillers = FillerCache()
accounts = AccountStore(ROOT / "data" / "accounts.db")
contexts = user_context.ContextCache(ROOT / "data" / "users", agent_tools.knowledge_embedder)
SESSION_COOKIE = "session"
# Pages and sockets are accepted only from these origins; the hosts guard against DNS rebinding.
ALLOWED_ORIGINS = {origin.strip().rstrip("/") for origin in
                   os.getenv("VOICE_ALLOWED_ORIGINS", "http://127.0.0.1:8765,http://localhost:8765").split(",") if origin.strip()}
ALLOWED_HOSTS = {urlsplit(origin).netloc for origin in ALLOWED_ORIGINS}
# Behind HTTPS the cookie must be Secure; local development over plain HTTP cannot send it otherwise.
SECURE_COOKIE = os.getenv("VOICE_SECURE_COOKIE") == "1"
OPEN_REGISTRATION = os.getenv("VOICE_REGISTRATION", "open") == "open"
DEMO_AUDIO_DIR = ROOT / "data" / "heard"
LATENCY_FILE = ROOT / "data" / "latency.jsonl"
DEMO_AUDIO_KEPT = 40
ASR_MODEL = os.getenv("VOICE_ASR_MODEL", str(ROOT / "models" / "whisper-base"))
SENSEVOICE_DIR = ROOT / "models" / "sensevoice-small"
# SenseVoice when its model is present; VOICE_ASR_PROVIDER=whisper switches back.
ASR_PROVIDER = os.getenv("VOICE_ASR_PROVIDER", "").lower() or ("sensevoice" if (SENSEVOICE_DIR / "model.int8.onnx").is_file() else "whisper")
SPEECH_PEAK_MIN = .5
# Server-wide default voice for anyone who has not chosen one. It comes only from the environment: the old
# voices/settings.json held one person's cloned voice, which must not become everyone's default.
voice_store = VoiceStore(ROOT / "voices")
TTS_PROVIDER = os.getenv("VOICE_TTS_PROVIDER", "edge").lower()
default_voice = "Microsoft Huihui Desktop" if TTS_PROVIDER == "windows" else "zh-CN-XiaoxiaoNeural"
VOICE = os.getenv("VOICE_TTS_VOICE", default_voice)
CLONE_ID = ""
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
# Whole-utterance matches only: anything misheard falls through to not doing it. The prompt asks for
# 确定执行 because Whisper base gets it right far more often; a lone 确认 failed even on clean speech.
CONFIRM_YES = {"确认", "确定执行"}
CONFIRM_NO = {"取消", "不要", "算了"}
CONFIRM_SUFFIX = "请说确定执行或取消。"
DEMO_PHRASE = "测试确认"
CONFIRM_SECONDS = 20
CONFIRM_READY_SECONDS = 30
PCM_BYTES_PER_SECOND = 32000
# Cancelling a reply does not stop the segment on the GPU, which takes a few seconds at most.
CLONE_BUSY_WAIT_SECONDS = 10
REMINDER_POLL_SECONDS = 5
latency_log = logging.getLogger("uvicorn.error")
@contextlib.asynccontextmanager
async def lifespan(application):
    with disconnect_logging(asyncio.get_running_loop()):
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
asr_model = None
sense_model = None
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


def confirmation_action(heard: str, playing: str, armed: bool):
    """Decide what an utterance does to a pending confirmation; returns (action, reason ignored).

    armed means the question finished playing before this utterance began. Echo is only checked
    before that: afterwards the prompt, which contains 确认, is still in the recent text.
    """
    words = re.sub(r"[\W_]+", "", heard)
    if not words:
        return "ignored", "noise"
    if not armed and is_own_voice(heard, playing):
        return "ignored", "echo"
    if words in CONFIRM_YES:
        return ("approve", "") if armed else ("ignored", "early")
    if words in CONFIRM_NO or STOP_COMMAND.fullmatch(heard.strip()):
        return "deny", ""
    return "question", ""


class PendingConfirmation:
    def __init__(self):
        self.result = asyncio.get_running_loop().create_future()
        self.spoken = asyncio.Event()
        self.armed_at = None

    def accepts(self, started):
        # Judged by when speech began: the tail of the prompt can finish transcribing after arming.
        return self.armed_at is not None and started >= self.armed_at

    def answer(self, approved, detail):
        if not self.result.done():
            self.result.set_result((approved, detail))
        self.spoken.set()


@dataclass(frozen=True)
class VoiceChoice:
    provider: str
    voice: str
    clone_id: str
    store: VoiceStore


def current_voice():
    """The signed-in user's voice, or the server default for anything they have not chosen."""
    context = user_context.CURRENT.get(None)
    saved = context.settings() if context else {}
    provider = saved.get("provider") or TTS_PROVIDER
    voice = saved.get("voice") or VOICE
    # A clone is only ever one of the user's own uploads.
    return VoiceChoice(provider, voice, saved.get("profile_id", "") if context else CLONE_ID, context.voices if context else voice_store)


def current_city():
    context = user_context.CURRENT.get(None)
    saved = context.settings() if context else {}
    return saved["default_city"] if "default_city" in saved else DEFAULT_CITY


def record_latency(timings, reply_chars, provider=""):
    """Stage timings for finding what is slow. Holds seconds and a length, never what was said."""
    try:
        LATENCY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LATENCY_FILE.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"at": time.strftime("%H:%M:%S"), "voice": provider, "reply_chars": reply_chars, **timings}) + "\n")
    except OSError:
        latency_log.warning("could not write the latency file")


def keep_demo_audio(pcm):
    """Keep what Whisper was given, for work/diagnose_audio.py. Demo mode only, newest clips only."""
    DEMO_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    with wave.open(str(DEMO_AUDIO_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000}.wav"), "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(16000)
        clip.writeframes(pcm)
    for old in sorted(DEMO_AUDIO_DIR.glob("*.wav"))[:-DEMO_AUDIO_KEPT]:
        old.unlink()


def transcribe(pcm: bytes) -> str:
    if len(pcm) % 2 or not 6400 <= len(pcm) <= MAX_BYTES:
        raise ValueError("录音应为 0.2～30 秒的 16kHz 单声道 PCM16")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    with asr_lock:
        text = transcribe_sensevoice(pcm, samples) if ASR_PROVIDER == "sensevoice" else transcribe_whisper(samples)
    # Traditional output would never match the simplified reply when filtering out the speakers.
    return to_chinese_variant(text, "zh-cn")


def transcribe_sensevoice(pcm, samples):
    """SenseVoice Small via sherpa-onnx. On the webcam microphone it read 24/24 band-limited test phrases
    where Whisper base read 14/24, in a quarter of the time.

    It never returns nothing: silence, hum and clicks all came back as 我. Silero peaked at 0.11 or less on
    those and 0.96 or more on every real recording, so a clip below SPEECH_PEAK_MIN is treated as silence.
    """
    global sense_model
    if not (SENSEVOICE_DIR / "model.int8.onnx").is_file():
        raise ValueError("尚未准备 SenseVoice 模型，请运行 prepare_asr.py --sensevoice")
    if measure_speech(pcm)[0] < SPEECH_PEAK_MIN:
        return ""
    if sense_model is None:
        import sherpa_onnx
        # Fixed to Chinese: automatic detection read short Chinese replies as Japanese.
        sense_model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(SENSEVOICE_DIR / "model.int8.onnx"), tokens=str(SENSEVOICE_DIR / "tokens.txt"),
            num_threads=2, language="zh", use_itn=True)
    stream = sense_model.create_stream()
    stream.accept_waveform(16000, samples)
    sense_model.decode_stream(stream)
    return stream.result.text.strip()


def transcribe_whisper(samples):
    global asr_model
    if not Path(ASR_MODEL).is_dir():
        raise ValueError("尚未准备识别模型，请先运行 prepare_asr.py")
    if asr_model is None:
        from faster_whisper import WhisperModel
        asr_model = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8", local_files_only=True)
    # Default retries at rising temperatures took 12 s on a 0.8 s clip of leaked playback,
    # holding asr_lock so every later sentence queued behind it. Decode once, bounded.
    segments, _ = asr_model.transcribe(samples, language="zh", beam_size=3, vad_filter=True,
                                       condition_on_previous_text=False, initial_prompt=SPEECH_PROMPT,
                                       temperature=0, max_new_tokens=300, no_repeat_ngram_size=3)
    text = "".join(segment.text for segment in segments if segment.no_speech_prob < NO_SPEECH_LIMIT).strip()
    # On unclear audio Whisper repeats all or part of its prompt instead of returning nothing.
    words, prompt = re.sub(r"[\W_]+", "", to_chinese_variant(text, "zh-cn")), re.sub(r"[\W_]+", "", SPEECH_PROMPT)
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


async def synthesize_clone_when_free(profile, text):
    deadline = time.monotonic() + CLONE_BUSY_WAIT_SECONDS
    while True:
        try:
            return await synthesize_clone(profile, text, COSYVOICE_URL)
        except CloneBusy:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(.25)


async def synthesize(text: str) -> bytes:
    choice = current_voice()
    if choice.provider == "cosyvoice":
        try:
            profile = choice.store.get(choice.clone_id)
            return await synthesize_clone_when_free(profile, text)
        except (httpx.HTTPError, OSError) as error:
            raise RuntimeError("无法连接本地 CosyVoice 服务，请先运行 ./start_cosyvoice.ps1") from error
    if choice.provider == "windows":
        return await synthesize_windows(text, choice.voice)
    if choice.provider != "edge":
        raise RuntimeError("VOICE_TTS_PROVIDER 仅支持 edge、windows 或 cosyvoice")
    import edge_tts
    try:
        async with asyncio.timeout(45):
            audio = bytearray()
            communicate = edge_tts.Communicate(text, choice.voice, rate=TTS_RATE, pitch=TTS_PITCH,
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
    choice = current_voice()
    if choice.provider != "cosyvoice" or CLONE_PLAYBACK != "stream":
        yield await synthesize(text)
        return
    try:
        async with contextlib.aclosing(stream_clone(choice.store.get(choice.clone_id), text, COSYVOICE_URL)) as chunks:
            async for audio in chunks:
                yield audio
    except httpx.HTTPError as error:
        raise RuntimeError("无法连接流式克隆服务，请检查 CosyVoice 服务窗口") from error


async def synthesize_windows(text: str, voice: str = "") -> bytes:
    if os.name != "nt":
        raise RuntimeError("当前语音合成使用 Windows System.Speech")
    runtime_temp = ROOT / "work" / "runtime-temp"
    runtime_temp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voice-chat-", dir=runtime_temp) as folder:
        input_path = Path(folder) / "request.json"
        output_path = Path(folder) / "speech.wav"
        input_path.write_text(json.dumps({"text": text, "voice": voice or VOICE}, ensure_ascii=False), encoding="utf-8")
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


def default_session_user(connection):
    """The signed-in user for a request or socket, from its session cookie, or None."""
    user = accounts.user_for_token(connection.cookies.get(SESSION_COOKIE))
    return contexts.get(user) if user else None


# Looked up at call time, so tests can stand in a known user without weakening the origin checks.
session_user = default_session_user


def require_user(request, write=False):
    """Every API call: an allowed host, an allowed origin for anything that changes data, and a session.

    Plain GETs carry no Origin, so Host is what stops another site reaching this through DNS rebinding;
    writes also need Origin, which with SameSite cookies is the cross-site request defence.
    """
    if request.headers.get("host") not in ALLOWED_HOSTS:
        raise HTTPException(403, "请通过本站页面操作")
    if write and request.headers.get("origin") not in ALLOWED_ORIGINS:
        raise HTTPException(403, "请通过本站页面操作")
    context = session_user(request)
    if context is None:
        raise HTTPException(401, "请先登录")
    user_context.CURRENT.set(context)
    return context


def require_page_origin(request):
    if request.headers.get("host") not in ALLOWED_HOSTS or request.headers.get("origin") not in ALLOWED_ORIGINS:
        raise HTTPException(403, "请通过本站页面操作")


@app.get("/")
async def index(request: Request):
    page = "index.html" if session_user(request) else "login.html"
    return FileResponse(ROOT / "static" / page, headers={"Cache-Control": "no-store"})


class Credentials(BaseModel):
    email: str
    password: str


def signed_in(user):
    response = Response(json.dumps({"email": user["email"]}), media_type="application/json")
    response.set_cookie(SESSION_COOKIE, accounts.start_session(user["id"]), max_age=30 * 24 * 3600,
                        httponly=True, samesite="lax", secure=SECURE_COOKIE, path="/")
    return response


@app.post("/api/register")
async def register(request: Request, credentials: Credentials):
    require_page_origin(request)
    if not OPEN_REGISTRATION:
        raise HTTPException(403, "暂未开放注册")
    try:
        user = await asyncio.to_thread(accounts.register, credentials.email, credentials.password)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return signed_in(user)


@app.post("/api/login")
async def login(request: Request, credentials: Credentials):
    require_page_origin(request)
    try:
        user = await asyncio.to_thread(accounts.authenticate, credentials.email, credentials.password)
    except PermissionError as error:
        raise HTTPException(429, str(error)) from error
    if user is None:
        # One message for both cases, so the page does not reveal which emails have accounts.
        raise HTTPException(401, "邮箱或密码不正确")
    return signed_in(user)


@app.post("/api/logout")
async def logout(request: Request):
    require_page_origin(request)
    accounts.end_session(request.cookies.get(SESSION_COOKIE))
    response = Response(json.dumps({"ok": True}), media_type="application/json")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/me")
async def me(request: Request):
    context = require_user(request)
    return {"email": context.email, "is_admin": context.is_admin}


class Settings(BaseModel):
    default_city: str


@app.post("/api/settings")
async def save_settings(request: Request, settings: Settings):
    context = require_user(request, write=True)
    city = settings.default_city.strip()
    if len(city) > 30:
        raise HTTPException(400, "城市名太长")
    context.save_settings({"default_city": city})
    return {"default_city": city}


@app.get("/api/status")
async def status(request: Request):
    context = require_user(request)
    # Do not return credentials or claim key validity without calling the provider.
    choice = current_voice()
    display_voice = choice.voice
    if choice.provider == "cosyvoice":
        try:
            display_voice = choice.store.get(choice.clone_id)["name"]
        except (ValueError, OSError, KeyError):
            display_voice = "参考音色缺失，请重新选择"
    asr_ready = (SENSEVOICE_DIR / "model.int8.onnx").is_file() if ASR_PROVIDER == "sensevoice" else (Path(ASR_MODEL) / "model.bin").is_file()
    # Server details are for administrators; everyone else learns only what affects their own use.
    server = {"key_fingerprint": fingerprint(API_KEY), "api_host": urlsplit(DEEPSEEK_URL).hostname,
              "server_pid": os.getpid()} if context.is_admin else {}
    return {"asr_ready": asr_ready, "asr_provider": ASR_PROVIDER, "llm_model": LLM_MODEL,
            "tts_voice": display_voice, "tts_provider": choice.provider, "clone_playback": CLONE_PLAYBACK,
            "tts_rate": TTS_RATE, "tts_pitch": TTS_PITCH, "api_key_configured": bool(API_KEY),
            "voice_protocol": 4, "tools": list(default_registry.tools), "default_city": current_city(),
            "email": context.email, **server}


@app.get("/api/memories")
async def list_memories(request: Request):
    context = require_user(request)
    return {"memories": context.memories.recent()}


@app.delete("/api/memories/{memory_id}")
async def delete_memory(request: Request, memory_id: int):
    context = require_user(request, write=True)
    if context.memories.forget(memory_id) is None:
        raise HTTPException(404, "没有这条记忆")
    context.action_log.record("forget", {"memory_id": memory_id}, "page", "executed")
    return {"ok": True}


@app.post("/api/memories/{memory_id}/restore")
async def restore_memory(request: Request, memory_id: int):
    context = require_user(request, write=True)
    if not context.memories.restore(memory_id):
        raise HTTPException(404, "没有可恢复的记忆")
    context.action_log.record("restore", {"memory_id": memory_id}, "page", "executed")
    return {"ok": True}


@app.get("/api/knowledge")
async def list_documents(request: Request):
    context = require_user(request)
    return {"files": await asyncio.to_thread(context.knowledge.list_files)}


@app.post("/api/knowledge")
async def upload_document(request: Request, file: UploadFile = File()):
    context = require_user(request, write=True)
    # One past the limit is enough to reject it without holding an arbitrarily large upload.
    data = await file.read(MAX_DOCUMENT_BYTES + 1)
    try:
        saved = await asyncio.to_thread(context.knowledge.save, file.filename or "", data)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    context.action_log.record("upload_document", {"name": saved["name"]}, "page", "executed")
    return saved


@app.delete("/api/knowledge/{name:path}")
async def delete_document(request: Request, name: str):
    context = require_user(request, write=True)
    if not await asyncio.to_thread(context.knowledge.delete, name):
        raise HTTPException(404, "没有这个文件")
    context.action_log.record("delete_document", {"name": name}, "page", "executed")
    return {"ok": True}


@app.get("/api/reminders")
async def list_reminders(request: Request):
    context = require_user(request)
    return {"reminders": context.reminders.pending()}


@app.delete("/api/reminders/{reminder_id}")
async def delete_reminder(request: Request, reminder_id: int):
    context = require_user(request, write=True)
    if context.reminders.cancel(reminder_id) is None:
        raise HTTPException(404, "没有这条提醒")
    context.action_log.record("cancel_reminder", {"reminder_id": reminder_id}, "page", "executed")
    return {"ok": True}


@app.post("/api/reminders/{reminder_id}/restore")
async def restore_reminder(request: Request, reminder_id: int):
    context = require_user(request, write=True)
    if not context.reminders.restore(reminder_id):
        raise HTTPException(404, "没有可恢复的提醒")
    context.action_log.record("restore_reminder", {"reminder_id": reminder_id}, "page", "executed")
    return {"ok": True}


@app.get("/api/voices")
async def voices(request: Request):
    context = require_user(request)
    choice = current_voice()
    ready = False
    service = {}
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            result = await client.get(COSYVOICE_URL + "/health")
            service = result.json() if result.status_code == 200 else {}
            ready = service.get("ready", False)
    except (httpx.HTTPError, ValueError):
        pass
    return {"profiles":context.voices.list(), "provider":choice.provider, "voice":choice.voice,
            "profile_id":choice.clone_id, "clone_service_ready":ready,
            "clone_service_busy":service.get("busy", False), "clone_device":service.get("device", "unknown")}


@app.post("/api/voices")
async def upload_voice(request: Request, name: str = Form(), prompt_text: str = Form(), audio: UploadFile = File()):
    context = require_user(request, write=True)
    data = await audio.read(10 * 1024 * 1024 + 1)
    await audio.close()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "参考录音不可超过10MB")
    try:
        profile = await asyncio.to_thread(context.voices.add, name, prompt_text, data)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    problem = await asyncio.to_thread(reference_mismatch, context.voices.get(profile["id"]))
    if problem:
        context.voices.remove(profile["id"])
        raise HTTPException(400, problem)
    return profile


# CosyVoice aligns the reference audio to its transcript. A 16.6 s clip saved with a 4-character transcript
# produced clone speech that read back as unrelated words, so a transcript that does not match is refused.
REFERENCE_MIN_SIMILARITY = .5


def reference_mismatch(profile):
    """Return why the transcript does not match the reference audio, or "" when it does or cannot be checked."""
    with wave.open(str(profile["wav_path"]), "rb") as reference:
        pcm = reference.readframes(reference.getnframes())
    try:
        heard = transcribe(pcm[:MAX_BYTES - MAX_BYTES % 2])
    except Exception:
        latency_log.exception("could not check a reference transcript")
        return ""
    heard_words, given = re.sub(r"[\W_]+", "", heard), re.sub(r"[\W_]+", "", profile["prompt_text"])
    if not heard_words:
        return ""
    if SequenceMatcher(None, heard_words, given, autojunk=False).ratio() >= REFERENCE_MIN_SIMILARITY:
        return ""
    return f"填写的文字和录音内容对不上，克隆出来的声音会念错。录音里识别到的是：{heard[:120]}。请照录音实际说的话填写。"


class VoiceSelection(BaseModel):
    provider: str
    voice: str = "zh-CN-XiaoxiaoNeural"
    profile_id: str = ""


@app.post("/api/voice/select")
async def select_voice(request: Request, selection: VoiceSelection):
    context = require_user(request, write=True)
    if selection.provider not in {"edge", "windows", "cosyvoice"}:
        raise HTTPException(400, "未知语音引擎")
    if not 1 <= len(selection.voice) <= 120:
        raise HTTPException(400, "音色名称长度无效")
    if selection.provider == "cosyvoice":
        try:
            # Only the user's own uploads: another user's profile id is not found here.
            context.voices.get(selection.profile_id)
        except (ValueError, OSError) as error:
            raise HTTPException(400, "请先上传并选择参考音色") from error
    # A personal setting: choosing a voice no longer changes it for everyone.
    context.save_settings(selection.model_dump())
    return {"ok":True}


@app.post("/api/mic-test")
async def mic_test(request: Request):
    context = require_user(request, write=True)
    pcm = await request.body()
    if len(pcm) > MAX_BYTES or len(pcm) % 2:
        raise HTTPException(400, "录音长度无效")
    return await asyncio.to_thread(analyse_microphone, pcm)


@app.post("/api/voice/preview")
async def preview_voice(request: Request):
    context = require_user(request, write=True)
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
    # No cross-origin microphone/audio ingestion, and no socket without a signed-in user.
    if ws.headers.get("origin") not in ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    context = session_user(ws)
    await ws.accept()
    if context is None:
        # A custom code the page can tell apart from a dropped connection, so it shows the sign-in page.
        await ws.close(code=4401)
        return
    # Everything this socket starts, replies, reminders and tools alike, inherits this user.
    user_context.CURRENT.set(context)
    watcher = None
    history = Conversation()
    finished_actions = []
    claimed = {}
    task = None
    pcm = bytearray()
    recording = False
    detector = None
    hear_queue = asyncio.Queue()
    hear_task = None
    playing_text = recent_text = ""
    paused = interrupted = False
    request_id = 0
    confirmation = None
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

    def can_interrupt_nobody():
        speaking = detector is not None and getattr(detector, "audio", None) is not None
        return not (reply_active() or recording or speaking or claimed)

    async def reminder_watcher():
        # Only an open page can speak, so a reminder stays pending until one is connected and idle.
        while True:
            await asyncio.sleep(REMINDER_POLL_SECONDS)
            try:
                if can_interrupt_nobody() and (due := context.reminders.claim_due()):
                    claimed[due["id"]] = due
                    await send("reminder_due", None, reminder_id=due["id"])
            except Exception:
                latency_log.exception("reminder check failed")

    async def announce(rid, reminder):
        text = agent_tools.reminder_speech(reminder)
        context.reminders.mark_delivered(reminder["id"])
        await send("token", rid, text=text)
        failed = False
        try:
            async with contextlib.aclosing(synthesize_parts(text)) as parts:
                async for audio in parts:
                    await send_audio(rid, audio, text)
        except Exception as error:
            failed = True
            reason = str(error) if isinstance(error, (ValueError, RuntimeError)) else "请检查网络及音色配置"
            await send("warning", rid, text=f"提醒播报失败：{reason}；请看文字")
        await send("done", rid, tts_failed=failed)

    async def voice(event, **data):
        await send("voice", None, event=event, **data)

    async def on_turn(event, audio):
        nonlocal paused
        if event == "meter":
            await voice("meter", chance=round(audio, 2))
            return
        if event == "sustained":
            # Confirm with Whisper while paused, so a cough costs a moment, not the reply.
            # A confirmation question must finish playing, so it is never paused.
            if playing_text and not confirmation:
                paused = True
                await voice("pause")
                hear_queue.put_nowait((audio, True, 0))
            return
        if event == "end":
            hear_queue.put_nowait((audio, False, time.monotonic() - len(audio) / PCM_BYTES_PER_SECOND))
            event = "transcribing"
        await voice(event)

    async def hear_worker():
        # One worker, and every failure counts as hearing nothing: listening cannot get stuck.
        nonlocal paused, interrupted
        while True:
            audio, partial, started = await hear_queue.get()
            if partial and confirmation:
                if paused:
                    paused = False
                    await voice("resume")
                continue
            if partial and not hear_queue.empty():
                continue
            if not partial and default_registry.get("demo_confirm"):
                keep_demo_audio(audio)
            try:
                heard = await asyncio.wait_for(asyncio.to_thread(transcribe, audio), ASR_TIMEOUT_SECONDS)
            except ValueError:
                heard = ""
            except Exception:
                latency_log.exception("hands-free transcription failed")
                heard = ""
            if confirmation and not partial:
                action, reason = confirmation_action(heard, recent_text, confirmation.accepts(started))
                # Only with the demo on: real use should not keep what people say here.
                if default_registry.get("demo_confirm"):
                    context.action_log.record("confirm_heard", {}, "listen", action, heard)
                if action == "ignored":
                    await voice("ignored", reason=reason)
                elif action == "question":
                    # The page starts a new turn, which cancels the reply and so the confirmation.
                    await voice("question", text=heard)
                else:
                    confirmation.answer(action == "approve", "voice:" + heard)
                continue
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

    async def reply(rid, text=None, audio=None, source="text"):
        worker = None
        # Read once per reply, so a voice change applies from the next reply, never halfway through one.
        voice = current_voice()
        tts_failed = False
        started = time.perf_counter()
        first_audio = True
        first_token = True

        timings = {}

        def mark(stage):
            seconds = time.perf_counter() - started
            timings.setdefault(stage, round(seconds, 2))
            latency_log.info("voice_latency request=%s stage=%s seconds=%.2f", rid, stage, seconds)

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
                    if isinstance(sentence, asyncio.Event):
                        sentence.set()
                        continue
                    if failed:
                        continue
                    try:
                        label = "正在合成语音…"
                        if voice.provider == "cosyvoice":
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
                        await send("warning", rid, text=f"{voice.provider} 播报失败：{reason}；文字回复仍可使用")

            worker = asyncio.create_task(speak_worker())
            # Bound all balanced-mode chunks, including the remainder after the lead.
            clone_full_reply = voice.provider == "cosyvoice"
            pending = ""
            filler_played = False

            async def on_llm_event(event):
                nonlocal pending, first_token, filler_played
                if isinstance(event, ToolResult):
                    mark("tool_done")
                    if event.result.get("undo") or event.result.get("changed"):
                        await send("memory_changed", rid, text=event.result.get("say", ""), undo=event.result.get("undo"))
                    return
                if isinstance(event, ToolCallStarted):
                    mark("tool_start")
                    tool = default_registry.get(event.name)
                    await send("status", rid, text=tool.status if tool else "正在处理…")
                    # Text already spoken covers the wait, and two parallel calls need one filler.
                    if tool and tool.filler and first_token and not filler_played:
                        filler_played = True
                        audio = fillers.take((voice.provider, voice.voice, voice.clone_id), synthesize)
                        if audio:
                            mark("filler")
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

            async def confirm(tool, arguments):
                nonlocal pending, confirmation
                prompt = tool.confirm_text + CONFIRM_SUFFIX
                if pending.strip():
                    await queue.put(pending)
                    pending = ""
                current = confirmation = PendingConfirmation()
                sent = asyncio.Event()
                try:
                    await send("token", rid, text=prompt)
                    await queue.put(prompt)
                    await queue.put(sent)
                    await sent.wait()
                    await send("confirm_request", rid, text=prompt)
                    try:
                        await asyncio.wait_for(current.spoken.wait(), CONFIRM_READY_SECONDS)
                        current.armed_at = time.monotonic()
                        approved, detail = await asyncio.wait_for(asyncio.shield(current.result), CONFIRM_SECONDS)
                    except TimeoutError:
                        approved, detail = False, "timeout"
                finally:
                    confirmation = None
                await send("confirm_done", rid, approved=approved)
                return approved, detail

            def audit(tool, arguments, outcome, detail):
                context.action_log.record(tool, arguments, source, outcome, detail)
                # Kept until a reply finishes, so a turn cut off after a write still reaches the model.
                if outcome == "executed":
                    finished_actions.append(f"{tool} {json.dumps(arguments, ensure_ascii=False)}")

            def chat(messages, **options):
                return stream_chat(DEEPSEEK_URL, API_KEY, LLM_MODEL, messages, **options)

            user_message = {"role": "user", "content": text}
            system_message = {"role": "system", "content": build_system_prompt(
                SYSTEM, current_city(), context.memories.recent(), finished_actions, context.reminders.pending())}
            demo = default_registry.get("demo_confirm") and re.sub(r"[\W_]+", "", text) == DEMO_PHRASE
            added = await run_turn([system_message, *history.messages(), user_message], default_registry,
                                   demo_chat if demo else chat, on_llm_event, confirm, audit)
            mark("llm_done")
            if pending.strip():
                await queue.put(pending)
            await queue.put(None)
            await worker
            mark("all_audio_sent")
            history.add_turn([user_message, *without_outside_results(added, default_registry)])
            finished_actions.clear()
            await send("done", rid, tts_failed=tts_failed)
            record_latency(timings, len(added[-1].get("content") or ""), voice.provider)
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
        watcher = asyncio.create_task(reminder_watcher())
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
            if kind == "confirm_ready" and confirmation and rid == request_id:
                confirmation.spoken.set()
            elif kind == "confirm_reply" and confirmation and rid == request_id:
                confirmation.answer(data.get("approved") is True, "button")
            elif kind == "announce" and rid == request_id and data.get("reminder_id") in claimed:
                await stop()
                task = asyncio.create_task(announce(rid, claimed.pop(data["reminder_id"])))
            elif kind in {"interrupt", "text", "audio_start"}:
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
                        source = "listen" if data.get("source") == "listen" else "text"
                        task = asyncio.create_task(reply(rid, text=text, source=source))
            elif kind == "audio_end" and recording and rid == request_id:
                recording = False
                task = asyncio.create_task(reply(rid, audio=bytes(pcm), source="audio"))
                pcm.clear()
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        if watcher:
            watcher.cancel()
        if hear_task:
            hear_task.cancel()
        await stop()
