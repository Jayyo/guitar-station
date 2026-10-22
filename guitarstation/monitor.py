"""The process that runs inside the systemd user unit while monitoring is on.

It owns a pw-loopback whose capture side is created with autoconnect off, so
WirePlumber never wires the interface's stereo pair into it. Once a second it
makes sure exactly one input channel feeds both loopback inputs:

    <interface>:capture_AUX2 -> input.<name>:input_FL
    <interface>:capture_AUX2 -> input.<name>:input_FR

and nothing else does. Re-plugging the interface tears those links down and
WirePlumber does not restore them for an autoconnect=false stream, so the
next tick puts them back (re-resolving the node name, which may have changed).

Started by core.start(); stops on SIGTERM/SIGINT.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading

from . import pw

TICK_S = 1.0


def _run(argv, timeout=10):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def spa_string(value: str) -> str:
    """Quote a value for a PipeWire property string (SPA JSON)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def loopback_argv(name: str, sink: str | None, latency: int | None,
                  rate: int = 48000) -> list[str]:
    cap = ["node.autoconnect=false", "node.description=" + spa_string(name + " input")]
    play = ["node.autoconnect=true", "node.description=" + spa_string(name)]
    if sink:
        # Pinned output: WirePlumber must not move the stream to whatever the
        # default is (headphones -> speakers would be rude). The router stops
        # monitoring if the sink goes away.
        play += ["target.object=" + spa_string(sink), "node.dont-reconnect=true"]
    if latency:
        req = f"node.latency={int(latency)}/{rate}"
        cap.append(req)
        play.append(req)
    return ["pw-loopback", "-n", name, "-m", "[ FL FR ]",
            "--capture-props=" + " ".join(cap),
            "--playback-props=" + " ".join(play)]


def plan_links(current: set[tuple[str, str]], source_port: str | None,
               name: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Links to add and remove so only `source_port` feeds our two inputs."""
    ours = {f"input.{name}:input_FL", f"input.{name}:input_FR"}
    want = {(source_port, i) for i in ours} if source_port else set()
    have = {(o, i) for (o, i) in current if i in ours}
    return sorted(want - have), sorted(have - want)


class Router:
    def __init__(self, args):
        self.args = args
        self.selector = {"node": args.node}
        if args.device is not None and args.pcm is not None:
            self.selector = {"device": args.device, "pcm": args.pcm, "node": args.node}
        self.node = args.node
        self.sink_missing = 0

    def source_port(self) -> str | None:
        """Full port name of the chosen channel, re-resolving after a replug."""
        rc, out = _run(["pw-link", "-o"])
        want = f"{self.node}:{self.args.port}" if self.node else None
        if rc == 0 and want and want in out.split():
            return want
        graph = pw.snapshot()
        node = graph.find_source(self.selector) if graph else None
        if node is None:
            return None
        ch = node.channel(self.args.channel, self.args.channel_index)
        if ch is None:
            return None
        self.node = node.name
        self.args.port = ch.port
        return f"{node.name}:{ch.port}"

    def sink_present(self) -> bool:
        rc, out = _run(["pw-link", "-i"])
        if rc != 0:
            return True              # can't tell; don't stop on a hiccup
        return any(p.startswith(self.args.sink + ":") for p in out.split())

    def tick(self) -> bool:
        """One round of link repair. False means monitoring should end."""
        if self.args.sink:
            self.sink_missing = 0 if self.sink_present() else self.sink_missing + 1
            if self.sink_missing >= 2:
                print(f"pinned output {self.args.sink} is gone, stopping", file=sys.stderr)
                return False
        rc, out = _run(["pw-link", "-l"])
        if rc != 0:
            return True
        add, remove = plan_links(pw.links(out), self.source_port(), self.args.name)
        for o, i in remove:
            _run(["pw-link", "-d", o, i])
        for o, i in add:
            _run(["pw-link", o, i])
        return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="guitarstation.monitor")
    ap.add_argument("--name", required=True, help="loopback / unit name")
    ap.add_argument("--node", required=True, help="capture node name at start")
    ap.add_argument("--port", required=True, help="capture port, e.g. capture_FL")
    ap.add_argument("--channel", required=True, help="channel position, e.g. FL")
    ap.add_argument("--channel-index", type=int, default=None)
    ap.add_argument("--device", default=None, help="card device.name")
    ap.add_argument("--pcm", type=int, default=None, help="ALSA PCM device number")
    ap.add_argument("--sink", default=None, help="pinned output node name")
    ap.add_argument("--latency", type=int, default=None, help="buffer request in samples")
    args = ap.parse_args(argv)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    loop = subprocess.Popen(loopback_argv(args.name, args.sink, args.latency))
    router = Router(args)
    try:
        while not stop.is_set() and loop.poll() is None:
            if not router.tick():
                stop.set()
                break
            stop.wait(TICK_S)
    finally:
        if loop.poll() is None:
            loop.terminate()
            try:
                loop.wait(timeout=3)
            except subprocess.TimeoutExpired:
                loop.kill()
                loop.wait()
    if stop.is_set():
        return 0
    # pw-loopback ended on its own. With a pinned output that is expected when
    # the output goes away; anything else is a failure.
    if args.sink and not router.sink_present():
        print(f"pinned output {args.sink} is gone, stopping", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
