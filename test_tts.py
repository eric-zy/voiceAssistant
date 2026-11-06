import asyncio

import edge_tts
import pytest

import app


def test_edge_audio_ignores_metadata(monkeypatch):
    monkeypatch.setattr(app, "TTS_PROVIDER", "edge")
    seen = {}
    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            seen.update(text=text, voice=voice, **kwargs)
        async def stream(self):
            yield {"type":"SentenceBoundary", "text":"你好"}
            yield {"type":"audio", "data":b"part1"}
            yield {"type":"audio", "data":b"part2"}
    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    assert asyncio.run(app.synthesize("你好")) == b"part1part2"
    assert seen["rate"] == app.TTS_RATE


def test_edge_failure_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(app, "TTS_PROVIDER", "edge")
    class FakeCommunicate:
        def __init__(self, *args, **kwargs):
            pass
        async def stream(self):
            raise OSError("private network detail")
            yield
    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    with pytest.raises(RuntimeError, match="edge-tts 合成失败"):
        asyncio.run(app.synthesize("你好"))


def test_edge_cancellation_closes_stream(monkeypatch):
    monkeypatch.setattr(app, "TTS_PROVIDER", "edge")
    state = {"closed":False}
    async def run():
        entered = asyncio.Event()
        class FakeCommunicate:
            def __init__(self, *args, **kwargs):
                pass
            async def stream(self):
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield
                finally:
                    state["closed"] = True
        monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
        task = asyncio.create_task(app.synthesize("你好"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert state["closed"]
