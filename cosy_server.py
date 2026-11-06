"""Run in .venv-cosy; isolates model dependencies from the chat app."""
import argparse
import asyncio
import base64
import json
import queue
from collections import OrderedDict
import hashlib
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
import threading
import wave

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

app = FastAPI()
model = None
model_lock = threading.Lock()
inference_gate = asyncio.Semaphore(1)
reference_cache = OrderedDict()
device = "unknown"
warmup_status = "not_started"
FLOW_PROMPT_SECONDS = float(os.getenv("COSY_FLOW_PROMPT_SECONDS", "5"))
VRAM_LIMIT_GB = float(os.getenv("COSY_VRAM_LIMIT_GB", "3.7"))
MAX_CHUNK_CHARS = 80
TOKENS_PER_SECOND = 25
MEL_FRAMES_PER_TOKEN = 2


@app.get("/health")
def health():
    return {"ready":model is not None, "sample_rate":getattr(model, "sample_rate", None),
            "busy":inference_gate.locked(), "device":device, "warmup":warmup_status}


def warmup_current_voice(root):
    """Pay reference extraction and first CUDA inference cost before accepting chat."""
    global warmup_status
    from voice_clone import VoiceStore
    store = VoiceStore(root / "voices")
    settings = store.settings()
    if settings.get("provider") != "cosyvoice" or not settings.get("profile_id"):
        warmup_status = "skipped"
        return
    warmup_status = "running"
    try:
        profile = store.get(settings["profile_id"])
        print("Warming selected voice and CUDA kernels; no audio will be played...", flush=True)
        generate("你好，很高兴和你聊天。", profile["prompt_text"], profile["wav_path"].read_bytes())
        warmup_status = "ready"
        print("Voice warmup finished. Ready for chat.", flush=True)
    except Exception as error:
        warmup_status = "failed"
        print(f"Voice warmup failed ({type(error).__name__}); check selected reference before chatting.", flush=True)


def crop_flow_prompt(speaker, seconds):
    """Keep only the reference tail for the flow model; the LLM prompt stays whole.

    Flow recomputes its prompt on all 10 solver steps of every segment, so a
    16 s reference costs far more than the 3 s of speech it conditions. Flow
    takes no prompt text, so it can be shortened without re-aligning transcript.
    """
    tokens = int(seconds * TOKENS_PER_SECOND)
    if tokens <= 0 or speaker["flow_prompt_speech_token"].shape[1] <= tokens:
        return
    frames = tokens * MEL_FRAMES_PER_TOKEN
    speaker["flow_prompt_speech_token"] = speaker["flow_prompt_speech_token"][:, -tokens:].contiguous()
    speaker["prompt_speech_feat"] = speaker["prompt_speech_feat"][:, -frames:].contiguous()
    speaker["flow_prompt_speech_token_len"] = speaker["flow_prompt_speech_token_len"].clone().fill_(tokens)
    speaker["prompt_speech_feat_len"] = speaker["prompt_speech_feat_len"].clone().fill_(frames)


def split_long_text(text, limit=MAX_CHUNK_CHARS):
    """Bound one inference: flow memory grows with the square of its length."""
    chunks = []
    while len(text) > limit:
        cut = max(text[:limit].rfind(mark) for mark in "。！？!?；;\n，,") + 1 or limit
        chunks.append(text[:cut])
        text = text[cut:]
    chunks.append(text)
    return [chunk for chunk in chunks if chunk.strip()] or [text]


def prepare_speaker(current_model, prompt_text, data, path):
    """Called under model_lock; cache only derived reference features in memory."""
    speaker_id = "clone_" + hashlib.sha256(data + prompt_text.encode("utf-8")).hexdigest()
    if speaker_id not in reference_cache:
        current_model.add_zero_shot_spk(prompt_text, str(path), speaker_id)
        crop_flow_prompt(current_model.frontend.spk2info[speaker_id], FLOW_PROMPT_SECONDS)
        reference_cache[speaker_id] = True
        if len(reference_cache) > 8:
            evicted, _ = reference_cache.popitem(last=False)
            current_model.frontend.spk2info.pop(evicted, None)
    reference_cache.move_to_end(speaker_id)
    return speaker_id


