"""Synthetic accuracy checks for the tuner. Run: python3 tests/test_tuner.py"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
from guitarstation import tuner  # noqa: E402


def plucked(freq, secs=0.6, rate=tuner.RATE, weak_fundamental=False, noise=0.002):
    """Bass-like tone: harmonics 1..10 with 1/n falloff, exponential decay."""
    t = np.arange(int(secs * rate)) / rate
    x = np.zeros_like(t)
    for n in range(1, 11):
        amp = 1.0 / n
        if weak_fundamental and n == 1:
            amp = 0.15
        x += amp * np.sin(2 * math.pi * freq * n * t + 0.3 * n)
    x *= np.exp(-t * 1.5)
    x += noise * np.random.default_rng(1).standard_normal(len(t))
    x /= np.max(np.abs(x)) * 1.5
    return (x * 32767).astype(np.int16)


def detect(sig, preset="chromatic"):
    """Run the tracker over 3200-frame chunks like LevelReader does; last reading."""
    tr = tuner.PitchTracker(preset=preset)
    r = None
    for i in range(0, len(sig), 3200):
        r = tr.feed(sig[i:i + 3200]) or r
    return r


fails = 0
print(f"{'note':>5} {'target Hz':>9} {'detune':>7} {'got Hz':>8} {'err ¢':>6}  weak_fund")
for name in ["B0", "E1", "A1", "D2", "G2", "E2", "A2", "D3", "G3", "B3", "E4"]:
    for detune in (-20, -7, 0, +5, +30):
        for weak in (False, True):
            target = tuner.midi_to_freq(tuner.parse_note(name)) * 2 ** (detune / 1200)
            r = detect(plucked(target, weak_fundamental=weak))
            if r is None:
                print(f"{name:>5} {target:9.2f} {detune:+7d} {'NONE':>8}")
                fails += 1
                continue
            err = tuner.cents_between(r.freq, target)
            bad = abs(err) > 1.0
            fails += bad
            if bad or detune == 0:
                print(f"{name:>5} {target:9.2f} {detune:+7d} {r.freq:8.2f} {err:+6.2f}  {weak}{'  <-- FAIL' if bad else ''}")

# silence / noise must not produce a reading
tr = tuner.PitchTracker()
sil = (np.random.default_rng(2).standard_normal(48000) * 30).astype(np.int16)
got = [tr.feed(sil[i:i + 3200]) for i in range(0, len(sil), 3200)]
assert all(g is None for g in got), "noise floor produced a reading"

# string resolution in bass4 preset: 44 Hz should map to E1, +113 cents
r = detect(plucked(44.0), preset="bass4")
assert r is None or r.target == "E1", r
# note names
assert tuner.note_name(28) == "E1" and tuner.parse_note("E1") == 28
assert tuner.parse_note("Bb3") == tuner.parse_note("A#3")
print("fails:", fails)
sys.exit(1 if fails else 0)
