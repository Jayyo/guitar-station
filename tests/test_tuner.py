"""Tuner accuracy and streaming behaviour on synthetic signals."""
import math

import numpy as np
import pytest

from guitarstation import tuner

CHUNK = 3200  # what the level reader delivers (~67 ms at 48 kHz)


def plucked(freq, secs=0.6, rate=tuner.RATE, weak_fundamental=False, noise=0.002,
            seed=1):
    """Bass-like tone: harmonics 1..10 with 1/n falloff, exponential decay."""
    t = np.arange(int(secs * rate)) / rate
    x = np.zeros_like(t)
    for n in range(1, 11):
        amp = 1.0 / n
        if weak_fundamental and n == 1:
            amp = 0.15
        x += amp * np.sin(2 * math.pi * freq * n * t + 0.3 * n)
    x *= np.exp(-t * 1.5)
    x += noise * np.random.default_rng(seed).standard_normal(len(t))
    x /= np.max(np.abs(x)) * 1.5
    return (x * 32767).astype(np.int16)


def feed_all(tracker, sig, chunk=CHUNK):
    out = []
    for i in range(0, len(sig), chunk):
        out.append(tracker.feed(sig[i:i + chunk]))
    return out


GRID = [(name, detune, weak)
        for name in ["B0", "E1", "A1", "D2", "G2", "E2", "A2", "D3", "G3", "B3", "E4"]
        for detune in (-20, -7, 0, 5, 30)
        for weak in (False, True)]


@pytest.mark.parametrize("name,detune,weak", GRID)
def test_steady_state_accuracy(name, detune, weak):
    target = tuner.midi_to_freq(tuner.parse_note(name)) * 2 ** (detune / 1200)
    readings = feed_all(tuner.PitchTracker(preset="chromatic"),
                        plucked(target, weak_fundamental=weak))
    last = readings[-1]
    assert last is not None, "no reading at the end of a sustained note"
    assert abs(tuner.cents_between(last.freq, target)) < 1.0


def test_fft_difference_matches_direct_sum():
    rng = np.random.default_rng(3)
    x = rng.standard_normal(600)
    window, tau_max = 256, 200
    seg = x[-(window + tau_max):]
    seg = seg - seg.mean()
    w = seg[:window]
    direct = np.array([np.sum((w - seg[tau:tau + window]) ** 2)
                       for tau in range(tau_max + 1)])
    # Same algebra as yin_pitch, written out on a small window.
    taus = np.arange(tau_max + 1)
    sq = np.concatenate(([0.0], np.cumsum(seg * seg)))
    e2 = sq[taus + window] - sq[taus]
    n = window + tau_max
    nfft = 1 << (n + window - 1).bit_length()
    cross = np.fft.irfft(np.fft.rfft(seg, nfft) * np.conj(np.fft.rfft(w, nfft)),
                         nfft)[:tau_max + 1]
    fft_d = np.dot(w, w) + e2 - 2 * cross
    assert np.allclose(direct, fft_d, atol=1e-6)


def test_low_b_with_weak_fundamental_is_not_an_octave_up():
    f = tuner.midi_to_freq(tuner.parse_note("B0"))
    last = feed_all(tuner.PitchTracker(preset="bass5"),
                    plucked(f, weak_fundamental=True, secs=0.8))[-1]
    assert last is not None and last.target == "B0" and last.note == "B0"


@pytest.mark.parametrize("chunk", [1, 37, 480, 4096, 20000])
def test_arbitrary_chunk_sizes(chunk):
    f = tuner.midi_to_freq(tuner.parse_note("A2"))
    sig = plucked(f, secs=0.5)
    last = feed_all(tuner.PitchTracker(preset="chromatic"), sig, chunk=chunk)[-1]
    assert last is not None and abs(tuner.cents_between(last.freq, f)) < 1.0


def test_empty_chunk_is_harmless():
    tr = tuner.PitchTracker()
    assert tr.feed(np.zeros(0, dtype=np.int16)) is None


def test_noise_floor_gives_no_reading():
    sil = (np.random.default_rng(2).standard_normal(48000) * 30).astype(np.int16)
    assert all(r is None for r in feed_all(tuner.PitchTracker(), sil))


