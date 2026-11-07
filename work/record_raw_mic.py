"""Record straight from Windows, skipping the browser, to tell a weak microphone from browser processing.

Needs ffmpeg. Records only after Enter is pressed; clips go to data/heard_raw and stay on this machine.
Usage: python work/record_raw_mic.py ["device name"]
"""
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "data" / "heard_raw"
DEFAULT_DEVICE = "麦克风 (2- USB2.0 MIC)"
SECONDS = 4
PHRASES = ["测试确认", "确定执行", "今天天气怎么样"]


def record(device, path):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "dshow", "-i", f"audio={device}",
               "-t", str(SECONDS), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path)]
    return subprocess.run(command, capture_output=True).returncode == 0


def main():
    sys.stdout.reconfigure(errors="replace")
    device = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DEVICE
    FOLDER.mkdir(parents=True, exist_ok=True)
    print(f"麦克风：{device}，共 {len(PHRASES)} 句，每句录 {SECONDS} 秒。和平时一样的距离和音量。")
    for number, phrase in enumerate(PHRASES, 1):
        input(f"\n第 {number} 句：准备好说“{phrase}”后按回车，然后马上说…")
        print("录音中…", flush=True)
        if not record(device, FOLDER / f"{time.strftime('%H%M%S')}-{number}.wav"):
            print("录音失败：请确认麦克风名称，并安装 ffmpeg。")
            return 1
        print("完成")
    print("\n分析结果：")
    return subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "work" / "diagnose_audio.py"), str(FOLDER)]).returncode


if __name__ == "__main__":
    sys.exit(main())
