"""Local ASR + DeepSeek text chat + configurable TTS. Run on loopback only."""
import asyncio
from datetime import datetime
import base64
import contextlib
from difflib import SequenceMatcher
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import subprocess
import tempfile
import time
import wave

import av
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
import llm_client
from llm_client import ToolCallStarted, stream_chat  # noqa: F401  (stream_chat: kept for scripts importing it here)
import llm_router
from accounts import AccountStore
import db
from audio_codec import OpusDecoder, to_ogg_opus
from memory_store import ITEM_KINDS, REPEATS, ActionLog, LookupCache  # noqa: F401  (tests build per-user logs through app)
import tools as agent_tools
from tools import default_registry
import agents
import audio_content
import meeting
import ops
import practice
import quiz
import pronunciation
import usage
import user_context
from urllib.parse import urlsplit
from windows_asyncio import disconnect_logging
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parent
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
API_KEY = normalize_key(os.getenv("DEEPSEEK_API_KEY", ""))
# A backup model service, tried when DeepSeek fails before its first word (OpenAI-compatible chat API). Unset: no backup.
BACKUP_URL = os.getenv("VOICE_LLM_BACKUP_BASE_URL", "").strip().rstrip("/")
BACKUP_KEY = normalize_key(os.getenv("VOICE_LLM_BACKUP_API_KEY", ""))
BACKUP_MODEL = os.getenv("VOICE_LLM_BACKUP_MODEL", "").strip()
# The backup's own request fields as JSON, e.g. {"thinking": {"type": "disabled"}} for a DeepSeek model on another host.
BACKUP_EXTRA_BODY = json.loads(os.getenv("VOICE_LLM_BACKUP_EXTRA_BODY", "") or "{}")
# Its price in yuan per million tokens, {"hit": .., "miss": .., "output": ..}; without it its calls show as unpriced.
if BACKUP_MODEL and os.getenv("VOICE_LLM_BACKUP_PRICE", "").strip():
    usage.set_flat_price(BACKUP_MODEL, json.loads(os.environ["VOICE_LLM_BACKUP_PRICE"]))
DEFAULT_CITY = os.getenv("VOICE_DEFAULT_CITY", "合肥").strip()
fillers = FillerCache()
accounts = AccountStore(db.target(ROOT / "data" / "accounts.db"))
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
def llm_providers():
    """DeepSeek first, then the backup if configured; read per call so configuration (and tests) can change it."""
    return [llm_router.Provider("deepseek", DEEPSEEK_URL, API_KEY, LLM_MODEL, llm_client.DEEPSEEK_OPTIONS),
            llm_router.Provider("backup", BACKUP_URL, BACKUP_KEY, BACKUP_MODEL, BACKUP_EXTRA_BODY)]


llm = llm_router.Router(llm_providers, lambda: metrics)


def agent_chat(model, messages, on_usage=None, **options):
    """How background agents reach the model: they are not listened to live, so the primary gets longer before the
    backup is tried. on_usage(usage, model) learns which model answered."""
    return llm.chat(messages, llm_router.BACKGROUND, primary_model=model,
                    on_usage=(lambda provider, used: on_usage(used, provider.model)) if on_usage else None, **options)


task_runner = agents.TaskRunner(agent_chat, lambda: LLM_MODEL)
NO_TOOLS = agent_tools.ToolRegistry([])


@contextlib.asynccontextmanager
async def lifespan(application):
    # PostgreSQL: create or update tables and row-level security before the first request; a bad role stops startup.
    if db.configured():
        await asyncio.to_thread(db.migrate)
    with disconnect_logging(asyncio.get_running_loop()):
        yield
        await task_runner.shutdown()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def count_requests(request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    metrics.count(f"http_{response.status_code // 100}xx")
    if request.url.path.startswith("/api/"):
        metrics.observe("http_api", time.perf_counter() - started)
    return response
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
asr_model = None
sense_models = {}
# Cancellation cannot stop CPU inference. A real thread lock prevents overlapping inference.
import threading
asr_lock = threading.Lock()  # Whisper only: one at a time
model_lock = threading.Lock()  # loading a recognizer once, however many requests arrive together
# How the service is doing, and what each account may do how often (ops.py).
limiter = ops.RateLimiter()
metrics = ops.Metrics()
recognition_gate = ops.RecognitionGate(ops.recognition_slots())


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


PRACTICE_VOICE = VoiceChoice("edge", practice.ENGLISH_VOICE, "", voice_store)


def current_city():
    context = user_context.CURRENT.get(None)
    saved = context.settings() if context else {}
    return saved["default_city"] if "default_city" in saved else DEFAULT_CITY


def record_latency(timings, reply_chars, provider=""):
    """Stage timings for finding what is slow. Holds seconds and a length, never what was said."""
    for stage, seconds in timings.items():
        metrics.observe("reply_" + stage, seconds)
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


def transcribe(pcm: bytes, language: str = "zh") -> str:
    """Speech to text; language is "zh" for everything except speaking practice, which passes "en"."""
    if len(pcm) % 2 or not 6400 <= len(pcm) <= MAX_BYTES:
        raise ValueError("录音应为 0.2～30 秒的 16kHz 单声道 PCM16")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    with recognition_gate as gate:
        metrics.observe("asr_wait", gate.waited)
        started = time.perf_counter()
        if ASR_PROVIDER == "sensevoice":
            text = transcribe_sensevoice(pcm, samples, language)
        else:
            with asr_lock:
                text = transcribe_whisper(samples, language)
        metrics.observe("asr", time.perf_counter() - started)
    # Traditional output would never match the simplified reply when filtering out the speakers.
    return to_chinese_variant(text, "zh-cn") if language == "zh" else text


meeting.TRANSCRIBE = lambda pcm: transcribe(pcm)  # looked up per call, so tests can swap it


def transcribe_sensevoice(pcm, samples, language="zh"):
    """SenseVoice Small via sherpa-onnx. On the webcam microphone it read 24/24 band-limited test phrases
    where Whisper base read 14/24, in a quarter of the time.

    It never returns nothing: silence, hum and clicks all came back as 我. Silero peaked at 0.11 or less on
    those and 0.96 or more on every real recording, so a clip below SPEECH_PEAK_MIN is treated as silence.
    """
    if not (SENSEVOICE_DIR / "model.int8.onnx").is_file():
        raise ValueError("尚未准备 SenseVoice 模型，请运行 prepare_asr.py --sensevoice")
    if measure_speech(pcm)[0] < SPEECH_PEAK_MIN:
        return ""
    with model_lock:
        if language not in sense_models:
            sense_models[language] = load_sensevoice(language)
    sense_model = sense_models[language]
    stream = sense_model.create_stream()
    stream.accept_waveform(16000, samples)
    sense_model.decode_stream(stream)
    return stream.result.text.strip()


def load_sensevoice(language):
    """One recognizer per language, shared by every request (decoding several streams at once is safe)."""
    import sherpa_onnx
    # Fixed per language: automatic detection read short Chinese replies as Japanese. English keeps words
    # as said (no "3" for "three"), which read-aloud checking compares against.
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(SENSEVOICE_DIR / "model.int8.onnx"), tokens=str(SENSEVOICE_DIR / "tokens.txt"),
        num_threads=2, language=language, use_itn=language == "zh")


