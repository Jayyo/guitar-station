"""Tuner: pitch detection on the interface input plus note/string math.

Pure numpy, no GUI. The detector is YIN (de Cheveigné & Kawahara 2002) with
the difference function computed through an FFT so an 8192-sample window at
48 kHz costs about a millisecond. That window is long enough to hold several
periods of a 5-string bass low B (30.9 Hz), which is what makes the cents
readout stable on the lowest strings.

    from guitarstation import tuner
    f, clarity = tuner.yin_pitch(samples)          # raw detector
    t = tuner.PitchTracker(preset="bass4")
    reading = t.feed(int16_left_channel)           # smoothed, note-resolved
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

RATE = 48000
WINDOW = 8192                    # analysis window (~170 ms)
F_MIN, F_MAX = 25.0, 1200.0      # detector range
TAU_MAX = int(RATE / F_MIN)      # longest period searched (1920 samples)
TAU_MIN = int(RATE / F_MAX)      # shortest period searched
NEED = WINDOW + TAU_MAX          # samples the detector wants per call

YIN_THRESHOLD = 0.15
GATE_DB = -45.0                  # quieter than this is hum, not a note
MIN_CLARITY = 0.6                # below this the period estimate is a guess
IN_TUNE_CENTS = 3.0
SMOOTH = 5                       # readings in the median filter

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# preset id -> (label, open strings low to high, or None for chromatic)
PRESETS = {
    "bass4": ("베이스 4현", ["E1", "A1", "D2", "G2"]),
    "bass5": ("베이스 5현", ["B0", "E1", "A1", "D2", "G2"]),
    "guitar6": ("기타 6현", ["E2", "A2", "D3", "G3", "B3", "E4"]),
    "chromatic": ("크로매틱 (가까운 음)", None),
}
DEFAULT_PRESET = "bass4"
DEFAULT_A4 = 440.0


# --------------------------------------------------------------------------
# note math
# --------------------------------------------------------------------------

def midi_to_freq(midi: float, a4: float = DEFAULT_A4) -> float:
    return a4 * 2.0 ** ((midi - 69) / 12.0)


def freq_to_midi(freq: float, a4: float = DEFAULT_A4) -> float:
    return 69 + 12 * math.log2(freq / a4)


def note_name(midi: int) -> str:
    """MIDI number -> scientific pitch name (60 -> C4, 28 -> E1)."""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def parse_note(name: str) -> int:
    """'E1' / 'C#4' / 'Bb3' -> MIDI number."""
    s = name.strip()
    i = 1
    if len(s) > 1 and s[1] in "#b":
        i = 2
    pc = NOTE_NAMES.index(s[0].upper() + ("#" if s[1:i] == "#" else ""))
    if s[1:i] == "b":
        pc = (pc - 1) % 12
    return (int(s[i:]) + 1) * 12 + pc


def cents_between(freq: float, target: float) -> float:
    return 1200.0 * math.log2(freq / target)


# --------------------------------------------------------------------------
# detector
# --------------------------------------------------------------------------

def yin_pitch(x, rate: int = RATE, window: int = WINDOW,
              tau_min: int = TAU_MIN, tau_max: int = TAU_MAX,
              threshold: float = YIN_THRESHOLD) -> tuple[float | None, float]:
    """Estimate the fundamental of the last (window + tau_max) samples.

    Returns (frequency_hz or None, clarity in 0..1). Clarity is one minus the
    normalized difference at the chosen period: ~1 for a clean sustained
    note, low for noise or the attack transient.
    """
    x = np.asarray(x, dtype=np.float64)
    n = window + tau_max
    if len(x) < n:
        return None, 0.0
    x = x[-n:]
    x = x - x.mean()
    w = x[:window]
    e1 = float(np.dot(w, w))
    if e1 <= 1e-9:
        return None, 0.0

    # d(tau) = sum_j (w[j] - x[j+tau])^2 = E(w) + E(x[tau:tau+W]) - 2 * xcorr(tau)
    taus = np.arange(tau_max + 1)
    sq = np.concatenate(([0.0], np.cumsum(x * x)))
    e2 = sq[taus + window] - sq[taus]
    nfft = 1 << (n + window - 1).bit_length()
    cross = np.fft.irfft(np.fft.rfft(x, nfft) * np.conj(np.fft.rfft(w, nfft)),
                         nfft)[:tau_max + 1]
    d = e1 + e2 - 2.0 * cross
    d[0] = 0.0

    # cumulative-mean normalized difference: d'(tau) = d(tau) * tau / sum_{1..tau} d
    cmndf = np.ones_like(d)
    cum = np.cumsum(d[1:])
    cmndf[1:] = d[1:] * taus[1:] / np.maximum(cum, 1e-12)

    lo, hi = tau_min, tau_max - 1
    below = np.nonzero(cmndf[lo:hi] < threshold)[0]
    if len(below):
        t = lo + int(below[0])
        while t + 1 < hi and cmndf[t + 1] < cmndf[t]:
            t += 1
    else:
        t = lo + int(np.argmin(cmndf[lo:hi]))
        if cmndf[t] > 0.5:
            return None, max(0.0, 1.0 - float(cmndf[t]))

    # Octave guard for bass: a weak fundamental can leave a dip at half the
    # true period that still clears the threshold. If the dip near 2*t is
    # clearly deeper, the longer period is the real one.
    t2 = 2 * t
    if t2 + 2 < hi:
        lo2, hi2 = max(lo, int(t2 * 0.97)), min(hi, int(t2 * 1.03) + 1)
        tt = lo2 + int(np.argmin(cmndf[lo2:hi2]))
        if cmndf[tt] < cmndf[t] - 0.05:
            t = tt

    # parabolic interpolation around the minimum
    if 1 <= t < len(cmndf) - 1:
        a, b, c = cmndf[t - 1], cmndf[t], cmndf[t + 1]
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0.0
        shift = max(-1.0, min(1.0, shift))
    else:
        shift = 0.0
    period = t + shift
    clarity = max(0.0, min(1.0, 1.0 - float(cmndf[t])))
    return rate / period, clarity


# --------------------------------------------------------------------------
# tracker: buffering, gating, smoothing, note resolution
# --------------------------------------------------------------------------

@dataclass
class Reading:
    freq: float          # smoothed fundamental, Hz
    clarity: float
    level_db: float
    note: str            # nearest chromatic note, e.g. "E1"
    note_cents: float    # offset from that note
    target: str          # nearest preset string (== note in chromatic mode)
    target_freq: float
    cents: float         # offset from target; what the needle shows
    in_tune: bool

    @property
    def advice(self) -> str:
        if self.in_tune:
            return "맞음"
        if abs(self.cents) > 50:
            return "다른 음"
        return ("조여" if self.cents < 0 else "풀어") + (
            "" if abs(self.cents) > 12 else " (조금)")


class PitchTracker:
    """Feed it int16 mono chunks; get a Reading or None (silence / unclear)."""

    def __init__(self, preset: str = DEFAULT_PRESET, a4: float = DEFAULT_A4):
        self.a4 = float(a4)
        self.set_preset(preset)
        self._buf = np.zeros(NEED, dtype=np.float32)
        self._filled = 0
        self._hist: list[float] = []
        self.last: Reading | None = None

    def set_preset(self, preset: str):
        self.preset = preset if preset in PRESETS else DEFAULT_PRESET
        strings = PRESETS[self.preset][1]
        self._strings = [(s, parse_note(s)) for s in strings] if strings else None

    def reset(self):
        self._hist.clear()
        self.last = None

    def feed(self, samples) -> Reading | None:
        x = np.asarray(samples, dtype=np.float32) / 32768.0
        if len(x) >= NEED:
            self._buf[:] = x[-NEED:]
        else:
            self._buf = np.roll(self._buf, -len(x))
            self._buf[-len(x):] = x
        self._filled = min(NEED, self._filled + len(x))
        if self._filled < NEED:
            return None

        peak = float(np.max(np.abs(x))) if len(x) else 0.0
        level_db = 20 * math.log10(peak) if peak > 0 else -120.0
        if level_db < GATE_DB:
            self.reset()
            return None

        freq, clarity = yin_pitch(self._buf)
        if freq is None or clarity < MIN_CLARITY or not (F_MIN <= freq <= F_MAX):
            # keep showing the last good reading briefly rather than flicker
            return self.last

        # median over the last few estimates, in log domain so it is cents-linear
        self._hist.append(math.log2(freq))
        del self._hist[:-SMOOTH]
        freq = 2.0 ** float(np.median(self._hist))

        midi_f = freq_to_midi(freq, self.a4)
        nearest = int(round(midi_f))
        note = note_name(nearest)
        note_cents = (midi_f - nearest) * 100.0

        if self._strings:
            target, tmidi = min(self._strings, key=lambda s: abs(midi_f - s[1]))
        else:
            target, tmidi = note, nearest
        target_freq = midi_to_freq(tmidi, self.a4)
        cents = cents_between(freq, target_freq)

        self.last = Reading(freq=freq, clarity=clarity, level_db=level_db,
                            note=note, note_cents=note_cents,
                            target=target, target_freq=target_freq,
                            cents=cents, in_tune=abs(cents) <= IN_TUNE_CENTS)
        return self.last
