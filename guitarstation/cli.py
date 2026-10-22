"""`guitar-monitor`: the same core as the window, from a terminal.

Uses the input, channel and output saved by the app (see `guitar-monitor devices`).
"""

from __future__ import annotations

import argparse
import math
import sys
import time

from . import core, pw, tuner
from .i18n import _


def _seconds(text: str) -> float:
    v = float(text)
    if not math.isfinite(v) or v <= 0 or v > 3600:
        raise argparse.ArgumentTypeError(_("expected a number of seconds between 0 and 3600"))
    return v


def _selection(cfg, graph):
    node = graph.find_source(cfg["input"]) if graph else None
    ch = node.channel(cfg["channel"], cfg["channel_index"]) if node else None
    return node, ch


def _plan_or_exit(cfg, graph) -> core.MonitorPlan:
    try:
        return core.make_plan(cfg, graph)
    except core.PlanError as e:
        msg = {"no-pipewire": _("Can't reach PipeWire."),
               "no-input": _("No usable input. Pick one in the app or check `guitar-monitor devices`."),
               "no-channel": _("The saved channel doesn't exist on this input."),
               "no-output": _("The pinned output isn't connected.")}.get(str(e), str(e))
        print(msg, file=sys.stderr)
        sys.exit(1)


def cmd_status(args) -> int:
    cfg, graph = core.load_config(), pw.snapshot()
    state = core.unit_state()
    node, ch = _selection(cfg, graph)
    print(_("monitoring : {state}").format(state=state))
    if node:
        chan = ch.position if ch else "?"
        print(_("input      : {device}, channel {channel}").format(device=node.description, channel=chan))
    else:
        print(_("input      : not connected or not chosen"))
    if cfg["output"]:
        sink = graph.find_sink(cfg["output"]) if graph else None
        if sink:
            print(_("output     : {name} (pinned)").format(name=sink.description))
        else:
            print(_("output     : {name} (pinned, not connected)").format(name=cfg["output"]["node"]))
    else:
        print(_("output     : follows the system output"))
    print(_("buffer     : {value}").format(
        value=_("{n} samples").format(n=cfg["latency"]) if cfg["latency"] else _("PipeWire default")))
    return 0


def cmd_on(args) -> int:
    cfg, graph = core.load_config(), pw.snapshot()
    if core.is_active() and not args.restart:
        print(_("Already on. Use `guitar-monitor on --restart` to apply new settings."))
        return 0
    ok, msg = core.start(_plan_or_exit(cfg, graph))
    print(_("on") if ok else _("failed: {reason}").format(reason=msg))
    return 0 if ok else 1


def cmd_off(args) -> int:
    ok, _msg = core.stop()
    print(_("off") if ok else _("could not stop {unit}").format(unit=core.UNIT_SERVICE))
    return 0 if ok else 1


def cmd_devices(args) -> int:
    cfg, graph = core.load_config(), pw.snapshot()
    if graph is None:
        print(_("Can't reach PipeWire."), file=sys.stderr)
        return 1
    chosen = graph.find_source(cfg["input"])
    print(_("Inputs:"))
    for n in graph.sources:
        mark = "*" if chosen is n else " "
        chans = ", ".join(c.position for c in n.channels) or "-"
        print(f" {mark} {n.description}  [{chans}]")
    pinned = graph.find_sink(cfg["output"])
    print(_("Outputs:"))
    for s in graph.sinks:
        mark = "*" if pinned is s else ("d" if s.name == graph.default_sink else " ")
        print(f" {mark} {s.description}")
    print(_("(* = chosen in guitar-station, d = system default)"))
    return 0


def _reader_or_exit(cfg, graph, on_level, on_samples=None) -> tuple[core.LevelReader, pw.Channel]:
    node, ch = _selection(cfg, graph)
    if node is None or ch is None:
        _plan_or_exit(cfg, graph)          # prints the reason and exits
    reader = core.LevelReader(node, ch.index, on_level, on_samples=on_samples)
    if not reader.start():
        print(_("Could not start pw-cat: {error}").format(error=reader.error), file=sys.stderr)
        sys.exit(1)
    return reader, ch


def cmd_level(args) -> int:
    cfg, graph = core.load_config(), pw.snapshot()
    peaks: list[float] = []
    clipped: set[int] = set()
    got = [0]

    def on_level(p, clips):
        got[0] += 1
        for i, v in enumerate(p):
            if i >= len(peaks):
                peaks.append(v)
            peaks[i] = max(peaks[i], v)
        clipped.update(i for i, c in enumerate(clips) if c)

    reader, ch = _reader_or_exit(cfg, graph, on_level)
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
    if not got[0]:
        print(_("No audio arrived from the input."), file=sys.stderr)
        return 1
    node, _ch = _selection(cfg, graph)
    for i, v in enumerate(peaks):
        c = node.channels[i] if i < len(node.channels) else None
        name = c.position if c else str(i + 1)
        verdict = core.gain_verdict(v) if i == ch.index else ""
        mark = "*" if i == ch.index else " "
        print(f"{mark} {name:<5} {v:6.1f} dBFS  {verdict}")
    if clipped:
        print(_("Clipping happened. Turn the gain down."))
    return 0


def cmd_tune(args) -> int:
    cfg, graph = core.load_config(), pw.snapshot()
    tracker = tuner.PitchTracker(cfg["tuner_preset"], cfg["a4"])
    last = [None]
    got = [0]

    def on_samples(chunk):
        got[0] += 1
        r = tracker.feed(chunk)
        if r is None:
            line = "—"
        else:
            line = (f"{r.target:<3} {r.freq:7.2f} Hz  {r.cents:+5.1f}¢  "
                    + {"ok": _("in tune"), "far": _("different note"),
                       "up": _("tune up"), "down": _("tune down")}[r.direction])
        if line != last[0]:
            print(line, flush=True)
            last[0] = line

    reader, _ch = _reader_or_exit(cfg, graph, lambda *a: None, on_samples)
    print(_("Play one string at a time for {secs} s (preset {preset}, A4 = {a4} Hz).").format(
        secs=f"{args.seconds:g}", preset=tracker.preset, a4=f"{tracker.a4:g}"), flush=True)
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
    if not got[0]:
        print(_("No audio arrived from the input."), file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="guitar-monitor",
                                 description=_("Control guitar-station from a terminal."))
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help=_("show monitoring state and the saved selection"))
    p = sub.add_parser("on", help=_("start monitoring with the saved settings"))
    p.add_argument("--restart", action="store_true", help=_("restart if already on"))
    sub.add_parser("off", help=_("stop monitoring"))
    sub.add_parser("devices", help=_("list inputs and outputs"))
    p = sub.add_parser("level", help=_("measure peak levels"))
    p.add_argument("seconds", nargs="?", type=_seconds, default=5.0)
    p = sub.add_parser("tune", help=_("print tuner readings"))
    p.add_argument("seconds", nargs="?", type=_seconds, default=15.0)
    args = ap.parse_args(argv)
    handler = {"status": cmd_status, "on": cmd_on, "off": cmd_off,
               "devices": cmd_devices, "level": cmd_level, "tune": cmd_tune}
    return handler[args.cmd or "status"](args)