def transcribe_whisper(samples, language="zh"):
    global asr_model
    if not Path(ASR_MODEL).is_dir():
        raise ValueError("尚未准备识别模型，请先运行 prepare_asr.py")
    if asr_model is None:
        from faster_whisper import WhisperModel
        asr_model = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8", local_files_only=True)
    # Default retries at rising temperatures took 12 s on a 0.8 s clip of leaked playback,
    # holding asr_lock so every later sentence queued behind it. Decode once, bounded.
    segments, _ = asr_model.transcribe(samples, language=language, beam_size=3, vad_filter=True,
                                       condition_on_previous_text=False, initial_prompt=SPEECH_PROMPT if language == "zh" else None,
                                       temperature=0, max_new_tokens=300, no_repeat_ngram_size=3)
    text = "".join(segment.text for segment in segments if segment.no_speech_prob < NO_SPEECH_LIMIT).strip()
    if language != "zh":
        return text
    # On unclear audio Whisper repeats all or part of its prompt instead of returning nothing.
    words, prompt = re.sub(r"[\W_]+", "", to_chinese_variant(text, "zh-cn")), re.sub(r"[\W_]+", "", SPEECH_PROMPT)
    return "" if words in prompt or prompt in words else text


def no_speech_message(pcm: bytes):
    """Why a recording gave no text, in numbers, so a weak or wrong microphone can be told from a quiet speaker."""
    seconds = len(pcm) / PCM_BYTES_PER_SECOND
    if seconds < .3:
        return f"录音只有 {seconds:.1f} 秒，按住说完整句再松开"
    peak, _ = measure_speech(pcm)
    samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
    rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    if peak < SPEECH_PEAK_MIN:
        hint = "几乎没有声音，请检查选的麦克风是否正确" if rms < .002 else "有声音但不像人声（噪声或音量太小），请靠近麦克风或在“麦克风设置”里换一个"
        return f"没听到人声（{seconds:.1f} 秒，语音概率 {peak:.2f}，需要 {SPEECH_PEAK_MIN:.2f} 以上；音量 {rms:.4f}）：{hint}"
    return f"听到了人声但没识别出文字（语音概率 {peak:.2f}，音量 {rms:.4f}），请说慢一点、清楚一点再试"


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


async def synthesize(text: str, choice=None) -> bytes:
    choice = choice or current_voice()
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


# Looked up per call so tests can replace synthesize; uses the voice of whoever the task belongs to.
audio_content.SYNTHESIZE = lambda text: synthesize(text)


async def synthesize_parts(text, choice=None):
    # Short buffered segments keep up with playback on the GTX 1060; streaming
    # re-runs flow for every chunk, so it stays opt-in.
    explicit, choice = choice, choice or current_voice()
    if choice.provider != "cosyvoice" or CLONE_PLAYBACK != "stream":
        yield await (synthesize(text, explicit) if explicit else synthesize(text))
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
    limit("auth", client_ip(request))
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
    limit("auth", client_ip(request))
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


class PasswordChange(BaseModel):
    current: str
    new: str


@app.post("/api/account/password")
async def change_password(request: Request, body: PasswordChange):
    """Change one's own password; other signed-in devices are signed out, this one stays."""
    context = require_user(request, write=True)
    limit("auth", client_ip(request))
    try:
        changed = await asyncio.to_thread(accounts.change_password, context.user_id, body.current, body.new, request.cookies.get(SESSION_COOKIE))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not changed:
        context.action_log.record("change_password", {}, "page", "refused", "wrong current password")
        raise HTTPException(403, "现在的密码不对")
    context.action_log.record("change_password", {}, "page", "executed")
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    context = require_user(request)
    return {"email": context.email, "is_admin": context.is_admin}


@app.get("/api/usage")
async def spend(request: Request):
    """The signed-in user's own spend; an admin also sees every account's share of this month."""
    context = require_user(request)
    result = await asyncio.to_thread(usage.LEDGER.summary, context.user_id)
    if context.is_admin:
        everyone = await asyncio.to_thread(usage.LEDGER.summary, None)
        emails = await asyncio.to_thread(accounts.emails)
        result["all"] = {**everyone, "users": [{**row, "email": emails.get(row["user_id"], f"#{row['user_id']}")}
                                               for row in everyone["users"]]}
    return result


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


@app.get("/healthz")
async def healthz():
    """For the load balancer and Docker: is the process up, can it write its data and reach its database? Says
    nothing else."""
    try:
        probe = ROOT / "data" / ".healthz"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        return Response(json.dumps({"ok": False}), status_code=503, media_type="application/json")
    if not await asyncio.to_thread(db.healthy):
        return Response(json.dumps({"ok": False}), status_code=503, media_type="application/json")
    return {"ok": True}


@app.get("/api/admin/metrics")
async def admin_metrics(request: Request):
    context = require_user(request)
    if not context.is_admin:
        raise HTTPException(403, "只有管理员能看")
    return {**metrics.snapshot(), "models": llm.status(), "recognition": {"slots": recognition_gate.size, "running": recognition_gate.active,
                                                  "waiting": recognition_gate.waiting}}


def limit(kind, key):
    """Refuse with 429 and a readable wait when this key has done `kind` too often."""
    retry = limiter.hit(kind, key)
    if retry is not None:
        metrics.count("limited_" + kind)
        raise HTTPException(429, limiter.message(kind, retry), headers={"Retry-After": str(retry)})


def client_ip(request):
    return request.client.host if request.client else "unknown"


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
            "voice_protocol": 5, "tools": list(default_registry.tools), "default_city": current_city(),
            "pronunciation": pronunciation.status(), "email": context.email, **server}


class MemoryText(BaseModel):
    content: str


def memory_text(content):
    text = " ".join(content.split())
    if not 1 <= len(text) <= agent_tools.MAX_MEMORY_CHARS:
        raise HTTPException(400, f"记忆要在 1 到 {agent_tools.MAX_MEMORY_CHARS} 字之间")
    return text


@app.post("/api/memories")
async def add_memory(request: Request, body: MemoryText):
    """Add a memory by typing it, as saying "记住……" would."""
    context = require_user(request, write=True)
    text = memory_text(body.content)
    memory_id = await asyncio.to_thread(context.memories.add, text)
    context.action_log.record("remember", {"content": text}, "page", "executed")
    return {"id": memory_id, "content": text}


@app.put("/api/memories/{memory_id}")
async def edit_memory(request: Request, memory_id: int, body: MemoryText):
    context = require_user(request, write=True)
    text = memory_text(body.content)
    if not await asyncio.to_thread(context.memories.update, memory_id, text):
        raise HTTPException(404, "没有这条记忆")
    context.action_log.record("edit_memory", {"memory_id": memory_id, "content": text}, "page", "executed")
    return {"id": memory_id, "content": text}


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
    limit("upload", context.user_id)
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


MEETING_FILE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]+$")


def probe_audio(path):
    """Quick check that an upload is audio of an allowed length, without decoding all of it."""
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise ValueError("文件里没有音频")
            if container.duration and container.duration / 1e6 > meeting.MAX_SECONDS:
                raise ValueError("录音超过 3 小时，请分段上传")
    except av.error.FFmpegError as error:
        raise ValueError("无法读取这个文件，请上传常见的音频格式") from error