def encode_wav(samples, rate):
    result = BytesIO()
    with wave.open(result, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((samples.clip(-1, 1) * 32767).astype("<i2").tobytes())
    return result.getvalue()


def generate(text, prompt_text, data, cancelled=None, emit=None):
    if cancelled is not None and cancelled.is_set():
        return b""
    import torch
    import torchaudio
    runtime_temp = Path(__file__).resolve().parent / "work" / "runtime-temp"
    runtime_temp.mkdir(parents=True, exist_ok=True)
    with model_lock, tempfile.TemporaryDirectory(prefix="cosy-reference-", dir=runtime_temp) as directory:
        path = Path(directory) / "reference.wav"
        path.write_bytes(data)
        audio, rate = torchaudio.load(str(path), backend="soundfile")
        if not 3 <= audio.shape[-1] / rate <= 30:
            raise ValueError("Reference must be 3-30 seconds")
        parts = []
        with torch.inference_mode():
            speaker_id = prepare_speaker(model, prompt_text, data, path)
            if cancelled is not None and cancelled.is_set():
                return b""
            # Avoid re-extracting 16s reference features and avoid splitting short chat replies
            # into many separate inference runs inside the model's text frontend.
            for chunk in split_long_text(text):
                if cancelled is not None and cancelled.is_set():
                    break
                # CosyVoice2 changes hop length while streaming; reset it for each run.
                if emit is not None and hasattr(model.model, "token_hop_len"):
                    model.model.token_hop_len = 25
                for part in model.inference_zero_shot(chunk, "", "", zero_shot_spk_id=speaker_id,
                                                      stream=emit is not None, text_frontend=False):
                    if cancelled is not None and cancelled.is_set():
                        # Drain the upstream iterator so its internal LLM thread joins
                        # and its GPU caches are cleared before we release model_lock.
                        continue
                    samples = part["tts_speech"].detach().cpu().numpy().reshape(-1)
                    if emit is not None:
                        if samples.size:
                            emit(encode_wav(samples, model.sample_rate))
                    else:
                        parts.append(samples)
        if emit is not None:
            return b""
        if cancelled is not None and cancelled.is_set():
            return b""
        if not parts:
            raise RuntimeError("Model returned no audio")
        return encode_wav(np.concatenate(parts), model.sample_rate)


@app.post("/inference_zero_shot_stream")
async def infer_stream(tts_text: str = Form(), prompt_text: str = Form(), prompt_wav: UploadFile = File()):
    if model is None:
        raise HTTPException(503, "Model not loaded")
    if not 1 <= len(tts_text) <= 1000 or not 1 <= len(prompt_text) <= 2000:
        raise HTTPException(400, "Text too long or empty")
    data = await prompt_wav.read(10 * 1024 * 1024 + 1)
    await prompt_wav.close()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Reference too large")

    async def chunks():
        # Acquire in the response iterator so an early disconnect cannot leak the gate.
        if inference_gate.locked():
            yield json.dumps({"error":"Previous synthesis is still running"}) + "\n"
            return
        await inference_gate.acquire()
        cancelled = threading.Event()
        pending = queue.Queue(maxsize=4)

        def emit(audio):
            while not cancelled.is_set():
                try:
                    pending.put(audio, timeout=.1)
                    return
                except queue.Full:
                    pass

        async def run_job():
            try:
                await run_in_threadpool(generate, tts_text, prompt_text, data, cancelled, emit)
            finally:
                inference_gate.release()

        task = asyncio.create_task(run_job())
        try:
            while not task.done() or not pending.empty():
                try:
                    audio = pending.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(.02)
                    continue
                yield json.dumps({"audio":base64.b64encode(audio).decode("ascii")}) + "\n"
            await asyncio.shield(task)
            yield '{"done":true}\n'
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging
            logging.exception("Streaming inference failed")
            yield '{"error":"Inference failed; see service terminal"}\n'
        finally:
            cancelled.set()
            # The gate belongs to the real model worker, even after client disconnect.
            # Retrieve background exceptions without cancelling its threadpool future.
            task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)

    return StreamingResponse(chunks(), media_type="application/x-ndjson")


@app.post("/inference_zero_shot")
async def infer(request: Request, tts_text: str = Form(), prompt_text: str = Form(), prompt_wav: UploadFile = File()):
    if model is None:
        raise HTTPException(503, "Model not loaded")
    if not 1 <= len(tts_text) <= 1000 or not 1 <= len(prompt_text) <= 2000:
        raise HTTPException(400, "Text too long or empty")
    data = await prompt_wav.read(10 * 1024 * 1024 + 1)
    await prompt_wav.close()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Reference too large")
    if inference_gate.locked():
        raise HTTPException(429, "Previous synthesis is still running; retry when idle")
    await inference_gate.acquire()
    cancelled = threading.Event()

    async def run_job():
        try:
            return await run_in_threadpool(generate, tts_text, prompt_text, data, cancelled)
        finally:
            inference_gate.release()

    task = asyncio.create_task(run_job())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.25)
            if await request.is_disconnected():
                cancelled.set()
        result = await asyncio.shield(task)
        if cancelled.is_set():
            return Response(status_code=499)
    except asyncio.CancelledError:
        cancelled.set()
        # run_job keeps the gate until the real worker has stopped.
        raise
    except Exception as error:
        import logging
        logging.exception("CosyVoice inference failed")
        raise HTTPException(500, "Inference failed; see service terminal") from error
    return Response(result, media_type="audio/wav")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="models/CosyVoice2-0.5B")
    parser.add_argument("--port", type=int, default=50001)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    sys.path[:0] = [str(root / "vendor/CosyVoice"), str(root / "vendor/CosyVoice/third_party/Matcha-TTS")]
    from cosyvoice.cli.cosyvoice import AutoModel
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Inference device: {device}", flush=True)
    print("Loading local CosyVoice model; first startup may take a minute...", flush=True)
    model = AutoModel(model_dir=str((root / args.model_dir).resolve()), fp16=False, load_jit=False, load_trt=False)
    import cosy_accel
    cosy_accel.tune_runtime(model, use_graph=os.getenv("COSY_CUDA_GRAPH", "1") != "0", vram_limit_gb=VRAM_LIMIT_GB)
    warmup_current_voice(root)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)