def test_reading_expires_when_note_turns_into_noise():
    """Loud non-periodic noise after a note must not keep the old note on screen."""
    f = tuner.midi_to_freq(tuner.parse_note("E1"))
    tr = tuner.PitchTracker(preset="bass4", hold_s=0.5)
    assert feed_all(tr, plucked(f))[-1] is not None
    noise = (np.random.default_rng(5).standard_normal(48000 * 2) * 3000).astype(np.int16)
    after = feed_all(tr, noise)
    # 0.5 s hold = 24000 samples = 7.5 chunks; give it one chunk of slack
    assert all(r is None for r in after[9:])


def test_reset_drops_buffered_audio():
    f = tuner.midi_to_freq(tuner.parse_note("A1"))
    tr = tuner.PitchTracker(preset="bass4")
    feed_all(tr, plucked(f))
    tr.reset()
    # The first chunk after a reset cannot fill the analysis window.
    assert tr.feed(plucked(f)[:CHUNK]) is None


def test_string_resolution_in_bass_preset():
    # 44 Hz sits between E1 (41.2) and F1; the nearest open string is E1.
    last = feed_all(tuner.PitchTracker(preset="bass4"), plucked(44.0))[-1]
    assert last is not None
    assert last.target == "E1"
    assert last.cents == pytest.approx(tuner.cents_between(44.0, 41.2034), abs=1.0)
    assert last.direction == "far"


def test_request_applies_on_next_feed():
    tr = tuner.PitchTracker(preset="bass4", a4=440)
    tr.request(preset="guitar6", a4=432)
    tr.feed(np.zeros(10, dtype=np.int16))
    assert tr.preset == "guitar6" and tr.a4 == 432


@pytest.mark.parametrize("a4", [None, "x", float("nan"), 300, 1000])
def test_bad_reference_pitch_falls_back(a4):
    assert tuner.PitchTracker(a4=a4).a4 == tuner.DEFAULT_A4


@pytest.mark.parametrize("name,midi", [
    ("E1", 28), ("C4", 60), ("C#4", 61), ("Bb3", 58), ("A#3", 58),
    ("Cb4", 59), ("B#3", 60), ("E#4", 65), ("Fb4", 64), ("B0", 23),
])
def test_parse_note(name, midi):
    assert tuner.parse_note(name) == midi


@pytest.mark.parametrize("bad", ["", "H2", "C", "E#", "Cx4"])
def test_parse_note_rejects_garbage(bad):
    with pytest.raises(ValueError):
        tuner.parse_note(bad)


def test_note_names_round_trip():
    for midi in range(12, 100):
        assert tuner.parse_note(tuner.note_name(midi)) == midi


@pytest.mark.parametrize("period", [48000, 48624, 49601])
def test_replucked_note_does_not_smear(period):
    """Windows that straddle a new pluck read several cents off; they must not show.

    Periods that are not a multiple of the chunk size put the attack at
    different places inside a chunk, including near its end.
    """
    f = 110 * 2 ** (-4 / 1200)
    t = np.arange(period) / tuner.RATE
    one = sum((0.5 / n ** 1.2) * np.sin(2 * math.pi * f * n * t + 0.7 * n) * np.exp(-t * 0.25 * n)
              for n in range(1, 12)) * np.exp(-t * 0.4)
    sig = np.tile(one, 5)                      # abrupt restart every period
    sig = (sig / np.max(np.abs(sig)) * 0.43 * 32767).astype(np.int16)
    tr = tuner.PitchTracker(preset="guitar6")
    readings = [r for r in feed_all(tr, sig) if r is not None]
    assert len(readings) > 40
    assert all(abs(r.cents - (-4.0)) < 1.0 for r in readings)


def test_request_does_not_lose_an_earlier_change():
    tr = tuner.PitchTracker(preset="bass4", a4=440)
    tr.request(preset="guitar6")
    tr.request(a4=432)
    tr.feed(np.zeros(10, dtype=np.int16))
    assert (tr.preset, tr.a4) == ("guitar6", 432)
