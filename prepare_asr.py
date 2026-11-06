"""Online once: download model; server uses only this local directory afterwards."""
from pathlib import Path
import httpx


def prepare():
    target = Path(__file__).resolve().parent / "models" / "whisper-base"
    target.mkdir(parents=True, exist_ok=True)
    # Plain HTTPS avoids optional Xet transfer dependencies on restricted networks.
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, connect=15)) as client:
        for name in ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin"):
            final = target / name
            if final.is_file() and final.stat().st_size > 0:
                print(f"Already downloaded: {name}", flush=True)
                continue
            partial = target / (name + ".download")
            url = f"https://huggingface.co/Systran/faster-whisper-base/resolve/main/{name}?download=true"
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
                        if size - last_report >= 1024 * 1024:
                            print(f"  {size // (1024 * 1024)} MiB", flush=True)
                            last_report = size
                expected = response.headers.get("content-length")
                if size == 0 or (expected and size != int(expected)):
                    raise RuntimeError("Incomplete model download; rerun this script")
            partial.replace(final)
    print(f"Ready: {target}")

if __name__ == "__main__":
    prepare()
