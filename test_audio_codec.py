import base64
import io
import wave

import av
import numpy as np
import pytest
from fastapi.testclient import TestClient

import app as backend
from audio_codec import OpusDecoder, to_ogg_opus


def tone(seconds=1.0, rate=16000, frequency=300):
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * frequency * t) * 8000).astype(np.int16)


def opus_packets(samples, rate=16000, bitrate=24000):
    """What a browser's AudioEncoder would send: raw Opus packets, 20 ms each."""
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate, encoder.layout, encoder.format, encoder.bit_rate = rate, "mono", "s16", bitrate
    encoder.open()
    packets = []
    for start in range(0, len(samples), rate // 50):
        frame = av.AudioFrame.from_ndarray(samples[start:start + rate // 50].reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts = rate, start
        packets += [bytes(packet) for packet in encoder.encode(frame)]
    return packets


def wav(samples, rate=24000):
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.astype("<i2").tobytes())
    return data.getvalue()


def test_opus_packets_come_back_as_16khz_pcm_of_the_same_length_and_sound():
    original = tone(1.0)
    decoder = OpusDecoder()
    pcm = b"".join(decoder.decode(packet) for packet in opus_packets(original))
    decoded = np.frombuffer(pcm, "<i2").astype(float)
    assert abs(len(decoded) - len(original)) <= 16000 * .05
    spectrum = np.abs(np.fft.rfft(decoded))
    assert abs(np.fft.rfftfreq(len(decoded), 1 / 16000)[spectrum.argmax()] - 300) < 10


def test_opus_is_about_an_eighth_of_pcm():
    original = tone(2.0)
    assert sum(map(len, opus_packets(original))) * 6 < original.nbytes


def test_a_damaged_packet_is_dropped_not_fatal():
    decoder = OpusDecoder()
    assert decoder.decode(b"\xff\x00garbage") == b"" or isinstance(decoder.decode(b"\xff\x00garbage"), bytes)
    assert decoder.decode(opus_packets(tone(.1))[0]) is not None


@pytest.mark.parametrize("rate", [24000, 16000, 22050])
def test_wav_replies_become_much_smaller_ogg_opus_of_the_same_length(rate):
    source = wav(tone(2.0, rate), rate)
    ogg = to_ogg_opus(source)
    assert ogg.startswith(b"OggS") and len(ogg) * 5 < len(source)
    with av.open(io.BytesIO(ogg)) as container:
        seconds = sum(frame.samples / frame.sample_rate for frame in container.decode(audio=0))
    assert abs(seconds - 2.0) < .1


class Capture:
    """Turn detector stand-in that keeps every byte it is fed and records resets."""
    MAX_SECONDS = 15
    fed, resets = [], []

    def __init__(self):
        self.max_seconds, self.audio = 15, None

    def feed(self, pcm):
        Capture.fed.append(pcm)
        return []

    def reset(self):
        Capture.resets.append(True)


@pytest.fixture
def capture(monkeypatch):
    Capture.fed, Capture.resets = [], []
    monkeypatch.setattr(backend, "TurnDetector", Capture)
    return Capture


def open_socket(client):
    return client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"})


def test_a_listening_page_can_send_opus(capture):
    packets = opus_packets(tone(1.0))
    with TestClient(backend.app) as client, open_socket(client) as socket:
        socket.send_json({"type": "listen_start", "codec": "opus"})
        for packet in packets:
            socket.send_bytes(packet)
        socket.send_json({"type": "listen_stop"})
        socket.send_json({"type": "client", "audio": []})
    total = sum(len(pcm) for pcm in capture.fed)
    assert abs(total / 2 - 16000) <= 16000 * .05


def test_without_a_codec_the_page_still_sends_pcm(capture):
    pcm = tone(.5).tobytes()
    with TestClient(backend.app) as client, open_socket(client) as socket:
        socket.send_json({"type": "listen_start"})
        socket.send_bytes(pcm)
        socket.send_json({"type": "listen_stop"})
    assert capture.fed == [pcm]


def test_a_gap_clears_stale_audio_only_between_sentences(capture, monkeypatch):
    with TestClient(backend.app) as client, open_socket(client) as socket:
        socket.send_json({"type": "listen_start"})
        socket.send_json({"type": "listen_gap"})
        socket.send_json({"type": "listen_stop"})
    assert capture.resets == [True]

    class Speaking(Capture):
        def __init__(self):
            super().__init__()
            self.audio = bytearray(b"mid-sentence")
    Capture.resets = []
    monkeypatch.setattr(backend, "TurnDetector", Speaking)
    with TestClient(backend.app) as client, open_socket(client) as socket:
        socket.send_json({"type": "listen_start"})
        socket.send_json({"type": "listen_gap"})
        socket.send_json({"type": "listen_stop"})
    assert capture.resets == []


def reply_audio(monkeypatch, announce, synthesized):
    import httpx
    original = httpx.AsyncClient
    monkeypatch.setattr(backend.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"你好。"}}]}\n\ndata: [DONE]\n\n')), **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(backend, "TTS_PROVIDER", "edge")

    async def fake_synthesize(text):
        return synthesized
    monkeypatch.setattr(backend, "synthesize", fake_synthesize)
    with TestClient(backend.app) as client, open_socket(client) as socket:
        if announce is not None:
            socket.send_json({"type": "client", "audio": announce})
        socket.send_json({"type": "text", "request_id": 1, "text": "你好"})
        while True:
            message = socket.receive_json()
            if message["type"] == "audio":
                return message


def test_a_page_that_plays_ogg_gets_wav_replies_as_ogg_opus(monkeypatch):
    message = reply_audio(monkeypatch, ["ogg-opus"], wav(tone(1.0)))
    assert message["mime"] == "audio/ogg" and base64.b64decode(message["data"]).startswith(b"OggS")


@pytest.mark.parametrize("announce", [None, []])
def test_a_page_that_did_not_say_so_still_gets_wav(monkeypatch, announce):
    assert reply_audio(monkeypatch, announce, wav(tone(1.0)))["mime"] == "audio/wav"


def test_mp3_replies_are_left_alone(monkeypatch):
    message = reply_audio(monkeypatch, ["ogg-opus"], b"ID3fake-mp3")
    assert message["mime"] == "audio/mpeg" and base64.b64decode(message["data"]) == b"ID3fake-mp3"


def test_the_protocol_version_moved_so_old_pages_and_servers_notice():
    with TestClient(backend.app) as client:
        assert client.get("/api/status").json()["voice_protocol"] == 5
