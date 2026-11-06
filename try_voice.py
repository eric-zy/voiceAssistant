"""Generate an Edge TTS sample, without calling DeepSeek or using an API key."""
import argparse
import asyncio
from pathlib import Path

import app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    parser.add_argument("--rate", default="-5%")
    args = parser.parse_args()
    app.TTS_PROVIDER, app.VOICE, app.TTS_RATE = "edge", args.voice, args.rate
    target = Path(__file__).resolve().parent / "outputs" / f"{args.voice}.mp3"
    target.parent.mkdir(exist_ok=True)
    try:
        audio = asyncio.run(app.synthesize("你好，我在呢。今天想聊点什么？别着急，我们可以慢慢说。"))
        target.write_bytes(audio)
        print(f"Saved {len(audio)} bytes: {target}")
    except RuntimeError as error:
        print(str(error))
        raise SystemExit(1)
