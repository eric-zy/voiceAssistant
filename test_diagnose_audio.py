import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "work"))
from diagnose_audio import measure, verdict


def clip(level, noise=0.0, seconds=2, high=True):
    """Half a clip of tones at level, half silence, with noise added throughout."""
    count = 16000 * seconds
    rng = np.random.default_rng(1)
    time = np.arange(count) / 16000
    voice = np.sin(2 * np.pi * 300 * time) + (.2 * np.sin(2 * np.pi * 5000 * time) if high else 0)
    tone = level * voice * (np.arange(count) < count / 2)
    return (np.clip(tone + noise * rng.standard_normal(count), -1, 1) * 32767).astype("<i2").tobytes()


def test_clear_speech_passes():
    assert verdict(measure(clip(.3, .001))) == "声音正常"


def test_a_quiet_microphone_is_named():
    assert "音量太小" in verdict(measure(clip(.005, .0001)))


def test_noise_is_named():
    assert "噪声大" in verdict(measure(clip(.1, .05)))


def test_clipping_is_named():
    assert "削波" in verdict(measure(clip(2, .001)))


def test_a_telephone_band_microphone_is_named_even_when_loud_and_quiet_behind():
    assert verdict(measure(clip(.3, .0001, high=False))) == "缺高频"


def test_duration_comes_from_the_sample_count():
    assert measure(clip(.3, seconds=3))["seconds"] == 3