@app.post("/api/meetings")
async def upload_meeting(request: Request, file: UploadFile = File(), date: str = Form("")):
    """Save a recording under the user's own folder and start the notes task for it."""
    context = require_user(request, write=True)
    limit("upload", context.user_id)
    if not API_KEY:
        raise HTTPException(400, "请设置 DEEPSEEK_API_KEY 环境变量后重启后端")
    original = Path(file.filename or "录音").name
    suffix = Path(original).suffix.lower()
    if suffix not in meeting.EXTENSIONS:
        raise HTTPException(400, "支持 " + "、".join(sorted(ext[1:] for ext in meeting.EXTENSIONS)))
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(400, "会议日期格式应为 YYYY-MM-DD")
    folder = context.root / "meetings"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (os.urandom(16).hex() + suffix)
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > meeting.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "文件超过 200 MB")
                output.write(chunk)
        await asyncio.to_thread(probe_audio, target)
        task_id = task_runner.submit(context, "meeting_notes", Path(original).stem, {"file": target.name, "name": original, "date": date})
    except ValueError as error:
        target.unlink(missing_ok=True)
        raise HTTPException(400, str(error)) from error
    except agents.AgentStopped as error:
        target.unlink(missing_ok=True)
        raise HTTPException(429, str(error)) from error
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    context.action_log.record("upload_meeting", {"name": original}, "page", "executed")
    return {"id": task_id}


@app.delete("/api/meetings/{name}")
async def delete_meeting_audio(request: Request, name: str):
    context = require_user(request, write=True)
    path = context.root / "meetings" / name
    if not MEETING_FILE.match(name) or not path.is_file():
        raise HTTPException(404, "没有这个录音")
    path.unlink()
    context.action_log.record("delete_meeting_audio", {"name": name}, "page", "executed")
    return {"ok": True}


class MeetingTodo(BaseModel):
    task: str
    due_at: str


class MeetingTodos(BaseModel):
    items: list[MeetingTodo]


@app.post("/api/meetings/reminders")
async def meeting_reminders(request: Request, body: MeetingTodos):
    """Create the reminders the user ticked. Each one is checked like the voice tool checks its own."""
    context = require_user(request, write=True)
    created, problems = [], []
    for item in body.items[:50]:
        try:
            result = await agent_tools.set_reminder(item.task, item.due_at)
        except agent_tools.ToolError as error:
            problems.append(f"{item.task[:20]}：{error}")
            continue
        context.action_log.record("set_reminder", {"content": item.task, "due_at": item.due_at}, "meeting", "executed")
        created.append(result["undo"]["id"])
    return {"created": created, "problems": problems}


class MeetingArchive(BaseModel):
    task_id: int


@app.post("/api/meetings/archive")
async def archive_meeting(request: Request, body: MeetingArchive):
    """Save finished notes, to-dos and transcript as a Markdown file in the user's knowledge base."""
    context = require_user(request, write=True)
    task = await asyncio.to_thread(context.tasks.get, body.task_id)
    if not task or task["kind"] != "meeting_notes" or task["status"] != "done":
        raise HTTPException(404, "没有可存档的会议纪要")
    result = task["result"]
    todos = "\n".join(f"- {item['task']}" + (f"（{item['owner']}）" if item["owner"] else "") +
                      (f" 截止：{item['due_text'] or item['due_at']}" if item["due_text"] or item["due_at"] else "")
                      for item in result["proposals"]) or "无"
    day = (await asyncio.to_thread(context.tasks.payload, body.task_id) or {}).get("date") or task["created_at"][:10]
    text = (f"# 会议纪要：{task['title']}\n\n日期：{day}\n\n{result['text']}\n\n## 待办\n\n{todos}\n\n"
            f"## 逐字稿\n\n{meeting.transcript_text(result['transcript'])}\n")
    name = re.sub(r'[\\/:*?"<>|]', "_", f"会议纪要-{task['title']}-{day}.md")
    try:
        saved = await asyncio.to_thread(context.knowledge.save, name, text.encode("utf-8"))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    context.action_log.record("upload_document", {"name": saved["name"]}, "meeting", "executed")
    return saved


class AudioScriptRequest(BaseModel):
    topic: str
    minutes: int = 3
    style: str = ""
    search: bool = False
    render: bool = False


class AudioRenderRequest(BaseModel):
    text: str
    title: str = ""


def numpy_concat(parts):
    return np.concatenate(parts) if parts else np.zeros(0, "<i2")


class TurnDetectorChance:
    """Silero speech probability per frame, fresh state for each recording."""

    def __init__(self):
        from turn_detector import SileroStream
        self.stream = SileroStream()

    def __call__(self, frame):
        return self.stream(frame)


def submit_or_refuse(context, kind, title, payload):
    if not API_KEY and kind != "audio_render":
        raise HTTPException(400, "请设置 DEEPSEEK_API_KEY 环境变量后重启后端")
    try:
        return {"id": task_runner.submit(context, kind, title, payload)}
    except agents.AgentStopped as error:
        raise HTTPException(429, str(error)) from error


@app.post("/api/audio/scripts")
async def start_audio_script(request: Request, body: AudioScriptRequest):
    """Writer and editor agents draft a script; with render it goes straight on to audio."""
    context = require_user(request, write=True)
    limit("task", context.user_id)
    topic = body.topic.strip()
    if not 2 <= len(topic) <= 500:
        raise HTTPException(400, "主题要在 2 到 500 字之间")
    if body.minutes not in audio_content.MINUTES:
        raise HTTPException(400, "时长只能选 1、3、5、10 分钟")
    payload = {"topic": topic, "minutes": body.minutes, "style": body.style.strip()[:30], "search": body.search, "render": body.render}
    return submit_or_refuse(context, "audio_script", topic[:30], payload)


@app.post("/api/audio/render")
async def start_audio_render(request: Request, body: AudioRenderRequest):
    """Voice a script the user has read and perhaps edited. No model is called, so no key is needed."""
    context = require_user(request, write=True)
    limit("task", context.user_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "稿子是空的")
    if len(text) > audio_content.MAX_CHARS:
        raise HTTPException(400, f"稿子超过 {audio_content.MAX_CHARS} 字（约 10 分钟），请删减")
    title = body.title.strip()[:30] or text[:20]
    return submit_or_refuse(context, "audio_render", "音频：" + title, {"text": text, "title": title})


def audio_path(context, name):
    path = context.root / "audio" / name
    if not audio_content.AUDIO_NAME.match(name) or not path.is_file():
        raise HTTPException(404, "没有这个音频")
    return path


@app.get("/api/audio/{name}")
async def get_audio(request: Request, name: str):
    context = require_user(request)
    return FileResponse(audio_path(context, name), media_type="audio/ogg", filename="有声内容.ogg", content_disposition_type="inline")


@app.delete("/api/audio/{name}")
async def delete_audio(request: Request, name: str):
    context = require_user(request, write=True)
    audio_path(context, name).unlink()
    context.action_log.record("delete_audio", {"name": name}, "page", "executed")
    return {"ok": True}


