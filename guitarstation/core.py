"""Core logic for guitar-station.

PipeWire device discovery, loopback control and live level metering for a
PreSonus AudioBox USB 96 (or any USB interface) feeding a guitar into the
desktop's current output.

Deliberately free of GUI dependencies so every piece can be exercised from a
shell:

    python3 -c "from guitarstation import core; print(core.status())"
"""

from __future__ import annotations

import array
import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

APP_ID = "io.github.jayyo.GuitarStation"
UNIT = "guitar-monitor"
UNIT_SERVICE = f"{UNIT}.service"

HOME = Path.home()
CONFIG_PATH = HOME / ".config" / "guitar-station" / "config.json"

# PreSonus AudioBox USB 96. Kept as USB ids because PipeWire node names are
# NOT stable across replugs and profile switches.
IFACE_USB_ID = "194f:0303"
IFACE_MATCH = "AudioBox_USB_96"

RATE = 48000
METER_FLOOR_DB = -60.0
CLIP_RAW = 32700          # s16 sample magnitude we call clipping
HOT_DB = -3.0             # too hot, headroom nearly gone
GOOD_LO_DB = -14.0        # comfortable recording window
GOOD_HI_DB = -5.0

QUANTUM_CHOICES = [128, 256, 512, 1024]

DEFAULT_CONFIG = {
    "output": None,       # sink fingerprint, or None = follow system default
    "mono": True,         # mirror Input 1 to both sides
    "quantum": 256,
    "auto_stop_min": 0,   # 0 = off; minutes of silence before stopping
    "tuner_preset": "bass4",   # see tuner.PRESETS
    "a4": 440.0,               # tuner reference pitch
}


