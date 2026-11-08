"""Online once: download speech recognition models; the server uses only these local directories afterwards.

  python prepare_asr.py               Whisper base (fallback, about 142 MB)
  python prepare_asr.py --sensevoice  SenseVoice Small int8 (default when present, about 239 MB)
"""
from pathlib import Path
import sys

import httpx

MODELS = Path(__file__).resolve().parent / "models"
WHISPER = ("Systran/faster-whisper-base", MODELS / "whisper-base", ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin"))
SENSEVOICE = ("csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17", MODELS / "sensevoice-small", ("tokens.txt", "model.int8.onnx"))


def prepare(repository, target, names):
    target.mkdir(parents=True, exist_ok=True)
    # Plain HTTPS avoids optional Xet transfer dependencies on restricted networks.
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, connect=15)) as client:
        for name in names:
            final = target / name
            if final.is_file() and final.stat().st_size > 0:
                print(f"Already downloaded: {name}", flush=True)
                continue
            partial = target / (name + ".download")
            url = f"https://huggingface.co/{repository}/resolve/main/{name}?download=true"
            size, last_report = 0, 0
            print(f"Downloading {name} ...", flush=True)
            with client.stream("GET", url) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise RuntimeError("Expected full model download, got partial response")
                with partial.open("wb") as output:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        output.write(chunk)
                        size += len(chunk)
                        if size - last_report >= 8 * 1024 * 1024:
                            print(f"  {size // (1024 * 1024)} MiB", flush=True)
                            last_report = size
                expected = response.headers.get("content-length")
                if size == 0 or (expected and size != int(expected)):
                    raise RuntimeError("Incomplete model download; rerun this script")
            partial.replace(final)
    print(f"Ready: {target}")


if __name__ == "__main__":
    prepare(*(SENSEVOICE if "--sensevoice" in sys.argv else WHISPER))
