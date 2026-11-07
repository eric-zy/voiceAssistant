"""Sentence vectors from bge-small-zh-v1.5 (int8 ONNX) on the CPU, leaving the GPU to the cloned voice."""
from pathlib import Path
import threading

import numpy as np

MODEL_FILE = Path("onnx") / "model_int8.onnx"
TOKENIZER_FILE = "tokenizer.json"
MAX_TOKENS = 512
BATCH = 16


class Embedder:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.session = self.tokenizer = None
        self.lock = threading.Lock()

    def available(self):
        return (self.folder / MODEL_FILE).is_file() and (self.folder / TOKENIZER_FILE).is_file()

    def load(self):
        import onnxruntime
        from tokenizers import Tokenizer
        self.tokenizer = Tokenizer.from_file(str(self.folder / TOKENIZER_FILE))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()
        self.session = onnxruntime.InferenceSession(str(self.folder / MODEL_FILE), providers=["CPUExecutionProvider"])

    def encode(self, texts):
        """Unit-length vectors, so a dot product is cosine similarity. The model uses the first token (CLS)."""
        with self.lock:
            if self.session is None:
                self.load()
            batches = []
            for start in range(0, len(texts), BATCH):
                encoded = self.tokenizer.encode_batch(list(texts[start:start + BATCH]))
                feed = {name: np.array([getattr(item, field) for item in encoded], dtype=np.int64)
                        for name, field in (("input_ids", "ids"), ("attention_mask", "attention_mask"), ("token_type_ids", "type_ids"))}
                vectors = self.session.run(None, feed)[0][:, 0]
                batches.append(vectors / np.linalg.norm(vectors, axis=1, keepdims=True))
        return np.concatenate(batches).astype(np.float32) if batches else np.zeros((0, 0), np.float32)
