"""Local reference-voice storage and CosyVoice WAV transport."""
from io import BytesIO
import base64
import json
from pathlib import Path
import re
import uuid
import wave
from urllib.parse import urlsplit

import httpx
import numpy as np


class VoiceStore:
    def __init__(self, root):
        self.root = Path(root)

    def list(self):
        if not self.root.exists():
            return []
        profiles = []
        for path in self.root.glob("*/profile.json"):
            try:
                profile = self.get(path.parent.name)
                profiles.append({k: profile[k] for k in ("id", "name", "duration")})
            except (ValueError, OSError, KeyError):
                continue
        return profiles

    def get(self, profile_id):
        if not isinstance(profile_id, str) or not re.fullmatch(r"[0-9a-f]{32}", profile_id):
            raise ValueError("请选择已上传的参考音色")
        folder = self.root / profile_id
        if not (folder / "reference.wav").is_file():
            raise ValueError("参考录音不存在，请重新上传")
        profile = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
        profile["wav_path"] = folder / "reference.wav"
        return profile

    def add(self, name, prompt_text, raw_audio):
        import av
        if not 1 <= len(name.strip()) <= 60 or not 1 <= len(prompt_text.strip()) <= 2000:
            raise ValueError("请填写音色名称（最多60字）和与录音准确对应的文字（最多2000字）")
        try:
            chunks, count = [], 0
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
            def append_frames(frames):
                nonlocal count
                for frame in frames:
                    samples = frame.to_ndarray().reshape(-1)
                    count += len(samples)
                    if count > 30 * 16000:
                        raise ValueError("参考录音需为3～30秒")
                    chunks.append(samples)
            with av.open(BytesIO(raw_audio)) as container:
                for frame in container.decode(audio=0):
                    append_frames(resampler.resample(frame))
                append_frames(resampler.resample(None))
            samples = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.float32)
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("无法读取音频，请上传 WAV、MP3 或 M4A 录音") from error
        duration = len(samples) / 16000
        if not 3 <= duration <= 30:
            raise ValueError("参考录音需为 3～30 秒，建议清晰单人录音约10秒")
        if not np.isfinite(samples).all() or np.sqrt(np.mean(samples ** 2)) < .0001:
            raise ValueError("参考录音无有效声音")
        profile_id = uuid.uuid4().hex
        folder = self.root / profile_id
        folder.mkdir(parents=True)
        with wave.open(str(folder / "reference.wav"), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes((samples.clip(-1, 1) * 32767).astype("<i2").tobytes())
        profile = {"id": profile_id, "name": name.strip(), "prompt_text": prompt_text.strip(), "duration": round(duration, 2)}
        (folder / "profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        return {k: profile[k] for k in ("id", "name", "duration")}

    def remove(self, profile_id):
        import shutil
        self.get(profile_id)
        shutil.rmtree(self.root / profile_id)

    def settings(self):
        try:
            return json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(self, settings):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / "settings.tmp"
        temporary.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.root / "settings.json")


class CloneBusy(RuntimeError):
    """The GPU is still on an earlier segment, even if the request for it was cancelled."""


def validate_service_url(base_url):
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("声音克隆服务地址必须是本机 HTTP 地址")


async def synthesize_clone(profile, text, base_url, *, transport=None):
    validate_service_url(base_url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=3), trust_env=False, transport=transport) as client:
        with profile["wav_path"].open("rb") as reference:
            async with client.stream("POST", base_url.rstrip("/") + "/inference_zero_shot",
                data={"tts_text":text, "prompt_text":profile["prompt_text"]},
                files={"prompt_wav":("reference.wav", reference, "audio/wav")}) as response:
                if response.status_code == 429:
                    raise CloneBusy("上一条克隆合成尚未结束，请稍后再试听；可用“检查服务”查看忙碌状态")
                if response.status_code != 200:
                    raise RuntimeError(f"CosyVoice 合成失败（HTTP {response.status_code}），请查看克隆服务窗口")
                audio = bytearray()
                async for chunk in response.aiter_bytes():
                    audio.extend(chunk)
                    if len(audio) > 32 * 1024 * 1024:
                        raise RuntimeError("克隆服务返回音频过大")
    try:
        with wave.open(BytesIO(audio), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getnframes() == 0:
                raise ValueError("invalid WAV")
    except (wave.Error, EOFError, ValueError) as error:
        raise RuntimeError("克隆服务未返回有效 WAV，请使用本项目 cosy_server.py，不能直接替换为返回裸 PCM 的服务") from error
    return bytes(audio)


async def stream_clone(profile, text, base_url, *, transport=None):
    """Yield independently decodable WAV chunks as soon as the model emits them."""
    validate_service_url(base_url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=3), trust_env=False, transport=transport) as client:
        with profile["wav_path"].open("rb") as reference:
            async with client.stream("POST", base_url.rstrip("/") + "/inference_zero_shot_stream",
                data={"tts_text":text, "prompt_text":profile["prompt_text"]},
                files={"prompt_wav":("reference.wav", reference, "audio/wav")}) as response:
                if response.status_code == 404:
                    raise RuntimeError("请重启 CosyVoice 服务以启用流式播报")
                if response.status_code != 200:
                    raise RuntimeError(f"流式克隆失败（HTTP {response.status_code}）")
                total, received = 0, False
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if item.get("error"):
                        raise RuntimeError("克隆服务繁忙或合成失败，请查看克隆服务窗口")
                    if item.get("done"):
                        if not received:
                            raise RuntimeError("克隆服务未返回音频")
                        return
                    audio = base64.b64decode(item["audio"], validate=True)
                    total += len(audio)
                    if total > 32 * 1024 * 1024:
                        raise RuntimeError("克隆服务返回音频过大")
                    with wave.open(BytesIO(audio), "rb") as wav:
                        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or not wav.getnframes():
                            raise RuntimeError("流式克隆返回无效 WAV")
                    received = True
                    yield audio
                raise RuntimeError("克隆音频传输中断，请重试")