@app.get("/api/practice")
async def practice_overview(request: Request):
    """Levels, the learner's last level, recent conversations, and how many items each book holds."""
    context = require_user(request)
    sessions = await asyncio.to_thread(context.english.sessions, 10)
    return {"levels": [{"id": key, "title": title} for key, (title, _) in practice.LEVELS.items()],
            "level": context.settings().get("english_level", "intermediate"),
            "sessions": [{"id": item["id"], "level": practice.LEVELS[item["level"]][0], "topic": item["topic"],
                          "started_at": item["started_at"], "turns": sum(turn["who"] == "learner" for turn in item["transcript"]),
                          "notes": len(item["notes"]), "has_report": bool(item["report"]), "ended": bool(item["ended_at"])}
                         for item in sessions],
            "counts": await asyncio.to_thread(context.english.counts)}


@app.get("/api/practice/sessions/{session_id}")
async def practice_session_detail(request: Request, session_id: int):
    """One conversation in full: every turn, its corrections, and the report once written."""
    context = require_user(request)
    session = await asyncio.to_thread(context.english.session, session_id)
    if not session:
        raise HTTPException(404, "没有这次对话")
    return {**session, "level_title": practice.LEVELS[session["level"]][0]}


@app.get("/api/practice/sessions/{session_id}/audio/{turn}")
async def practice_turn_audio(request: Request, session_id: int, turn: int):
    """One turn's recording: what the learner said, or what the partner said back."""
    context = require_user(request)
    session = await asyncio.to_thread(context.english.session, session_id)
    if not session or not 0 <= turn < len(session["transcript"]) or not session["transcript"][turn].get("audio"):
        raise HTTPException(404, "这一句没有录音")
    path = context.root / "english_audio" / Path(session["transcript"][turn]["audio"]).name
    if not path.is_file():
        raise HTTPException(404, "这一句的录音已不在了")
    return FileResponse(path, media_type="audio/ogg")


class SayRequest(BaseModel):
    text: str
    voice: str = "standard"


def own_clone(context):
    """The learner's own cloned voice: the one in use if it is a clone, otherwise the first one uploaded."""
    choice = current_voice()
    if choice.provider == "cosyvoice" and choice.clone_id:
        return choice.clone_id
    profiles = context.voices.list()
    return profiles[0]["id"] if profiles else None


@app.post("/api/practice/say")
async def practice_say(request: Request, body: SayRequest):
    """An English sentence read aloud, to compare with how the learner said it: the standard English voice
    (edge-tts, free, sends the text to Microsoft) or the learner's own cloned voice (CosyVoice on this machine).
    The same sentence in the same voice is made once and kept, so replaying it costs nothing."""
    context = require_user(request, write=True)
    limit("lookup", context.user_id)
    text = " ".join(body.text.split())
    if not 1 <= len(text) <= practice.MAX_SAY_CHARS or not practice.words(text):
        raise HTTPException(400, f"要读的句子需要是 1 到 {practice.MAX_SAY_CHARS} 字的英文")
    if body.voice not in ("standard", "mine"):
        raise HTTPException(400, "音色只能是标准读法或我的声音")
    profile_id = own_clone(context) if body.voice == "mine" else None
    if body.voice == "mine" and not profile_id:
        raise HTTPException(400, "还没有自己的音色：先在“音色”页上传一段自己的录音")
    folder = context.root / "english_audio" / "say"
    path = folder / f"{body.voice}-{hashlib.sha1(((profile_id or '') + '|' + text).encode('utf-8')).hexdigest()}.ogg"
    if not path.is_file():
        try:
            if body.voice == "standard":
                audio = await synthesize(text, PRACTICE_VOICE)
            else:
                audio = await synthesize_clone_when_free(context.voices.get(profile_id), text)
        except CloneBusy as error:
            raise HTTPException(429, "克隆服务正在合成别的内容，请稍后再点") from error
        except (httpx.HTTPError, OSError) as error:
            raise HTTPException(503, "无法连接本机 CosyVoice 服务，请先运行 start_cosyvoice.ps1") from error
        except (RuntimeError, ValueError) as error:
            raise HTTPException(502, str(error)) from error
        samples = await asyncio.to_thread(audio_content.to_samples, audio)
        folder.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, await asyncio.to_thread(to_ogg_opus, audio_content.wav_bytes(samples)))
    return FileResponse(path, media_type="audio/ogg")


class TranslateRequest(BaseModel):
    text: str


class ExplainRequest(BaseModel):
    text: str
    context: str = ""


def english_text(text, limit):
    text = " ".join(text.split())
    if not 1 <= len(text) <= limit or not practice.words(text):
        raise HTTPException(400, f"需要是 1 到 {limit} 字的英文")
    return text


