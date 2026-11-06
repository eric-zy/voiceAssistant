"""Measure warmed local synthesis only; excludes DeepSeek, ASR and playback."""
import asyncio
import json
from io import BytesIO
from pathlib import Path
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_clone import VoiceStore, synthesize_clone


async def main():
    store = VoiceStore(ROOT / 'voices')
    profile = store.get(store.settings()['profile_id'])
    samples = [('lead', '可以，我们先检查网络。'),
               ('full', '可以，我们先检查网络。确认连接正常以后，再检查接口配置和服务运行状态，一步一步排查就能找到原因。')]
    results = []
    for label, text in samples:
        started = time.perf_counter()
        audio = await synthesize_clone(profile, text, 'http://127.0.0.1:50001')
        elapsed = round(time.perf_counter()-started, 3)
        with wave.open(BytesIO(audio), 'rb') as wav:
            duration = round(wav.getnframes()/wav.getframerate(), 3)
        result = {'mode':label,'text_characters':len(text),'synthesis_seconds':elapsed,'audio_seconds':duration}
        results.append(result)
        (ROOT / 'outputs' / ('first_audio_'+label+'.wav')).write_bytes(audio)
        print(json.dumps(result), flush=True)
    (ROOT / 'outputs' / 'first_audio_benchmark.json').write_text(json.dumps(results,indent=2),encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
