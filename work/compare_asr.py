"""Whisper base (as the app runs it) against SenseVoice Small on the same audio. Offline; changes nothing.

Uses recordings kept in data/heard (browser) and data/heard_raw (direct), plus synthetic speech at full and
telephone bandwidth, which mimics the webcam microphone. Needs models/sensevoice-small and sherpa-onnx.
"""
import asyncio
from pathlib import Path
import re
import sys
import tempfile
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app  # noqa: E402

SENSEVOICE = ROOT / "models" / "sensevoice-small"
# data/heard_raw is recorded by work/record_raw_mic.py in this order.
RAW_TRUTH = {"1": "测试确认", "2": "确定执行", "3": "今天天气怎么样"}
PHRASES = ["确认", "取消", "确定执行", "测试确认", "今天天气怎么样", "合肥明天的天气怎么样", "记住我不吃香菜", "一分钟后提醒我喝水"]
VOICES = ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-YunyangNeural"]


def words(text):
    return re.sub(r"[\W_]+", "", app.to_chinese_variant(text, "zh-cn"))


def sensevoice():
    import sherpa_onnx
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(SENSEVOICE / "model.int8.onnx"), tokens=str(SENSEVOICE / "tokens.txt"),
        num_threads=2, language="zh", use_itn=True)


def recognize(recognizer, samples):
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    return stream.result.text


def read(path):
    with wave.open(str(path), "rb") as audio:
        return np.frombuffer(audio.readframes(audio.getnframes()), "<i2").astype(np.float32) / 32768


def pcm(samples):
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


def both(recognizer, samples):
    started = time.perf_counter()
    try:
        whisper = app.transcribe(pcm(samples))
    except ValueError:
        whisper = ""
    middle = time.perf_counter()
    sense = recognize(recognizer, samples)
    return whisper, sense, middle - started, time.perf_counter() - middle


def synthetic():
    import av
    import edge_tts
    folder = Path(tempfile.mkdtemp())
    for voice in VOICES:
        for phrase in PHRASES:
            path = folder / "a.mp3"
            try:
                asyncio.run(edge_tts.Communicate(phrase, voice).save(str(path)))
            except Exception:
                continue
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            with av.open(str(path)) as container:
                parts = [out.to_ndarray().reshape(-1) for frame in container.decode(audio=0) for out in resampler.resample(frame)]
            clean = np.concatenate(parts).astype(np.float32) / 32768
            padded = np.concatenate([np.zeros(4800, np.float32), clean, np.zeros(9600, np.float32)])
            spectrum = np.fft.rfft(padded)
            spectrum[np.fft.rfftfreq(len(padded), 1 / 16000) > 3400] = 0
            yield phrase, voice, padded, np.fft.irfft(spectrum, len(padded)).astype(np.float32)


def main():
    sys.stdout.reconfigure(errors="replace")
    started = time.perf_counter()
    recognizer = sensevoice()
    print(f"SenseVoice loaded in {time.perf_counter() - started:.1f}s")
    app.transcribe(pcm(np.zeros(16000, np.float32)))  # load Whisper before timing

    print("\n== 直录（绕过浏览器），内容已知 ==")
    for path in sorted((ROOT / "data" / "heard_raw").glob("*.wav")):
        truth = RAW_TRUTH.get(path.stem.split("-")[-1], "?")
        whisper, sense, *_ = both(recognizer, read(path))
        print(f"  应为 {truth:<8} Whisper {whisper!r:<22} {'对' if words(whisper) == truth else '错'}   SenseVoice {sense!r:<22} {'对' if words(sense) == truth else '错'}")

    print("\n== 浏览器录下的片段，内容未逐条记录 ==")
    for path in sorted((ROOT / "data" / "heard").glob("*.wav")):
        whisper, sense, *_ = both(recognizer, read(path))
        print(f"  {path.stem[9:15]}  Whisper {whisper!r:<26} SenseVoice {sense!r}")

    print("\n== 合成语音：完整音质 / 切到 3.4 kHz（模拟摄像头麦克风） ==")
    score = {"whisper_full": 0, "sense_full": 0, "whisper_phone": 0, "sense_phone": 0}
    times = {"whisper": [], "sense": []}
    total = 0
    for phrase, voice, full, phone in synthetic():
        total += 1
        wf, sf, tw, ts = both(recognizer, full)
        wp, sp, *_ = both(recognizer, phone)
        times["whisper"].append(tw)
        times["sense"].append(ts)
        score["whisper_full"] += words(wf) == phrase
        score["sense_full"] += words(sf) == phrase
        score["whisper_phone"] += words(wp) == phrase
        score["sense_phone"] += words(sp) == phrase
        if words(wp) != phrase or words(sp) != phrase:
            print(f"  {phrase:<10} {voice[6:12]}  3.4kHz  Whisper {wp!r:<20} SenseVoice {sp!r}")
    print(f"\n{total} 句合成语音全对的句数：")
    print(f"  完整音质  Whisper {score['whisper_full']}/{total}   SenseVoice {score['sense_full']}/{total}")
    print(f"  3.4 kHz   Whisper {score['whisper_phone']}/{total}   SenseVoice {score['sense_phone']}/{total}")
    print(f"每句识别耗时中位数：Whisper {np.median(times['whisper']) * 1000:.0f} ms，SenseVoice {np.median(times['sense']) * 1000:.0f} ms")


if __name__ == "__main__":
    main()
