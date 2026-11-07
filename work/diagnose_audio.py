"""Is misrecognition the sound or the model? Measures recordings kept by ./start.ps1 -DemoConfirm.

Usage: python work/diagnose_audio.py [folder]. Default folder: data/heard.
"""
from pathlib import Path
import sys
import wave

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FRAME = 480  # 30 ms at 16 kHz
QUIET_DB, NOISY_SNR_DB, CLIPPED_SHARE = -35, 15, .001
# Clean speech keeps about 3% of its energy above 3.4 kHz; a telephone-band microphone keeps none.
BAND_EDGE_HZ, MIN_HIGH_SHARE = 3400, .005


def read_pcm(path):
    with wave.open(str(path), "rb") as audio:
        return audio.readframes(audio.getnframes())


def decibels(value):
    return 20 * np.log10(max(float(value), 1e-6))


def high_share(frames, levels):
    """Share of speech energy above the telephone band, where 测, 试 and 确 are told apart."""
    loud = frames[levels > .1 * levels.max()]
    power = (np.abs(np.fft.rfft(loud * np.hanning(FRAME), axis=1)) ** 2).sum(0)
    return float(power[np.fft.rfftfreq(FRAME, 1 / 16000) >= BAND_EDGE_HZ].sum() / max(power.sum(), 1e-12))


def measure(pcm):
    """Speech level and noise floor come from the loudest and quietest frames of the same clip."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    frames = samples[:len(samples) // FRAME * FRAME].reshape(-1, FRAME)
    levels = np.sqrt(np.mean(frames ** 2, axis=1))
    speech, noise = np.percentile(levels, 90), np.percentile(levels, 10)
    return {"seconds": len(samples) / 16000, "speech_db": decibels(speech), "noise_db": decibels(noise),
            "snr_db": decibels(speech) - decibels(noise), "clipped": float(np.mean(np.abs(samples) >= .99)),
            "high_share": high_share(frames, levels)}


def verdict(stats):
    problems = []
    if stats["speech_db"] < QUIET_DB:
        problems.append("音量太小")
    if stats["clipped"] > CLIPPED_SHARE:
        problems.append("音量过大削波")
    if stats["snr_db"] < NOISY_SNR_DB:
        problems.append("噪声大")
    if stats["high_share"] < MIN_HIGH_SHARE:
        problems.append("缺高频")
    return "、".join(problems) or "声音正常"


def main():
    sys.stdout.reconfigure(errors="replace")
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "heard"
    paths = sorted(folder.glob("*.wav"))
    if not paths:
        print(f"没有录音：{folder}。请先用 ./start.ps1 -DemoConfirm 启动并开启持续监听说几句。")
        return 1
    import app
    from turn_detector import measure_speech
    print(f"{'文件':<20}{'秒':>5}{'语音dB':>8}{'底噪dB':>8}{'信噪比':>7}{'削波%':>7}{'高频%':>7}{'语音占比':>9}  判断 / 识别结果")
    verdicts = []
    for path in paths:
        pcm = read_pcm(path)
        stats = measure(pcm)
        _, share = measure_speech(pcm)
        try:
            heard = app.transcribe(pcm)
        except ValueError:
            heard = ""
        verdicts.append(verdict(stats))
        print(f"{path.stem:<20}{stats['seconds']:>5.1f}{stats['speech_db']:>8.1f}{stats['noise_db']:>8.1f}{stats['snr_db']:>7.1f}"
              f"{stats['clipped'] * 100:>7.2f}{stats['high_share'] * 100:>7.2f}{share:>9.2f}  {verdicts[-1]} / {heard!r}")
    print(f"\n共 {len(paths)} 段，声音正常 {verdicts.count('声音正常')} 段。")
    print("多数“声音正常”但仍认错：问题在模型或浏览器处理，试麦克风诊断的“原始声音”，或换更大的模型。")
    print("多数有问题：先修声音，比如靠近麦克风、调输入音量、换麦克风。缺高频通常要换麦克风。")
    print(f"也可以直接听：{folder}。人耳听不清就是声音问题，听得清就是模型问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
