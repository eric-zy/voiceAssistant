"""Run in .venv-cosy. Online model preparation only."""
from pathlib import Path
import shutil
from modelscope import snapshot_download

if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "models/CosyVoice2-0.5B"
    downloaded = Path(snapshot_download("iic/CosyVoice2-0.5B", local_dir=str(target),
                      cache_dir=str(target.parent / ".modelscope"),
                      ignore_file_pattern=["*.zip", "*.engine", "*.plan"]))
    if not (target / "cosyvoice2.yaml").is_file() and downloaded.is_dir() and downloaded.resolve() != target.resolve():
        shutil.copytree(downloaded, target, dirs_exist_ok=True)
    for required in ("cosyvoice2.yaml", "llm.pt", "flow.pt", "hift.pt", "speech_tokenizer_v2.onnx"):
        if not (target / required).is_file():
            raise RuntimeError(f"Model download incomplete: {required}")
    print(f"Model downloaded: {target}")