async def ask_agent(context, spec, material, task):
    """One small agent call for the page; the daily spending cap applies like any background agent."""
    if not API_KEY:
        raise HTTPException(400, "请设置 DEEPSEEK_API_KEY 环境变量后重启后端")
    try:
        return await agents.run_agent(spec, material, agent_chat, context.user_id, LLM_MODEL, task=task)
    except agents.AgentStopped as error:
        raise HTTPException(429, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(502, str(error)) from error


@app.post("/api/english/translate")
async def english_translate(request: Request, body: TranslateRequest):
    """A Chinese translation of one sentence, made when first asked for and kept."""
    context = require_user(request, write=True)
    limit("lookup", context.user_id)
    text = english_text(body.text, practice.MAX_SAY_CHARS)
    cache, key = LookupCache(db.target(context.root / "agent.db", context.user_id)), "zh:" + hashlib.sha1(text.encode("utf-8")).hexdigest()
    if (saved := await asyncio.to_thread(cache.get, key)) is not None:
        return {"text": text, "zh": saved}
    zh = (await ask_agent(context, practice.TRANSLATOR, text, "请翻译：")).strip()
    if not zh:
        raise HTTPException(502, "没有得到翻译，请再试一次")
    await asyncio.to_thread(cache.set, key, zh)
    return {"text": text, "zh": zh}


@app.post("/api/english/explain")
async def english_explain(request: Request, body: ExplainRequest):
    """What a selected word, phrase or sentence means where it was used, how to say it, and an example.

    Looking something up means it was not understood, so it goes into the vocabulary book (once; looking it
    up again just counts it again)."""
    context = require_user(request, write=True)
    limit("lookup", context.user_id)
    text = english_text(body.text, practice.MAX_LOOKUP_CHARS)
    around = " ".join(body.context.split())[:500]
    cache = LookupCache(db.target(context.root / "agent.db", context.user_id))
    key = "explain:" + hashlib.sha1(f"{text.lower()}|{around.lower()}".encode("utf-8")).hexdigest()
    result = await asyncio.to_thread(cache.get, key)
    if result is None:
        answer = await ask_agent(context, practice.EXPLAINER, f"选中的内容：{text}\n所在的句子：{around or text}", "请解释选中的内容。")
        result = practice.parse_explanation(answer)
        if not result:
            raise HTTPException(502, "没有得到解释，请再试一次")
        await asyncio.to_thread(cache.set, key, result)
    kind = practice.lookup_kind(text)
    item_id = await asyncio.to_thread(context.english.add_item, kind, text, result["meaning"], phonetic=result["phonetic"],
                                      note=result["note"], context=around)
    return {"text": text, "kind": kind, "item_id": item_id, **result}


async def quiet_agent(context, spec, material, task):
    """An agent call whose failure just means falling back (no key, over budget, network): returns "" then."""
    if not API_KEY:
        return ""
    try:
        return await agents.run_agent(spec, material, agent_chat, context.user_id, LLM_MODEL, task=task)
    except Exception:
        return ""


@app.get("/api/english/review")
async def english_review(request: Request):
    """How many items are due now, when the next one is, and recent quiz scores."""
    context = require_user(request)
    due, upcoming = await asyncio.to_thread(context.english.due_count)
    return {"due": due, "next_due_at": upcoming, "quizzes": await asyncio.to_thread(context.english.quizzes, 5)}


@app.post("/api/english/quizzes")
async def english_quiz_start(request: Request):
    """A quiz from what is due: mistakes first, then whatever has waited longest, ten at most."""
    context = require_user(request, write=True)
    limit("task", context.user_id)
    items = await asyncio.to_thread(context.english.due, quiz.MAX_QUESTIONS)
    if not items:
        due, upcoming = await asyncio.to_thread(context.english.due_count)
        raise HTTPException(400, "现在没有要复习的内容" + (f"，下一个在 {upcoming[:16].replace('T', ' ')}" if upcoming else "：先在对话里积累错题和生词"))
    wanted = [item for item in items if quiz.plan(item) == "choice"]
    distractors = {}
    if wanted:
        distractors = quiz.parse_distractors(await quiet_agent(context, quiz.DISTRACTORS, quiz.distractor_material(wanted), "请出干扰项："))
        # Without the model, wrong options can come from the learner's other items.
        others = list({item["meaning"] for item in await asyncio.to_thread(context.english.items, None, True, 200) if item["meaning"] and item["kind"] != "mistake"})
        for item in wanted:
            if item["id"] not in distractors:
                pool = [meaning for meaning in others if meaning != item["meaning"]]
                if len(pool) >= 3:
                    distractors[item["id"]] = random.sample(pool, 3)
    questions = quiz.build(items, distractors)
    quiz_id = await asyncio.to_thread(context.english.create_quiz, questions)
    return {"id": quiz_id, "questions": quiz.for_page(questions)}


@app.get("/api/english/quizzes")
async def english_quiz_history(request: Request):
    """Finished quizzes, newest first, with their scores."""
    context = require_user(request)
    return {"quizzes": await asyncio.to_thread(context.english.quizzes, 30)}


@app.get("/api/english/quizzes/{quiz_id}")
async def english_quiz_detail(request: Request, quiz_id: int):
    """One finished quiz: the questions, what was answered, and how each was graded."""
    context = require_user(request)
    saved = await asyncio.to_thread(context.english.quiz, quiz_id)
    if not saved or not saved["finished_at"]:
        raise HTTPException(404, "没有这次测验")
    return {"id": saved["id"], "created_at": saved["created_at"], "score": saved["score"], "questions": quiz.for_page(saved["questions"]),
            "answers": saved["answers"], "results": saved["results"]}


class QuizAnswers(BaseModel):
    answers: dict


@app.post("/api/english/quizzes/{quiz_id}/submit")
async def english_quiz_submit(request: Request, quiz_id: int, body: QuizAnswers):
    """Grade a quiz once: rules where they decide, the grader agent for translations and corrections, then move
    every item along its review schedule."""
    context = require_user(request, write=True)
    saved = await asyncio.to_thread(context.english.quiz, quiz_id)
    if not saved:
        raise HTTPException(404, "没有这次测验")
    if saved["finished_at"]:
        raise HTTPException(409, "这次测验已经交过了")
    answers = {str(key): value for key, value in body.answers.items()}
    questions = saved["questions"]
    verdicts = {question["id"]: quiz.grade_locally(question, answers.get(str(question["id"]))) for question in questions}
    open_questions = [question for question in questions if verdicts[question["id"]] is None]
    grades = quiz.parse_grades(await quiet_agent(context, quiz.GRADER, quiz.grader_material(open_questions, answers), "请批改：")) if open_questions else {}
    results = []
    for question in questions:
        answer = answers.get(str(question["id"]))
        ok, comment = verdicts[question["id"]], ""
        near = ok == quiz.NEAR
        if near:
            ok, comment = True, f"拼写差一个字母，正确是 {question['expected']}"
        if ok is None:
            grade = grades.get(question["id"])
            ok, comment = (grade["ok"], grade["comment"]) if grade else (quiz.closeness(question["expected"], answer) >= quiz.OPEN_PASS, "")
        review = await asyncio.to_thread(context.english.record_review, question["item_id"], None if near else ok)
        expected = question["options"][question["expected"]] if question["type"] == "choice" else question["expected"]
        results.append({"id": question["id"], "ok": ok, "near": near, "expected": expected, "comment": comment, "review": review})
    score = sum(result["ok"] for result in results)
    if not await asyncio.to_thread(context.english.finish_quiz, quiz_id, answers, results, score):
        raise HTTPException(409, "这次测验已经交过了")
    return {"score": score, "total": len(results), "results": results}


class ChatTitle(BaseModel):
    title: str


@app.get("/api/chats")
async def list_chats(request: Request, q: str = ""):
    """The assistant conversations, newest first; q finds those whose title or messages contain it."""
    context = require_user(request)
    return {"chats": await asyncio.to_thread(context.chats.list, q[:100])}


@app.get("/api/chats/{chat_id}")
async def get_chat(request: Request, chat_id: int):
    context = require_user(request)
    chat = await asyncio.to_thread(context.chats.get, chat_id)
    if not chat:
        raise HTTPException(404, "没有这次对话")
    return chat


@app.post("/api/chats/{chat_id}/title")
async def rename_chat(request: Request, chat_id: int, body: ChatTitle):
    context = require_user(request, write=True)
    if not body.title.strip():
        raise HTTPException(400, "标题不能为空")
    if not await asyncio.to_thread(context.chats.rename, chat_id, body.title):
        raise HTTPException(404, "没有这次对话")
    return {"ok": True}


@app.delete("/api/chats/{chat_id}")
async def delete_chat(request: Request, chat_id: int):
    context = require_user(request, write=True)
    if not await asyncio.to_thread(context.chats.delete, chat_id):
        raise HTTPException(404, "没有这次对话")
    return {"ok": True}


@app.get("/api/english/items")
async def english_items(request: Request, kind: str = "", mastered: bool = False):
    context = require_user(request)
    if kind and kind not in ITEM_KINDS:
        raise HTTPException(400, "没有这种条目")
    return {"items": await asyncio.to_thread(context.english.items, kind or None, mastered)}


@app.post("/api/english/items/{item_id}/mastered")
async def english_item_mastered(request: Request, item_id: int, undo: bool = False):
    context = require_user(request, write=True)
    if not await asyncio.to_thread(context.english.set_mastered, item_id, not undo):
        raise HTTPException(404, "没有这个条目")
    return {"ok": True}


@app.delete("/api/english/items/{item_id}")
async def english_item_delete(request: Request, item_id: int):
    context = require_user(request, write=True)
    if not await asyncio.to_thread(context.english.delete_item, item_id):
        raise HTTPException(404, "没有这个条目")
    return {"ok": True}


@app.post("/api/practice/read")
async def practice_read(request: Request, audio: UploadFile = File(), target: str = Form()):
    """Read-aloud check: what the recognizer heard, word by word against the sentence. Runs locally, costs nothing."""
    context = require_user(request, write=True)
    limit("lookup", context.user_id)
    target = target.strip()
    if not 1 <= len(target) <= 200 or not practice.words(target):
        raise HTTPException(400, "跟读句子要是 1 到 200 字的英文")
    pcm = await audio.read(MAX_BYTES + 1)
    if len(pcm) % 2 or not 16000 * 2 * .3 <= len(pcm) <= 16000 * 2 * 15:
        raise HTTPException(400, "录音应为 0.3～15 秒")
    try:
        heard = await asyncio.wait_for(asyncio.to_thread(transcribe, pcm, "en"), ASR_TIMEOUT_SECONDS)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return practice.read_aloud(target, heard)


@app.post("/api/practice/{session_id}/report")
async def practice_report_again(request: Request, session_id: int):
    """Ask again for a report, for a session whose report could not start when it ended."""
    context = require_user(request, write=True)
    limit("task", context.user_id)
    session = await asyncio.to_thread(context.english.session, session_id)
    if not session or not session["ended_at"] or not session["transcript"]:
        raise HTTPException(404, "没有可总结的对话")
    return submit_or_refuse(context, "practice_report", "英语对话总结", {"session_id": session_id})


class TaskRequest(BaseModel):
    kind: str
    title: str = ""
    input: dict = {}


@app.get("/api/tasks")
async def list_tasks(request: Request):
    context = require_user(request)
    return {"tasks": await asyncio.to_thread(context.tasks.recent), "kinds": sorted(agents.FEATURES)}


@app.post("/api/tasks")
async def submit_task(request: Request, body: TaskRequest):
    context = require_user(request, write=True)
    limit("task", context.user_id)
    if len(json.dumps(body.input, ensure_ascii=False)) > agents.MAX_INPUT_CHARS:
        raise HTTPException(413, "内容太长")
    if not API_KEY:
        raise HTTPException(400, "请设置 DEEPSEEK_API_KEY 环境变量后重启后端")
    try:
        task_id = task_runner.submit(context, body.kind, body.title, body.input)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except agents.AgentStopped as error:
        raise HTTPException(429, str(error)) from error
    return {"id": task_id}


@app.get("/api/tasks/{task_id}")
async def get_task(request: Request, task_id: int):
    context = require_user(request)
    task = await asyncio.to_thread(context.tasks.get, task_id)
    if task is None:
        raise HTTPException(404, "没有这个任务")
    return task


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: int):
    context = require_user(request, write=True)
    return {"cancelled": task_runner.cancel(context, task_id)}


