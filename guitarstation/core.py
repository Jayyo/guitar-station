"""Core logic for guitar-station: config, the monitoring unit, level metering.

No GUI dependencies, so every piece can be exercised from a shell:

    python3 -c "from guitarstation import core, pw; print(core.unit_state(), pw.snapshot())"

Monitoring runs as a transient systemd user service (`guitarstation.monitor`
inside it), so it keeps playing after the window is closed and the CLI can
switch it off.
"""

from __future__ import annotations

import array
import fcntl
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import pw

APP_ID = "io.github.jayyo.GuitarStation"
# Overridable so a test run never touches a real monitoring session.
UNIT = os.environ.get("GUITAR_STATION_UNIT", "guitar-monitor")
UNIT_SERVICE = f"{UNIT}.service"

RATE = 48000
METER_FLOOR_DB = -60.0
CLIP_RAW = 32700          # s16 sample magnitude we call clipping
HOT_DB = -3.0             # too hot, headroom nearly gone
GOOD_LO_DB = -14.0        # comfortable recording window
GOOD_HI_DB = -5.0

LATENCY_CHOICES = [0, 128, 256, 512, 1024]   # 0 = leave it to PipeWire
AUTO_STOP_MIN = 10

DEFAULT_CONFIG = {
    "input": None,          # pw selector dict of the capture node
    "channel": None,        # channel position on that node, e.g. "FL"
    "channel_index": None,  # fallback when a profile change renames positions
    "output": None,         # pw selector dict of a pinned sink, None = follow default
    "latency": 256,         # buffer request in samples, 0 = PipeWire default
    "auto_stop": False,
    "tuner_preset": "bass4",
    "a4": 440.0,
}

F_SETPIPE_SZ = getattr(fcntl, "F_SETPIPE_SZ", 1031)

# Passed into the unit so it talks to the same PipeWire instance as the app.
_PASSTHROUGH_ENV = ("PIPEWIRE_RUNTIME_DIR", "PIPEWIRE_REMOTE")


def _run(argv, timeout=15):
    """Run a command, return (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "guitar-station" / "config.json"


def _clean_selector(v) -> dict | None:
    if not isinstance(v, dict) or not isinstance(v.get("node"), str) or not v["node"]:
        return None
    out = {"node": v["node"]}
    pcm = v.get("pcm")
    if isinstance(v.get("device"), str) and isinstance(pcm, int) and not isinstance(pcm, bool):
        out.update(device=v["device"], pcm=pcm)
    return out


def validate_config(raw) -> dict:
    """Merge whatever is on disk into the defaults, dropping anything malformed."""
    from . import tuner
    cfg = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    cfg["input"] = _clean_selector(raw.get("input"))
    cfg["output"] = _clean_selector(raw.get("output"))
    if isinstance(raw.get("channel"), str):
        cfg["channel"] = raw["channel"]
    index = raw.get("channel_index")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < 64:
        cfg["channel_index"] = index
    latency = raw.get("latency")
    if isinstance(latency, int) and not isinstance(latency, bool) and latency in LATENCY_CHOICES:
        cfg["latency"] = latency
    if isinstance(raw.get("auto_stop"), bool):
        cfg["auto_stop"] = raw["auto_stop"]
    preset = raw.get("tuner_preset")
    if isinstance(preset, str) and preset in tuner.PRESETS:
        cfg["tuner_preset"] = preset
    a4 = raw.get("a4")
    if isinstance(a4, (int, float)) and not isinstance(a4, bool):
        try:
            a4 = float(a4)
        except OverflowError:
            a4 = math.nan
        lo, hi = tuner.A4_RANGE
        if math.isfinite(a4) and lo <= a4 <= hi:
            cfg["a4"] = a4
    return cfg


def load_config() -> dict:
    try:
        raw = json.loads(config_path().read_text())
    except (OSError, ValueError):
        raw = None
    return validate_config(raw)


def save_config(cfg: dict):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    tmp.replace(path)


# --------------------------------------------------------------------------
# monitoring unit
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MonitorPlan:
    """Everything the unit needs, resolved on the caller's side right now."""
    node: str
    port: str
    channel: str
    channel_index: int
    device: str | None
    pcm: int | None
    sink: str | None          # pinned sink node name, None = follow default
    latency: int              # samples, 0 = no request