def _run(argv, timeout=15):
    """Run a command, return (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _env():
    e = dict(os.environ)
    e.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return e


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    if cfg.get("quantum") not in QUANTUM_CHOICES:
        cfg["quantum"] = DEFAULT_CONFIG["quantum"]
    return cfg


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------
# device discovery
# --------------------------------------------------------------------------

def interface_connected() -> bool:
    rc, out, _ = _run(["lsusb"])
    return rc == 0 and IFACE_USB_ID in out


def _pw_dump() -> list:
    rc, out, _ = _run(["pw-dump"], timeout=10)
    if rc != 0:
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


@dataclass
class Sink:
    name: str          # current PipeWire node name (volatile)
    nick: str          # human label
    fingerprint: str   # node name minus the profile suffix (stable across profiles)
    is_default: bool = False


def sink_fingerprint(node_name: str) -> str:
    """Drop the trailing profile segment.

    The same G733 shows up as ...-00.iec958-stereo one day and
    ...-00.analog-stereo the next, so the full node name is useless as an
    identity. Everything up to the last dot survives profile changes.
    """
    return node_name.rsplit(".", 1)[0] if "." in node_name else node_name


def default_sink_name() -> str | None:
    rc, out, _ = _run(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"])
    if rc != 0:
        return None
    for line in out.splitlines():
        if "node.name" in line and "=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    return None


def list_sinks() -> list[Sink]:
    default = default_sink_name()
    sinks = []
    for o in _pw_dump():
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        props = (o.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name") or ""
        if not name:
            continue
        nick = props.get("node.nick") or props.get("node.description") or name
        sinks.append(Sink(name=name, nick=nick,
                          fingerprint=sink_fingerprint(name),
                          is_default=(name == default)))
    sinks.sort(key=lambda s: (not s.is_default, s.nick))
    return sinks


def resolve_sink(fingerprint: str | None) -> str | None:
    """Fingerprint -> current node name, or None to follow the system default."""
    if not fingerprint:
        return None
    for s in list_sinks():
        if s.fingerprint == fingerprint:
            return s.name
    return None


def _iface_node(media_class: str) -> str | None:
    for o in _pw_dump():
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        props = (o.get("info") or {}).get("props") or {}
        if props.get("media.class") != media_class:
            continue
        name = props.get("node.name") or ""
        if IFACE_MATCH in name:
            return name
    return None


def input_node() -> str | None:
    """Current capture node of the interface. Re-queried every time; replugging
    the box changes the node id and any cached name goes stale."""
    return _iface_node("Audio/Source")


def output_node() -> str | None:
    return _iface_node("Audio/Sink")


# --------------------------------------------------------------------------
# monitoring (loopback) control
# --------------------------------------------------------------------------

def is_active() -> bool:
    rc, out, _ = _run(["systemctl", "--user", "is-active", UNIT_SERVICE])
    return out.strip() == "active"


def set_quantum(q: int | None):
    """Force graph quantum, or None to hand control back to PipeWire."""
    _run(["pw-metadata", "-n", "settings", "0", "clock.force-quantum",
          str(q if q else 0)])


def current_quantum() -> int | None:
    rc, out, _ = _run(["pw-metadata", "-n", "settings"])
    for line in out.splitlines():
        if "clock.force-quantum" in line:
            try:
                v = int(line.split("value:'")[1].split("'")[0])
                return v or None
            except (IndexError, ValueError):
                return None
    return None


def quantum_latency_ms(q: int) -> float:
    return round(q / RATE * 1000, 1)


def _links_of(port: str, direction: str) -> list[str]:
    rc, out, _ = _run(["pw-link", "-l"])
    return out.splitlines() if rc == 0 else []


def start(sink_name: str | None = None, mono: bool = True,
          quantum: int | None = 256) -> tuple[bool, str]:
    """Bring up the loopback. Returns (ok, message)."""
    if not interface_connected():
        return False, "오디오 인터페이스가 연결되어 있지 않아"

    src = input_node()
    if not src:
        return False, "인터페이스 입력 노드를 찾지 못했어"

    stop(restore_quantum=False)

    argv = ["systemd-run", "--user", f"--unit={UNIT}",
            f"--setenv=XDG_RUNTIME_DIR={_env()['XDG_RUNTIME_DIR']}",
            "/usr/bin/pw-loopback", "-n", UNIT, "-C", src]
    if sink_name:
        argv += ["-P", sink_name]
    else:
        # No target: the loopback lands on whatever sink is default, and keeps
        # following it. Pinning a sink is the usual cause of "no sound".
        argv += ["--playback-props=node.autoconnect=true"]

    rc, _, err = _run(argv)
    if rc != 0:
        return False, f"시작 실패: {err.strip()[:120]}"

    _wait_for_node("input." + UNIT, timeout=4.0)
    if mono:
        _mirror_input_one(src)
    set_quantum(quantum)
    return True, "켜짐"


def _wait_for_node(name: str, timeout: float = 4.0) -> bool:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, out, _ = _run(["pw-link", "-i"])
        if name in out:
            return True
        time.sleep(0.2)
    return False


def _mirror_input_one(src: str):
    """Input 1 arrives on the left channel only; feed it to both sides.

    pw-loopback's own audio.position=[MONO] does not do this reliably, so the
    links are rewired by hand. This has to be redone after every replug because
    a new node comes up with the stock stereo wiring.
    """
    _run(["pw-link", "-d", f"{src}:capture_FR", f"input.{UNIT}:input_FR"])
    _run(["pw-link", f"{src}:capture_FL", f"input.{UNIT}:input_FR"])


def stop(restore_quantum: bool = True) -> tuple[bool, str]:
    _run(["systemctl", "--user", "stop", UNIT_SERVICE])
    if restore_quantum:
        set_quantum(None)
    return True, "꺼짐"


def status() -> dict:
    return {
        "connected": interface_connected(),
        "active": is_active(),
        "input_node": input_node(),
        "default_sink": default_sink_name(),
        "quantum": current_quantum(),
    }


# --------------------------------------------------------------------------
# live metering
# --------------------------------------------------------------------------

def db_to_fraction(db: float) -> float:
    if db <= METER_FLOOR_DB:
        return 0.0
    return max(0.0, min(1.0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB))


def _peak_db(raw: int) -> float:
    if raw <= 0:
        return METER_FLOOR_DB
    import math
    return max(METER_FLOOR_DB, 20 * math.log10(raw / 32768.0))


class LevelReader:
    """Streams the interface input through pw-cat and reports peak levels.

    on_level(left_db, right_db, clipping) fires ~15x/second on a worker thread;
    callers marshal to their own main loop. Runs whether or not monitoring is
    on, so gain can be set before any sound is routed anywhere.

    on_samples(left) (optional) gets the raw Input 1 chunk as an int16
    array.array on the same thread -- the tuner hangs off this so the input is
    only captured once.
    """

    CHUNK = 3200          # frames per update (~15 Hz at 48k)

    def __init__(self, on_level, on_error=None, on_samples=None):
        self.on_level = on_level
        self.on_error = on_error
        self.on_samples = on_samples
        self._proc = None
        self._thread = None
        self._stop = threading.Event()

    def start(self) -> bool:
        src = input_node()
        if not src:
            if self.on_error:
                self.on_error("입력 장치를 찾지 못했어")
            return False
        self._stop.clear()
        try:
            self._proc = subprocess.Popen(
                ["pw-cat", "--record", "--target", src, "--rate", str(RATE),
                 "--channels", "2", "--format", "s16", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_env())
        except OSError as e:
            if self.on_error:
                self.on_error(str(e))
            return False
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return True

    def _pump(self):
        need = self.CHUNK * 2 * 2      # frames * channels * bytes
        stream = self._proc.stdout
        while not self._stop.is_set():
            buf = stream.read(need)
            if not buf or len(buf) < need:
                break
            a = array.array("h")
            a.frombytes(buf)
            left, right = a[0::2], a[1::2]
            # max()/min() on array.array run in C; cheap enough at 15 Hz.
            pl = max(max(left), -min(left))
            pr = max(max(right), -min(right))
            self.on_level(_peak_db(pl), _peak_db(pr),
                          pl >= CLIP_RAW or pr >= CLIP_RAW)
            if self.on_samples:
                self.on_samples(left)

    def stop(self):
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


def gain_verdict(peak_db: float) -> tuple[str, str]:
    """(verdict, css class) for a peak reading."""
    if peak_db <= METER_FLOOR_DB + 0.5:
        return "무신호", "idle"
    if peak_db >= HOT_DB:
        return "게인 너무 높음", "hot"
    if peak_db >= GOOD_HI_DB:
        return "살짝 높음", "warm"
    if peak_db >= GOOD_LO_DB:
        return "적정", "good"
    return "게인 낮음", "low"
