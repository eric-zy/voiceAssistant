"""Exercise real local cloning without changing the saved voice or calling DeepSeek."""
import argparse
import asyncio
from pathlib import Path
import tempfile
import time

from voice_clone import VoiceStore, synthesize_clone


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    parser.add_argument("--prompt-text", required=True)
    parser.add_argument("--text", default="你好，欢迎回来。今天过得怎么样？")
    parser.add_argument("--output", default="outputs/clone-preview.wav")
    args = parser.parse_args()
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="clone-test-") as directory:
        store = VoiceStore(directory)
        created = store.add("Temporary preview", args.prompt_text, Path(args.reference).read_bytes())
        result = asyncio.run(synthesize_clone(store.get(created["id"]), args.text, "http://127.0.0.1:50001"))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result)
    print(f"Saved: {output.resolve()}, {len(result)} bytes, {time.monotonic()-start:.1f}s")