class PlanError(Exception):
    """A user-facing reason why monitoring can't start (message is an id)."""


def make_plan(cfg: dict, graph: pw.Graph | None) -> MonitorPlan:
    if graph is None:
        raise PlanError("no-pipewire")
    src = graph.find_source(cfg.get("input"))
    if src is None:
        raise PlanError("no-input")
    ch = src.channel(cfg.get("channel"), cfg.get("channel_index"))
    if ch is None:
        raise PlanError("no-channel")
    sink = None
    if cfg.get("output"):
        node = graph.find_sink(cfg["output"])
        if node is None:
            raise PlanError("no-output")
        sink = node.name
    return MonitorPlan(node=src.name, port=ch.port, channel=ch.position,
                       channel_index=ch.index, device=src.device, pcm=src.pcm,
                       sink=sink, latency=int(cfg.get("latency") or 0))


def _package_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def unit_argv(plan: MonitorPlan) -> list[str]:
    argv = ["systemd-run", "--user", "--quiet", f"--unit={UNIT}", "--collect",
            "--service-type=exec",
            "--property=KillSignal=SIGTERM", "--property=TimeoutStopSec=5",
            f"--setenv=PYTHONPATH={_package_root()}"]
    for key in _PASSTHROUGH_ENV:
        if os.environ.get(key):
            argv.append(f"--setenv={key}={os.environ[key]}")
    argv += ["--", sys.executable, "-m", "guitarstation.monitor",
             "--name", UNIT, "--node", plan.node, "--port", plan.port,
             "--channel", plan.channel, "--channel-index", str(plan.channel_index)]
    if plan.device is not None and plan.pcm is not None:
        argv += ["--device", plan.device, "--pcm", str(plan.pcm)]
    if plan.sink:
        argv += ["--sink", plan.sink]
    if plan.latency:
        argv += ["--latency", str(plan.latency)]
    return argv


def unit_state() -> str:
    """systemd ActiveState of the unit: active, inactive, failed, activating, ..."""
    rc, out, _ = _run(["systemctl", "--user", "show", "--property=ActiveState",
                       "--value", UNIT_SERVICE], timeout=10)
    return out.strip() if rc == 0 and out.strip() else "unknown"


def is_active() -> bool:
    return unit_state() in ("active", "activating", "reloading")


def _routed(plan: MonitorPlan) -> bool:
    """Both loopback inputs fed by the chosen port, and the output going somewhere."""
    rc, out, _ = _run(["pw-link", "-l"])
    if rc != 0:
        return False
    have = pw.links(out)
    src = f"{plan.node}:{plan.port}"
    inputs_ok = all((src, f"input.{UNIT}:input_{side}") in have for side in ("FL", "FR"))
    output_ok = any(o.startswith(f"output.{UNIT}:") for o, _i in have)
    return inputs_ok and output_ok


def start(plan: MonitorPlan, timeout: float = 5.0) -> tuple[bool, str]:
    """(Re)start monitoring. Returns (ok, message id)."""
    stop()
    # A unit that failed before --collect was used would block the name.
    _run(["systemctl", "--user", "reset-failed", UNIT_SERVICE])
    rc, _, err = _run(unit_argv(plan))
    if rc != 0:
        return False, "start-failed:" + (err.strip().splitlines() or [""])[-1][:160]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_active():
            return False, "exited"
        if _routed(plan):
            return True, "on"
        time.sleep(0.2)
    stop()
    return False, "not-routed"


def stop() -> tuple[bool, str]:
    rc, _, err = _run(["systemctl", "--user", "stop", UNIT_SERVICE], timeout=15)
    if rc in (0, 5):                 # 5: unit not loaded, i.e. already off
        return True, "off"
    return False, "stop-failed:" + (err.strip().splitlines() or [""])[-1][:160]


