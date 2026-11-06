"""Original cartoon-style test speech; not a clone of any actor or character."""
import asyncio
from pathlib import Path
import subprocess

import edge_tts

ROOT = Path(__file__).resolve().parents[1]
TEXT = "嘿嘿，今天的任务，就是开开心心地玩耍！先吃一块小饼干，再去外面找点好玩的。咦，怎么又要收拾玩具啦？好吧好吧，等我数到三，我们就一起行动！"

async def main():
    folder = ROOT / "outputs"
    folder.mkdir(exist_ok=True)
    mp3 = folder / "cartoon_child_test.mp3"
    wav = folder / "cartoon_child_reference.wav"
    (folder / "cartoon_child_transcript.txt").write_text(TEXT, encoding="utf-8")
    communicate = edge_tts.Communicate(TEXT, "zh-CN-YunxiaNeural", rate="-10%", pitch="+25Hz")
    await communicate.save(str(mp3))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], check=True)
    print(f"MP3: {mp3}\nReference WAV: {wav}")

if __name__ == "__main__":
    asyncio.run(main())
