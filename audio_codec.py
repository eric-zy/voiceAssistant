"""Opus both ways: microphone packets in, spoken replies out. PyAV's bundled libopus, no new dependency.

Uncompressed, a listening page uploads 16 kHz PCM at 256 kbps all the time, and a cloned reply comes back as
24 kHz WAV at 384 kbps. Opus carries the same speech in roughly an eighth of that.
"""
import io
import logging
import wave

import av
import numpy as np

log = logging.getLogger("uvicorn.error")
REPLY_BITRATE = 32000
OPUS_RATES = {8000, 12000, 16000, 24000, 48000}


class OpusDecoder:
    """One per listening socket: Opus keeps state between packets, so a decoder cannot be shared."""

    def __init__(self):
        self.codec = av.CodecContext.create("opus", "r")
        self.codec.sample_rate = 16000
        self.codec.layout = "mono"
        # libopus decodes at 48 kHz whatever was asked; the turn detector and recognizer want 16 kHz PCM16.
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

    def decode(self, packet):
        """PCM16 at 16 kHz for one packet; a damaged packet yields nothing rather than failing the socket."""
        try:
            frames = [out for frame in self.codec.decode(av.Packet(packet)) for out in self.resampler.resample(frame)]
        except Exception:
            log.warning("dropped an undecodable Opus packet")
            return b""
        return b"".join(frame.to_ndarray().astype("<i2").tobytes() for frame in frames)


def to_ogg_opus(wav_bytes):
    """Ogg/Opus for a WAV reply, which browsers that announced support can decode directly."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        rate, channels = source.getframerate(), source.getnchannels()
        samples = np.frombuffer(source.readframes(source.getnframes()), "<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype("<i2")
    output = io.BytesIO()
    with av.open(output, "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=rate if rate in OPUS_RATES else 48000)
        stream.bit_rate = REPLY_BITRATE
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        frames = [frame]
        if rate not in OPUS_RATES:
            frames = av.AudioResampler(format="s16", layout="mono", rate=48000).resample(frame)
        for item in frames:
            for packet in stream.encode(item):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()