@app.post("/api/tasks/{task_id}/seen")
async def task_seen(request: Request, task_id: int):
    context = require_user(request, write=True)
    return {"ok": await asyncio.to_thread(context.tasks.mark_seen, task_id)}


class ReminderForm(BaseModel):
    content: str
    due_at: str
    repeat: str = ""


def reminder_fields(body):
    """The same checks the voice tool makes: some words, a real time in the future, a known way of repeating."""
    text = " ".join(body.content.split())
    if not 1 <= len(text) <= agent_tools.MAX_MEMORY_CHARS:
        raise HTTPException(400, f"提醒内容要在 1 到 {agent_tools.MAX_MEMORY_CHARS} 字之间")
    try:
        due = agent_tools.parse_due(body.due_at)
    except agent_tools.ToolError as error:
        raise HTTPException(400, "时间格式不对") from error
    if due <= datetime.now():
        raise HTTPException(400, "这个时间已经过去了")
    if body.repeat not in REPEATS:
        raise HTTPException(400, "重复方式只能是每天、工作日或每周")
    return text, due, body.repeat


@app.post("/api/reminders")
async def add_reminder(request: Request, body: ReminderForm):
    context = require_user(request, write=True)
    text, due, repeat = reminder_fields(body)
    reminder_id = await asyncio.to_thread(context.reminders.add, text, due, repeat)
    context.action_log.record("set_reminder", {"content": text, "due_at": body.due_at, "repeat": repeat}, "page", "executed")
    return {"id": reminder_id}


