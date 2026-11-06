"""Decides from a live microphone stream when someone starts and stops talking."""
from collections import deque
import os

import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
CONTEXT_SAMPLES = 64


class SileroStream:
    """Silero VAD, bundled with faster-whisper, keeping its state across frames."""

    def __init__(self):
        from faster_whisper.utils import get_assets_path
        from faster_whisper.vad import SileroVADModel
        self.session = SileroVADModel(os.path.join(get_assets_path(), "silero_vad_v6.onnx")).session
        self.h = np.zeros((1, 1, 128), dtype="float32")
        self.c = np.zeros((1, 1, 128), dtype="float32")
        self.context = np.zeros(CONTEXT_SAMPLES, dtype="float32")

    def __call__(self, frame):
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768
        window = np.concatenate([self.context, samples]).reshape(1, -1)
        output, self.h, self.c = self.session.run(None, {"input": window, "h": self.h, "c": self.c})
        self.context = samples[-CONTEXT_SAMPLES:]
        return float(output.reshape(-1)[0])


def measure_speech(pcm):
    """Return the peak speech chance and the share of frames that count as speech."""
    model = SileroStream()
    chances = [model(pcm[i:i + FRAME_BYTES]) for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
    if not chances:
        return 0.0, 0.0
    return max(chances), sum(chance >= TurnDetector.START_THRESHOLD for chance in chances) / len(chances)


class TurnDetector:
    """Turns audio into (event, audio) pairs: start, sustained, end, dropped and meter.

    Starting needs several likely frames in a short window, not an unbroken run:
    a real voice in a real room flickers around the threshold. meter carries the
    highest recent speech probability, so a live microphone can be diagnosed.
    """
    START_THRESHOLD = .4
    START_WINDOW_FRAMES = 10
    START_FRAMES = 4
    STOP_THRESHOLD = .35
    METER_FRAMES = 15
    # LiveKit's default for treating speech as a deliberate interruption.
    SUSTAINED_SECONDS = .5
    MIN_SPEECH_SECONDS = .3
    END_SILENCE_SECONDS = .6
    PRE_ROLL_SECONDS = .3
    MAX_SECONDS = 15

    def __init__(self, probability=None):
        self.probability = probability or SileroStream()
        self.max_seconds = self.MAX_SECONDS
        self.pending = bytearray()
        self.pre_roll = deque(maxlen=round(self.PRE_ROLL_SECONDS / FRAME_SECONDS))
        self.recent = deque(maxlen=self.START_WINDOW_FRAMES)
        self.meter_frames = 0
        self.meter_peak = 0.0
        self.reset()

    def reset(self):
        self.audio = None
        self.speech = self.silence = 0.0
        self.sustained = False
        self.pre_roll.clear()
        self.recent.clear()

    def feed(self, pcm):
        self.pending.extend(pcm)
        events = []
        while len(self.pending) >= FRAME_BYTES:
            frame = bytes(self.pending[:FRAME_BYTES])
            del self.pending[:FRAME_BYTES]
            events.extend(self._frame(frame))
        return events

    def _frame(self, frame):
        chance = self.probability(frame)
        meter = self._meter(chance)
        if self.audio is None:
            return meter + self._waiting(frame, chance)
        self.audio.extend(frame)
        if chance >= self.STOP_THRESHOLD:
            self.speech += FRAME_SECONDS
            self.silence = 0.0
        else:
            self.silence += FRAME_SECONDS
        events = meter
        if not self.sustained and self.speech >= self.SUSTAINED_SECONDS:
            self.sustained = True
            events.append(("sustained", bytes(self.audio)))
        too_long = len(self.audio) / 2 / SAMPLE_RATE >= self.max_seconds
        if self.silence >= self.END_SILENCE_SECONDS or too_long:
            kept = self.speech >= self.MIN_SPEECH_SECONDS
            events.append(("end", bytes(self.audio)) if kept else ("dropped", None))
            self.reset()
        return events

    def _waiting(self, frame, chance):
        self.pre_roll.append(frame)
        self.recent.append(chance >= self.START_THRESHOLD)
        voiced = sum(self.recent)
        if voiced < self.START_FRAMES:
            return []
        self.audio = bytearray(b"".join(self.pre_roll))
        self.speech = voiced * FRAME_SECONDS
        self.pre_roll.clear()
        self.recent.clear()
        return [("start", None)]

    def _meter(self, chance):
        self.meter_peak = max(self.meter_peak, chance)
        self.meter_frames += 1
        if self.meter_frames < self.METER_FRAMES:
            return []
        peak, self.meter_frames, self.meter_peak = self.meter_peak, 0, 0.0
        return [("meter", peak)]
