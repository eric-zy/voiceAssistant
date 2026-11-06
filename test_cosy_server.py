import asyncio
from collections import OrderedDict
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

import cosy_server as server


def test_startup_warms_saved_reference_without_playback(tmp_path, monkeypatch):
    from voice_clone import VoiceStore
    store = VoiceStore(tmp_path / 'voices')
    profile_id = 'a' * 32
    folder = store.root / profile_id
    folder.mkdir(parents=True)
    import json
    (folder / 'reference.wav').write_bytes(b'reference')
    (folder / 'profile.json').write_text(json.dumps({'name':'test','prompt_text':'reference words'}))
    store.save_settings({'provider':'cosyvoice','profile_id':profile_id})
    calls = []
    monkeypatch.setattr(server,'warmup_status','not_started')
    monkeypatch.setattr(server,'generate',lambda *args: calls.append(args))
    server.warmup_current_voice(tmp_path)
    assert server.warmup_status == 'ready'
    assert calls == [('你好，很高兴和你聊天。','reference words',b'reference')]


def test_busy_service_does_not_queue_stale_requests(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, responses = [], []
    def fake_generate(text, prompt, data, cancelled):
        calls.append(text)
        entered.set()
        release.wait(5)
        return b"WAV-test"
    monkeypatch.setattr(server, "model", SimpleNamespace(sample_rate=24000))
    monkeypatch.setattr(server, "inference_gate", asyncio.Semaphore(1))
    monkeypatch.setattr(server, "generate", fake_generate)
    with TestClient(server.app) as client:
        def submit(text):
            return client.post('/inference_zero_shot',data={'tts_text':text,'prompt_text':'prompt'},files={'prompt_wav':('reference.wav',b'placeholder')})
        worker = threading.Thread(target=lambda: responses.append(submit('first')))
        worker.start()
        try:
            assert entered.wait(3)
            assert client.get('/health').json()['busy'] is True
            started = time.monotonic()
            response = submit('second')
            assert response.status_code == 429
            assert time.monotonic() - started < 1
        finally:
            release.set()
            worker.join(5)
        assert calls == ['first']
        assert responses[0].status_code == 200
        assert client.get('/health').json()['busy'] is False


def test_long_text_splits_at_punctuation_within_limit():
    text = '今天天气不错，' * 30
    chunks = server.split_long_text(text)
    assert ''.join(chunks) == text
    assert len(chunks) > 1
    assert all(len(chunk) <= server.MAX_CHUNK_CHARS and chunk.endswith('，') for chunk in chunks)


def test_text_without_punctuation_is_still_bounded():
    chunks = server.split_long_text('字' * 170)
    assert [len(chunk) for chunk in chunks] == [80, 80, 10]


def test_short_text_stays_one_inference():
    assert server.split_long_text('你好。') == ['你好。']


def test_blank_tail_does_not_start_an_inference():
    assert server.split_long_text('字' * 79 + '。' + '\n') == ['字' * 79 + '。']


def test_reference_features_are_cached_and_bounded(monkeypatch):
    monkeypatch.setattr(server, 'reference_cache', OrderedDict())
    monkeypatch.setattr(server, 'crop_flow_prompt', lambda speaker, seconds: None)
    calls = []
    model = SimpleNamespace(frontend=SimpleNamespace(spk2info={}))
    def add(text, path, speaker_id):
        calls.append(speaker_id)
        model.frontend.spk2info[speaker_id] = 'features'
    model.add_zero_shot_spk = add
    first = server.prepare_speaker(model,'words',b'first','unused.wav')
    assert server.prepare_speaker(model,'words',b'first','unused.wav') == first
    assert len(calls) == 1
    for i in range(8):
        server.prepare_speaker(model,'words',str(i).encode(),'unused.wav')
    assert len(server.reference_cache) == 8
    assert first not in model.frontend.spk2info


def test_stream_delivers_before_inference_finishes_and_releases_gate(monkeypatch):
    import json
    import base64
    release = threading.Event()
    def fake_generate(text, prompt, data, cancelled, emit):
        emit(b'first-wav')
        release.wait(3)
        emit(b'second-wav')
    monkeypatch.setattr(server, 'model', SimpleNamespace(sample_rate=24000))
    monkeypatch.setattr(server, 'inference_gate', asyncio.Semaphore(1))
    monkeypatch.setattr(server, 'generate', fake_generate)
    async def run():
        from starlette.datastructures import UploadFile
        from io import BytesIO
        response = await server.infer_stream('hello', 'prompt', UploadFile(BytesIO(b'reference')))
        iterator = response.body_iterator
        first = await asyncio.wait_for(anext(iterator), 2)
        assert base64.b64decode(json.loads(first)['audio']) == b'first-wav'
        assert not release.is_set()
        assert server.inference_gate.locked()
        release.set()
        remaining = [json.loads(chunk) async for chunk in iterator]
        assert base64.b64decode(remaining[0]['audio']) == b'second-wav'
        assert remaining[-1] == {'done':True}
        assert not server.inference_gate.locked()
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_stream_disconnect_keeps_gate_until_worker_stops(monkeypatch):
    release, stopped = threading.Event(), threading.Event()
    def fake_generate(text, prompt, data, cancelled, emit):
        emit(b'first')
        release.wait(3)
        assert cancelled.is_set()
        stopped.set()
    monkeypatch.setattr(server, 'model', SimpleNamespace(sample_rate=24000))
    monkeypatch.setattr(server, 'inference_gate', asyncio.Semaphore(1))
    monkeypatch.setattr(server, 'generate', fake_generate)
    async def run():
        from starlette.datastructures import UploadFile
        from io import BytesIO
        response = await server.infer_stream('hello', 'prompt', UploadFile(BytesIO(b'reference')))
        iterator = response.body_iterator
        await asyncio.wait_for(anext(iterator), 2)
        await iterator.aclose()
        assert server.inference_gate.locked()
        release.set()
        for _ in range(100):
            if not server.inference_gate.locked():
                break
            await asyncio.sleep(.02)
        assert stopped.is_set()
        assert not server.inference_gate.locked()
    try:
        asyncio.run(run())
    finally:
        release.set()