# --------------------------------------------------------------------------
# live metering
# --------------------------------------------------------------------------

def db_to_fraction(db: float) -> float:
    if db <= METER_FLOOR_DB:
        return 0.0
    return max(0.0, min(1.0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB))


def peak_db(raw: int) -> float:
    if raw <= 0:
        return METER_FLOOR_DB
    return max(METER_FLOOR_DB, 20 * math.log10(raw / 32768.0))


def capture_argv(node: pw.Node) -> list[str]:
    positions = ",".join(c.position for c in node.channels) or "MONO"
    return ["pw-cat", "--record", "--target", node.name, "--rate", str(RATE),
            "--channels", str(max(1, len(node.channels))),
            "--channel-map", positions, "--format", "s16",
            # keep the device's own channels; no up/down-mixing
            "-P", "{ stream.dont-remix = true node.description = \"guitar-station meter\" }",
            "-"]


class LevelReader:
    """Streams one capture node through pw-cat and reports per-channel peaks.

    on_level(peaks_db: list[float], clipping: list[bool]) fires ~15x/second on
    a worker thread; callers marshal to their own main loop. Runs whether or
    not monitoring is on, so gain can be set before any sound is routed.

    on_samples(int16 array.array) gets the chosen channel's raw chunk on the
    same thread; the tuner hangs off this so the input is only captured once.
    """

    CHUNK = 3200          # frames per update (~15 Hz at 48k)

    def __init__(self, node: pw.Node, channel_index: int, on_level,
                 on_samples=None):
        self.node = node
        self.channel_index = channel_index
        self.on_level = on_level
        self.on_samples = on_samples
        self.error: str | None = None
        self.last_data = 0.0          # time.monotonic() of the last chunk
        self._proc = None
        self._thread = None
        self._stop = threading.Event()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        self._stop.clear()
        try:
            self._proc = subprocess.Popen(capture_argv(self.node),
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
        except OSError as e:
            self.error = str(e)
            return False
        # A 64 KiB pipe holds ~0.3 s of stereo audio. If this thread is held
        # up for longer, pw-cat stalls and drops samples, and a gap in the
        # stream throws the tuner off. Ask for a bigger pipe; best effort.
        try:
            fcntl.fcntl(self._proc.stdout.fileno(), F_SETPIPE_SZ, 1 << 20)
        except OSError:
            pass
        self.last_data = time.monotonic()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return True

    def _pump(self):
        nch = max(1, len(self.node.channels))
        need = self.CHUNK * nch * 2
        stream = self._proc.stdout
        try:
            while not self._stop.is_set():
                buf = stream.read(need)
                if not buf or len(buf) < need:
                    break
                a = array.array("h")
                a.frombytes(buf)
                peaks, clips = [], []
                for c in range(nch):
                    ch = a[c::nch]
                    # max()/min() on array.array run in C; cheap enough at 15 Hz.
                    p = max(max(ch), -min(ch))
                    peaks.append(peak_db(p))
                    clips.append(p >= CLIP_RAW)
                self.last_data = time.monotonic()
                self.on_level(peaks, clips)
                if self.on_samples and 0 <= self.channel_index < nch:
                    self.on_samples(a[self.channel_index::nch])
        except Exception as e:  # never leave the pw-cat process orphaned
            self.error = repr(e)
        finally:
            self._kill()

    def _kill(self):
        p = self._proc
        if p is None:
            return
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if p.stdout:
            p.stdout.close()

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._kill()
        self._proc = None


def gain_verdict(peak: float) -> str:
    """'idle', 'low', 'good', 'warm' or 'hot' for a peak reading in dBFS."""
    if peak <= METER_FLOOR_DB + 0.5:
        return "idle"
    if peak >= HOT_DB:
        return "hot"
    if peak >= GOOD_HI_DB:
        return "warm"
    if peak >= GOOD_LO_DB:
        return "good"
    return "low"
