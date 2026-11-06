"""Compare local WAV buffering and streaming; no LLM or credentials involved."""
import asyncio
from io import BytesIO
import json
from pathlib import Path
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from voice_clone import VoiceStore, stream_clone


async def main():
    store = VoiceStore(ROOT / 'voices')
    profile = store.get(store.settings()['profile_id'])
    text = '你好，今天我们可以聊聊有趣的事情，也可以一起安排明天的计划。'
    results = []
    for mode in ('stream_cold', 'buffered_warm', 'stream_warm'):
        start = time.perf_counter()
        times, chunks = [], []
        if mode.startswith('stream'):
            async for chunk in stream_clone(profile, text, 'http://127.0.0.1:50001'):
                times.append(round(time.perf_counter() - start, 3))
                chunks.append(chunk)
        else:
            from voice_clone import synthesize_clone
            chunks.append(await synthesize_clone(profile, text, 'http://127.0.0.1:50001'))
            times.append(round(time.perf_counter() - start, 3))
        durations, frames = [], []
        for chunk in chunks:
            with wave.open(BytesIO(chunk), 'rb') as wav:
                rate = wav.getframerate()
                durations.append(round(wav.getnframes() / rate, 3))
                frames.append(wav.readframes(wav.getnframes()))
        with wave.open(str(ROOT / 'outputs' / (mode + '.wav')), 'wb') as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(b''.join(frames))
        result = dict(mode=mode, first_audio_seconds=times[0], total_seconds=round(time.perf_counter()-start,3),
                      chunk_arrival_seconds=times, chunk_audio_seconds=durations)
        results.append(result)
        print(json.dumps(result), flush=True)
    (ROOT / 'outputs' / 'stream_benchmark.json').write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
