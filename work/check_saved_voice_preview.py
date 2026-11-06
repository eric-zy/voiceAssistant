"""Exercise the current saved custom voice through the real main-app preview route."""
from io import BytesIO
from pathlib import Path
import sys
import time
import wave

root = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from fastapi.testclient import TestClient
import app

if app.TTS_PROVIDER != 'cosyvoice':
    raise SystemExit('Select a saved custom voice first')
start=time.monotonic()
with TestClient(app.app) as client:
    response=client.post('/api/voice/preview',headers={'origin':'http://127.0.0.1:8765'})
if response.status_code != 200:
    print('Preview HTTP',response.status_code,response.text[:300])
    raise SystemExit(1)
output=root/'outputs/custom_voice_fixed.wav'
output.write_bytes(response.content)
with wave.open(BytesIO(response.content),'rb') as audio:
    print('seconds',round(audio.getnframes()/audio.getframerate(),2),'rate',audio.getframerate())
print('elapsed',round(time.monotonic()-start,2),'saved',output)
