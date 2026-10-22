"""Tuner: pitch detection on the interface input plus note/string math.

Pure numpy, no GUI. The detector is YIN (de Cheveigné & Kawahara 2002) with
the difference function computed through an FFT so an 8192-sample window at
48 kHz costs about a millisecond. That window is long enough to hold several
periods of a 5-string bass low B (30.9 Hz), which is what makes the cents
readout stable on the lowest strings.

    from guitarstation import tuner
    f, clarity = tuner.yin_pitch(samples)          # raw detector
    t = tuner.PitchTracker(preset="bass4")
    reading = t.feed(int16_chunk)                  # smoothed, note-resolved
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import numpy as np

RATE = 48000
WINDOW = 8192                    # analysis window (~170 ms)
F_MIN, F_MAX = 25.0, 1200.0      # detector range
TAU_MAX = int(RATE / F_MIN)      # longest period searched (1920 samples)
TAU_MIN = int(RATE / F_MAX)      # shortest period searched
NEED = WINDOW + TAU_MAX          # samples the detector wants per call

YIN_THRESHOLD = 0.15
GATE_OPEN_DB = -45.0             # a note has to peak above this to be analysed
GATE_CLOSE_DB = -50.0            # ...and analysis stops once it falls below this
MIN_CLARITY = 0.6                # below this the period estimate is a guess
IN_TUNE_CENTS = 3.0
SMOOTH = 5                       # readings in the median filter
HOLD_S = 1.0                     # keep the last good reading this long
ONSET_RATIO = 2.0                # envelope jumping by this factor = new pluck
ONSET_BLOCK = 960                # newest samples (at least 20 ms, or the chunk) ...
ONSET_LOOKBACK = 2400            # ... compared with the 50 ms before them

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_LETTER_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# preset id -> open strings low to high, or None for chromatic
PRESETS = {
    "bass4": ["E1", "A1", "D2", "G2"],
    "bass5": ["B0", "E1", "A1", "D2", "G2"],
    "guitar6": ["E2", "A2", "D3", "G3", "B3", "E4"],
    "chromatic": None,
}
DEFAULT_PRESET = "bass4"
DEFAULT_A4 = 440.0
A4_RANGE = (415.0, 466.0)


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
    """'E1' / 'C#4' / 'Bb3' / 'Cb4' -> MIDI number.

    The octave number belongs to the letter, so Cb4 is B3 (59) and B#3 is C4 (60).
    """
    s = name.strip()
    if len(s) < 2 or s[0].upper() not in _LETTER_PC:
        raise ValueError(f"not a note name: {name!r}")
    i, acc = 1, 0
    while i < len(s) and s[i] in "#b":
        acc += 1 if s[i] == "#" else -1
        i += 1
    try:
        octave = int(s[i:])
    except ValueError:
        raise ValueError(f"not a note name: {name!r}") from None
    return (octave + 1) * 12 + _LETTER_PC[s[0].upper()] + acc


def cents_between(freq: float, target: float) -> float:
    return 1200.0 * math.log2(freq / target)


# --------------------------------------------------------------------------
# detector
# --------------------------------------------------------------------------

def yin_pitch(x, rate: int = RATE, window: int = WINDOW,
              f_min: float = F_MIN, f_max: float = F_MAX,
              threshold: float = YIN_THRESHOLD) -> tuple[float | None, float]:
    """Estimate the fundamental of the last (window + rate/f_min) samples.

    Returns (frequency_hz or None, clarity in 0..1). Clarity is one minus the
    normalized difference at the chosen period: ~1 for a clean sustained
    note, low for noise or the attack transient.
    """
    tau_max = int(rate / f_min)
    tau_min = max(2, int(rate / f_max))
    x = np.asarray(x, dtype=np.float64).ravel()
    n = window + tau_max
    if len(x) < n or tau_min >= tau_max - 1:
        return None, 0.0
    x = x[-n:]
    if not np.all(np.isfinite(x)):
        return None, 0.0
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
    # Rounding can leave tiny negative values where the true difference is 0.
    d = np.maximum(e1 + e2 - 2.0 * cross, 0.0)
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
    # clearly deeper, the longer period is the real one. This is a heuristic:
    # a tone made only of even harmonics is genuinely ambiguous.
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
    def direction(self) -> str:
        """'ok', 'up' (tighten), 'down' (loosen) or 'far' (another note)."""
        if self.in_tune:
            return "ok"
        if abs(self.cents) > 50:
            return "far"
        return "up" if self.cents < 0 else "down"


class PitchTracker:
    """Feed it int16 mono chunks; get a Reading or None (silence / unclear).

    Owned by one thread (the audio reader). Other threads change the preset or
    reference pitch through request(), which is applied on the next feed().
    """

    def __init__(self, preset: str = DEFAULT_PRESET, a4: float = DEFAULT_A4,
                 rate: int = RATE, hold_s: float = HOLD_S):
        self.rate = rate
        self.hold_samples = int(hold_s * rate)
        self._lock = threading.Lock()
        self._apply(preset, a4)
        self._wanted = (self.preset, self.a4)   # guarded by _lock
        self.reset()

    # -- settings ---------------------------------------------------------

    def request(self, preset: str | None = None, a4: float | None = None):
        """Thread-safe settings change; takes effect on the next feed()."""
        with self._lock:
            p, a = self._wanted
            self._wanted = (preset or p, a4 if a4 is not None else a)

    def _apply(self, preset: str, a4: float):
        self.preset = preset if preset in PRESETS else DEFAULT_PRESET
        try:
            a4 = float(a4)
        except (TypeError, ValueError):
            a4 = DEFAULT_A4
        lo, hi = A4_RANGE
        self.a4 = a4 if math.isfinite(a4) and lo <= a4 <= hi else DEFAULT_A4
        strings = PRESETS[self.preset]
        self._strings = [(s, parse_note(s)) for s in strings] if strings else None

    def reset(self):
        """Forget everything, including the audio buffer (new input, replug)."""
        self._buf = np.zeros(NEED, dtype=np.float32)
        self._filled = 0
        self._hist: list[float] = []
        self._gate_open = False
        self._settle_until = 0       # no new estimates before this sample
        self._clock = 0              # samples fed so far
        self._last_ok_at = None      # self._clock at the last good detection
        self.last: Reading | None = None

    # -- streaming --------------------------------------------------------

    def feed(self, samples) -> Reading | None:
        with self._lock:
            wanted = self._wanted
        if wanted != (self.preset, self.a4):
            self._apply(*wanted)
            with self._lock:
                # store what _apply() accepted, unless a newer request came in
                if self._wanted == wanted:
                    self._wanted = (self.preset, self.a4)
            self._hist.clear()
            self.last = None

        x = np.asarray(samples, dtype=np.float32).ravel() / 32768.0
        if len(x) == 0:
            return self._current()
        self._clock += len(x)
        if len(x) >= NEED:
            self._buf[:] = x[-NEED:]
        else:
            self._buf = np.roll(self._buf, -len(x))
            self._buf[-len(x):] = x
        self._filled = min(NEED, self._filled + len(x))
        if self._filled < NEED:
            return None

        peak = float(np.max(np.abs(x)))
        level_db = 20 * math.log10(peak) if peak > 0 else -120.0
        threshold = GATE_CLOSE_DB if self._gate_open else GATE_OPEN_DB
        self._gate_open = level_db >= threshold
        # Compare envelopes over fixed spans of the buffer so the result does
        # not depend on how the caller chunks the stream.
        blk = min(max(len(x), ONSET_BLOCK), NEED - ONSET_LOOKBACK)
        recent = float(np.max(np.abs(self._buf[-blk:])))
        before = float(np.max(np.abs(self._buf[-blk - ONSET_LOOKBACK:-blk])))
        onset = recent > ONSET_RATIO * before
        if not self._gate_open:
            self._hist.clear()
            self.last = None
            return None
        if onset:
            # A window that straddles a new pluck mixes the old note with the
            # attack and reads several cents off. Start the median over and
            # wait until the analysis window lies entirely after the onset.
            tail = np.abs(self._buf[-blk:])
            first = int(np.argmax(tail > ONSET_RATIO * before))
            onset_at = self._clock - blk + first
            self._hist.clear()
            self._settle_until = max(self._settle_until, onset_at + NEED)
        if self._clock < self._settle_until:
            return self._current()

        freq, clarity = yin_pitch(self._buf, rate=self.rate)
        if freq is None or clarity < MIN_CLARITY or not (F_MIN <= freq <= F_MAX):
            # Keep showing the last good reading briefly rather than flicker,
            # but never past the hold time.
            return self._current()

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

        self._last_ok_at = self._clock
        self.last = Reading(freq=freq, clarity=clarity, level_db=level_db,
                            note=note, note_cents=note_cents,
                            target=target, target_freq=target_freq,
                            cents=cents, in_tune=abs(cents) <= IN_TUNE_CENTS)
        return self.last

    def _current(self) -> Reading | None:
        if self.last is None or self._last_ok_at is None:
            return None
        if self._clock - self._last_ok_at > self.hold_samples:
            self.last = None
            self._hist.clear()
            return None
        return self.last
