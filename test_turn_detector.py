import pytest

from turn_detector import FRAME_BYTES, FRAME_SECONDS, SileroStream, TurnDetector, measure_speech


def frames(seconds):
    return round(seconds / FRAME_SECONDS)


def run(*parts):
    """Each part is (speech probability, seconds); returns events with audio seconds."""
    script = iter([chance for chance, seconds in parts for _ in range(frames(seconds))])
    detector = TurnDetector(lambda frame: next(script))
    events = []
    for chance, seconds in parts:
        for _ in range(frames(seconds)):
            events += detector.feed(bytes(FRAME_BYTES))
    return [(name, None if audio is None else len(audio) / FRAME_BYTES * FRAME_SECONDS) for name, audio in events if name != "meter"]


def names(events):
    return [name for name, _ in events]


def test_a_sentence_starts_is_sustained_and_ends_after_a_pause():
    events = run((.05, 1), (.9, 1), (.05, 1))
    assert names(events) == ["start", "sustained", "end"]


def test_audio_from_just_before_speech_is_kept():
    _, _, (_, seconds) = run((.05, 1), (.9, 1), (.05, 1))
    assert 1 + TurnDetector.END_SILENCE_SECONDS + .1 < seconds < 2


def test_a_short_breath_inside_a_sentence_does_not_end_it():
    events = run((.9, .6), (.05, .3), (.9, .6), (.05, 1))
    assert names(events) == ["start", "sustained", "end"]


def test_quiet_syllables_do_not_read_as_silence():
    events = run((.9, .5), (.4, 1), (.9, .3), (.05, 1))
    assert names(events) == ["start", "sustained", "end"]


@pytest.mark.parametrize("chance,seconds", [(.05, 5), (.3, 5), (.9, .09)])
def test_noise_and_clicks_never_start_a_turn(chance, seconds):
    assert run((chance, seconds), (.05, 1)) == []


def test_isolated_spikes_never_start_a_turn():
    assert run(*[(.9, .03), (.05, .3)] * 20) == []


def test_a_voice_flickering_around_the_threshold_still_starts():
    assert names(run((.05, .5), *[(.6, .06), (.2, .06)] * 8, (.05, 1)))[0] == "start"


def test_the_live_speech_chance_is_reported_about_twice_a_second():
    script = iter([.1] * 20 + [.8] + [.1] * 100)
    detector = TurnDetector(lambda frame: next(script))
    meters = [value for name, value in detector.feed(bytes(FRAME_BYTES * 45)) if name == "meter"]
    assert meters == [pytest.approx(.1), pytest.approx(.8), pytest.approx(.1)]


def test_a_blip_that_barely_starts_is_dropped():
    assert names(run((.9, .2), (.05, 1))) == ["start", "dropped"]


def test_endless_sound_is_cut_at_the_limit():
    events = run((.9, 16))
    assert names(events)[:3] == ["start", "sustained", "end"]
    assert events[2][1] == pytest.approx(TurnDetector.MAX_SECONDS, abs=.2)


def test_the_limit_can_shrink_while_the_assistant_talks():
    script = iter([.9] * 1000)
    detector = TurnDetector(lambda frame: next(script))
    detector.max_seconds = 2
    events = []
    for _ in range(frames(3)):
        events += detector.feed(bytes(FRAME_BYTES))
    assert "end" in [name for name, _ in events]


def test_browser_chunks_need_not_line_up_with_model_frames():
    seen = []
    detector = TurnDetector(lambda frame: seen.append(len(frame)) or 0)
    for _ in range(3):
        detector.feed(bytes(1364))
    assert seen == [FRAME_BYTES] * 3


def test_reset_abandons_the_sentence_in_progress():
    detector = TurnDetector(lambda frame: .9)
    detector.feed(bytes(FRAME_BYTES * 10))
    detector.reset()
    assert detector.audio is None
    assert detector.feed(bytes(FRAME_BYTES * 2)) == []


def test_bundled_silero_model_runs_as_a_stream():
    model = SileroStream()
    chances = [model(bytes(FRAME_BYTES)) for _ in range(5)]
    assert all(0 <= chance < .5 for chance in chances)


def test_silence_measures_as_no_speech():
    assert measure_speech(bytes(FRAME_BYTES * 20))[1] == 0
    assert measure_speech(b"") == (0.0, 0.0)