@app.put("/api/reminders/{reminder_id}")
async def edit_reminder(request: Request, reminder_id: int, body: ReminderForm):
    context = require_user(request, write=True)
    text, due, repeat = reminder_fields(body)
    if not await asyncio.to_thread(context.reminders.update, reminder_id, text, due, repeat):
        raise HTTPException(404, "没有这条待办提醒")
    context.action_log.record("edit_reminder", {"reminder_id": reminder_id, "content": text, "due_at": body.due_at, "repeat": repeat}, "page", "executed")
    return {"id": reminder_id}


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
    limit("upload", context.user_id)
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
    limit("lookup", context.user_id)
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
    # Set by the page: Opus microphone packets in, and whether it can play Ogg/Opus replies.
    decoder = None
    downlink_ogg = False
    history = Conversation()
    chat_id = None  # the saved conversation this socket is adding to; one is created on the first answer
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
    # Free English conversation, when on: level, topic, the partner's own short history, what was said, corrections.
    practice_session = None
    evaluations = set()
    last_heard = None  # (text, pcm) of the last sentence heard while listening in English practice
    confirmation = None
    send_lock = asyncio.Lock()

    spoken_audio = None  # (rid, [audio...]) of the English partner's reply being spoken

    async def send(kind, rid, **data):
        async with send_lock:
            await ws.send_json({"type": kind, "request_id": rid, **data})

    async def send_audio(rid, audio, text):
        if spoken_audio is not None and rid == spoken_audio[0]:
            spoken_audio[1].append(audio)  # kept so the partner's turn can be replayed later
        mime = "audio/wav" if audio.startswith(b"RIFF") else "audio/mpeg"
        # A cloned reply is WAV, about ten times the size of the same speech in Ogg/Opus.
        if mime == "audio/wav" and downlink_ogg:
            try:
                audio, mime = await asyncio.to_thread(to_ogg_opus, audio), "audio/ogg"
            except Exception:
                latency_log.exception("could not compress a reply to Opus; sending WAV")
        await send("audio", rid, data=base64.b64encode(audio).decode("ascii"), mime=mime, text=text)

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
        return not (reply_active() or recording or speaking or claimed or practice_session)

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
                hearing = asyncio.to_thread(transcribe, audio, "en") if practice_session else asyncio.to_thread(transcribe, audio)
                heard = await asyncio.wait_for(hearing, ASR_TIMEOUT_SECONDS)
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
                if action == "question" and practice_session is not None:
                    nonlocal_last_heard(heard, audio)
                await voice(action, text=heard if action == "question" else "")

    def nonlocal_last_heard(text, pcm):
        nonlocal last_heard
        last_heard = (text, pcm)

    def take_heard(text):
        """The recording of a listened sentence the page has just sent back as text, used once."""
        nonlocal last_heard
        heard, last_heard = last_heard, None
        return heard[1] if heard and heard[0] == text else None

    def save_turn_audio(session, turn, parts=None, pcm=None):
        """Keep one turn's audio as Ogg/Opus for replay: the learner's 16 kHz recording, or the partner's spoken
        reply (parts, as sent: MP3 or WAV per sentence). Returns the file name, or None; never fails a reply."""
        try:
            wav = practice.wav16k(pcm) if pcm is not None else audio_content.wav_bytes(numpy_concat([audio_content.to_samples(part) for part in parts]))
            name = f"{session['id']}-{turn}.ogg"
            folder = context.root / "english_audio"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(to_ogg_opus(wav))
            return name
        except Exception:
            latency_log.exception("could not save practice audio")
            return None

    def save_progress(session):
        """Every turn is saved as it happens, so a conversation survives a closed page or a restart."""
        with contextlib.suppress(Exception):
            context.english.save_progress(session["id"], session["turns"], session["notes"])

    async def evaluate(session, heard, before, rid, turn, pcm=None):
        """Review one learner sentence beside the partner's reply; any part that fails is just left out.

        The corrector judges language and content; a spoken sentence also gets a local fluency reference and,
        when a provider is set up, a pronunciation score. Everything is kept on the turn, a correction also
        goes into the mistake book, and the page gets each with the turn it belongs to.
        """
        async def judge():
            try:
                return await agents.run_agent(practice.CORRECTOR, f"陪练上一句：{before or '（开场）'}\n学生这一句：{heard}",
                                              agent_chat, context.user_id, LLM_MODEL, task="请检查学生这一句。")
            except Exception:
                return ""

        async def measure():
            if pcm is None:
                return None, None
            fluency = await asyncio.to_thread(practice.speech_stats, pcm, heard, TurnDetectorChance())
            scores = await asyncio.to_thread(pronunciation.assess, pcm, heard)
            return fluency, scores

        text, (fluency, scores) = await asyncio.gather(judge(), measure())
        note = practice.parse_note(text, heard)
        still_there = turn < len(session["turns"]) and session["turns"][turn].get("text") == heard and session["turns"][turn]["who"] == "learner"
        if not still_there:
            return
        review = {"logic": practice.parse_logic(text), "fluency": fluency, "pron": scores}
        if not note and session.get("review"):
            credited = session.setdefault("credited", [])
            fresh = [word for word in session["review"] if word["id"] not in credited]
            used = practice.used_words(heard, [word["text"] for word in fresh])
            for word in fresh:
                if word["text"] in used:
                    credited.append(word["id"])
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(context.english.record_review, word["id"], True)
            if used:
                review["used"] = used
        if any(review.values()):
            session["turns"][turn]["review"] = review
            await asyncio.to_thread(save_progress, session)
            with contextlib.suppress(Exception):
                await send("practice_review", rid, turn=turn, **review)
            for word in pronunciation.weak_words(scores):
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(context.english.add_item, "word", word["word"], note=f"发音需要练习（{word['score']} 分）",
                                            context=heard, session_id=session["id"])
        if note:
            note["turn"] = turn
            session["notes"].append(note)
            await asyncio.to_thread(save_progress, session)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(context.english.add_item, "mistake", heard, note["corrected"] or note["better"],
                                        note=note["explain"], context=note["original"], session_id=session["id"])
            with contextlib.suppress(Exception):
                await send("practice_note", rid, **note)

    async def finish_practice(session):
        """Save the session once its last corrections are in, then hand it to the report agent."""
        pending_notes = [item for item in evaluations if not item.done()]
        if pending_notes:
            await asyncio.wait(pending_notes, timeout=8)
        context.english.finish(session["id"], session["turns"], session["notes"])
        task_id, error = None, ""
        if any(turn["who"] == "learner" for turn in session["turns"]):
            try:
                task_id = task_runner.submit(context, "practice_report", "英语对话总结", {"session_id": session["id"]})
            except (agents.AgentStopped, ValueError) as refused:
                error = str(refused)
        else:
            error = "这次没有对话，不生成总结"
        with contextlib.suppress(Exception):
            await send("practice_stopped", None, session_id=session["id"], task_id=task_id, error=error, notes=len(session["notes"]))

    async def save_chat_turn(rid, said, answered):
        """Keep a finished turn of the assistant conversation, so it can be read and continued later."""
        nonlocal chat_id
        try:
            if chat_id is None or not await asyncio.to_thread(context.chats.add_turn, chat_id, said, answered):
                chat_id = await asyncio.to_thread(context.chats.create, said)
                await asyncio.to_thread(context.chats.add_turn, chat_id, said, answered)
            saved = await asyncio.to_thread(context.chats.get, chat_id)
            await send("chat_saved", rid, chat_id=chat_id, title=saved["title"], turns=saved["turns"])
        except Exception:
            latency_log.exception("could not save the conversation")

    async def reply(rid, text=None, audio=None, source="text"):
        nonlocal spoken_audio
        worker = None
        spoken_pcm = None
        # Practice is decided once per reply, like the voice: stopping practice never changes a reply halfway.
        session = practice_session
        # Read once per reply, so a voice change applies from the next reply, never halfway through one.
        voice = PRACTICE_VOICE if session else current_voice()
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
                await send("status", rid, text=f"识别排队中，前面还有 {recognition_gate.queue_length() + 1} 句…" if recognition_gate.busy() else "正在识别…")
                text = await (asyncio.to_thread(transcribe, audio, "en") if session else asyncio.to_thread(transcribe, audio))
                spoken_pcm = audio
                mark("asr_done")
                if not text:
                    await send("error", rid, text=await asyncio.to_thread(no_speech_message, audio))
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
                        async with contextlib.aclosing(synthesize_parts(sentence, voice if session else None)) as parts:
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
                    sentence, pending = (practice.split_english if session else split_sentence)(pending)
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
                # Spoken as it streams: the backup is tried only if DeepSeek has said nothing within a few seconds.
                return llm.chat(messages, llm_router.VOICE, **options,
                                on_usage=lambda provider, used: usage.LEDGER.record_chat(context.user_id, provider.model, used))

            user_message = {"role": "user", "content": text}
            if session:
                # The partner: its own prompt and history, no tools, billed as "tutor". The corrector starts now,
                # beside it, so a correction costs the learner no waiting.
                if text != practice.OPENING:
                    before = next((turn["text"] for turn in reversed(session["turns"]) if turn["who"] == "tutor"), "")
                    session["turns"].append({"who": "learner", "text": text})
                    turn = len(session["turns"]) - 1
                    pcm = spoken_pcm if spoken_pcm is not None else (take_heard(text) if source == "listen" else None)
                    if pcm is not None:
                        recording_name = await asyncio.to_thread(save_turn_audio, session, turn, pcm=pcm)
                        if recording_name:
                            session["turns"][turn].update(audio=recording_name, seconds=round(len(pcm) / 32000, 1))
                    await send("practice_turn", rid, turn=turn, who="learner", audio=bool(session["turns"][turn].get("audio")))
                    await asyncio.to_thread(save_progress, session)
                    evaluation = asyncio.create_task(evaluate(session, text, before, rid, turn, pcm))
                    evaluations.add(evaluation)
                    evaluation.add_done_callback(evaluations.discard)
                system_message = {"role": "system", "content": practice.partner_prompt(
                    session["level"], session["topic"], [word["text"] for word in session.get("review", [])])}
                spoken_audio = (rid, [])
                # Everything before this sentence; the opening has nothing before it.
                earlier = [] if text == practice.OPENING else practice.history_for(session["turns"][:-1])[-practice.HISTORY_MESSAGES:]
                agent_token = usage.CURRENT_AGENT.set("tutor")
                try:
                    added = await run_turn([system_message, *earlier, user_message], NO_TOOLS, chat, on_llm_event)
                finally:
                    usage.CURRENT_AGENT.reset(agent_token)
            else:
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
            if session:
                answer = added[-1].get("content") or ""
                session["turns"].append({"who": "tutor", "text": answer})
                turn = len(session["turns"]) - 1
                parts = spoken_audio[1] if spoken_audio and spoken_audio[0] == rid else []
                spoken_audio = None
                if parts:
                    name = await asyncio.to_thread(save_turn_audio, session, turn, parts)
                    if name:
                        session["turns"][turn]["audio"] = name
                await send("practice_turn", rid, turn=turn, who="tutor", audio=bool(session["turns"][turn].get("audio")))
                await asyncio.to_thread(save_progress, session)
            else:
                history.add_turn([user_message, *without_outside_results(added, default_registry)])
                finished_actions.clear()
                answered = (added[-1].get("content") or "").strip()
                if answered and not demo:
                    await save_chat_turn(rid, text, answered)
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
                    audio_in = decoder.decode(message["bytes"]) if decoder else message["bytes"]
                    for event, audio in detector.feed(audio_in):
                        await on_turn(event, audio)
                    continue
                if not recording or len(pcm) + len(message["bytes"]) > MAX_BYTES:
                    await ws.close(code=1009)
                    break
                pcm.extend(message["bytes"])
                continue
            data = json.loads(message.get("text", "{}"))
            kind = data.get("type")
            if kind == "client":
                downlink_ogg = "ogg-opus" in (data.get("audio") or [])
                continue
            if kind == "listen_start":
                detector = await asyncio.to_thread(TurnDetector)
                decoder = OpusDecoder() if data.get("codec") == "opus" else None
                hear_task = hear_task or asyncio.create_task(hear_worker())
                continue
            if kind == "listen_gap":
                # The page went quiet and stopped sending. Drop stale pre-roll so the next sentence does not
                # start with audio from before the silence; never cut off a sentence still in progress.
                if detector is not None and getattr(detector, "audio", None) is None:
                    detector.reset()
                continue
            if kind == "listen_stop":
                detector = None
                decoder = None
                paused = interrupted = False
                continue
            if kind == "chat_new":
                await stop()
                history.turns.clear()
                chat_id = None
                await send("chat_started", None, chat_id=None)
                continue
            if kind == "chat_open":
                opened = await asyncio.to_thread(context.chats.get, data.get("chat_id")) if isinstance(data.get("chat_id"), int) else None
                if opened is None:
                    await send("error", None, text="没有这次对话")
                    continue
                await stop()
                history.turns.clear()
                messages = opened["messages"]
                for said, answered in zip(messages[::2], messages[1::2]):
                    history.add_turn([{"role": "user", "content": said["text"]}, {"role": "assistant", "content": answered["text"]}])
                chat_id = opened["id"]
                await send("chat_started", None, chat_id=chat_id, title=opened["title"], messages=messages)
                continue
            if kind == "practice_stop":
                if practice_session is not None:
                    await stop()
                    ended, practice_session = practice_session, None
                    asyncio.create_task(finish_practice(ended))
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
            elif kind == "practice_start":
                level, topic = data.get("level"), " ".join(str(data.get("topic") or "").split())
                if level not in practice.LEVELS:
                    await send("error", rid, text="请选择英语水平")
                    continue
                if len(topic) > practice.MAX_TOPIC_CHARS:
                    await send("error", rid, text=f"话题最多 {practice.MAX_TOPIC_CHARS} 字")
                    continue
                await stop()
                request_id, recording = rid, False
                pcm.clear()
                if practice_session is not None:
                    asyncio.create_task(finish_practice(practice_session))
                context.save_settings({"english_level": level})
                review = [{"id": item["id"], "text": item["text"]} for item in context.english.due(3, kinds=("word", "phrase"))]
                practice_session = {"id": context.english.start(level, topic), "level": level, "topic": topic, "turns": [], "notes": [],
                                    "review": review}
                await send("practice_started", rid, level=practice.LEVELS[level][0], topic=topic, session_id=practice_session["id"],
                           review_words=[word["text"] for word in review])
                task = asyncio.create_task(reply(rid, text=practice.OPENING, source="practice"))
            elif kind == "practice_resume":
                # Start a new conversation from just before one sentence of an earlier one, to say it again.
                earlier = context.english.session(data.get("session_id")) if isinstance(data.get("session_id"), int) else None
                turn = data.get("turn")
                if not earlier or not practice.is_learner_turn(earlier["transcript"], turn):
                    await send("error", rid, text="找不到要重说的那一句")
                    continue
                await stop()
                request_id, recording = rid, False
                pcm.clear()
                if practice_session is not None:
                    asyncio.create_task(finish_practice(practice_session))
                kept = earlier["transcript"][:turn]
                notes = [note for note in earlier["notes"] if note.get("turn", turn) < turn]
                practice_session = {"id": context.english.start(earlier["level"], earlier["topic"]), "level": earlier["level"],
                                    "topic": earlier["topic"], "turns": kept, "notes": notes}
                await asyncio.to_thread(save_progress, practice_session)
                await send("practice_started", rid, level=practice.LEVELS[earlier["level"]][0], topic=earlier["topic"],
                           session_id=practice_session["id"], turns=kept, notes=notes, redo=earlier["transcript"][turn]["text"])
            elif kind == "practice_rewind":
                # Take back one of your sentences and everything after it, then say it again.
                turn = data.get("turn")
                if practice_session is None or not practice.is_learner_turn(practice_session["turns"], turn):
                    await send("error", rid, text="找不到要重说的那一句")
                    continue
                await stop()
                request_id, recording = rid, False
                pcm.clear()
                redo = practice_session["turns"][turn]["text"]
                practice_session["turns"] = practice_session["turns"][:turn]
                practice_session["notes"] = [note for note in practice_session["notes"] if note.get("turn", turn) < turn]
                await asyncio.to_thread(save_progress, practice_session)
                await send("practice_rewound", rid, turn=turn, text=redo)
            elif ((kind in {"text", "practice_start", "practice_resume"} or (kind == "audio_end" and recording and rid == request_id))
                  and (turn_wait := limiter.hit("turn", context.user_id)) is not None):
                # Too many questions too fast: say how long to wait; a recording in progress is dropped.
                metrics.count("limited_turn")
                await send("error", rid, text=limiter.message("turn", turn_wait))
                if kind == "audio_end":
                    recording = False
                    pcm.clear()
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
        if practice_session is not None:
            asyncio.create_task(finish_practice(practice_session))
