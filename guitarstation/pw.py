"""Read-only view of the PipeWire graph, built from a single pw-dump call.

PipeWire node names are not stable: a USB box that is re-plugged or switched
to another profile comes back as `...analog-stereo`, `...pro-input-0` or
`...mono-fallback.2`. So a device is remembered by a selector that prefers the
card name plus the ALSA PCM device number and only falls back to the node name
for virtual or non-ALSA nodes.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

SOURCE_CLASSES = ("Audio/Source", "Audio/Source/Virtual")
SINK_CLASSES = ("Audio/Sink",)
# Things with a microphone that should never be picked for you automatically.
NOT_AN_INSTRUMENT_INPUT = {"webcam", "headset", "handset", "headphone", "microphone"}


@dataclass
class Channel:
    position: str        # PipeWire channel position: FL, FR, MONO, AUX2, ...
    port: str            # port name, e.g. capture_FL
    index: int           # order on the node

    @property
    def number(self) -> int:
        """1-based input number as printed on most interfaces."""
        m = re.fullmatch(r"AUX(\d+)", self.position)
        if m:
            return int(m.group(1)) + 1
        return self.index + 1


@dataclass
class Node:
    id: int
    name: str                      # node.name, volatile across replugs
    description: str
    media_class: str
    device: str | None = None      # device.name of the owning card
    pcm: int | None = None         # ALSA PCM device number on that card
    bus: str | None = None
    form_factor: str | None = None
    channels: list[Channel] = field(default_factory=list)

    @property
    def selector(self) -> dict:
        if self.device is not None and self.pcm is not None:
            return {"device": self.device, "pcm": self.pcm, "node": self.name}
        return {"node": self.name}

    def channel(self, position: str | None, index: int | None = None) -> Channel | None:
        for ch in self.channels:
            if ch.position == position:
                return ch
        if index is not None and 0 <= index < len(self.channels):
            return self.channels[index]
        return None


@dataclass
class Graph:
    sources: list[Node]
    sinks: list[Node]
    default_sink: str | None = None
    default_source: str | None = None

    def find_source(self, selector: dict | None) -> Node | None:
        return _find(self.sources, selector)

    def find_sink(self, selector: dict | None) -> Node | None:
        return _find(self.sinks, selector)

    def suggested_source(self) -> Node | None:
        """A sensible first choice: a USB input that isn't a webcam or headset."""
        for n in self.sources:
            if n.bus == "usb" and (n.form_factor or "") not in NOT_AN_INSTRUMENT_INPUT:
                return n
        return None


def _find(nodes: list[Node], selector: dict | None) -> Node | None:
    if not isinstance(selector, dict):
        return None
    device, pcm = selector.get("device"), selector.get("pcm")
    if device is not None and pcm is not None:
        for n in nodes:
            if n.device == device and n.pcm == pcm:
                return n
    name = selector.get("node")
    if name:
        for n in nodes:
            if n.name == name:
                return n
    return None


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _default_names(dump: list) -> tuple[str | None, str | None]:
    sink = source = None
    for o in dump:
        if o.get("type") != "PipeWire:Interface:Metadata":
            continue
        if (o.get("props") or {}).get("metadata.name") != "default":
            continue
        for entry in o.get("metadata") or []:
            value = entry.get("value")
            name = value.get("name") if isinstance(value, dict) else None
            if entry.get("key") == "default.audio.sink":
                sink = name
            elif entry.get("key") == "default.audio.source":
                source = name
    return sink, source


def parse(dump: list) -> Graph:
    """Build a Graph from parsed `pw-dump` output (pure, for tests)."""
    devices = {}
    ports: dict[int, list[tuple[int, str, str]]] = {}
    for o in dump:
        info = o.get("info") or {}
        props = info.get("props") or {}
        if o.get("type") == "PipeWire:Interface:Device":
            devices[o.get("id")] = props
        elif o.get("type") == "PipeWire:Interface:Port":
            if props.get("port.direction") != "out":
                continue
            node_id = _as_int(props.get("node.id"))
            name = props.get("port.name") or ""
            if node_id is None or not name or "monitor" in name:
                continue
            pos = props.get("audio.channel") or name.split("_", 1)[-1]
            ports.setdefault(node_id, []).append(
                (_as_int(props.get("port.id")) or 0, pos, name))

    sources, sinks = [], []
    for o in dump:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        props = (o.get("info") or {}).get("props") or {}
        mclass = props.get("media.class")
        if mclass not in SOURCE_CLASSES + SINK_CLASSES:
            continue
        name = props.get("node.name")
        if not name:
            continue
        dev = devices.get(_as_int(props.get("device.id"))) or {}
        node = Node(
            id=o.get("id"),
            name=name,
            description=(props.get("node.description") or props.get("node.nick")
                         or dev.get("device.description") or name),
            media_class=mclass,
            device=dev.get("device.name"),
            pcm=_as_int(props.get("alsa.device", props.get("api.alsa.pcm.device"))),
            bus=dev.get("device.bus"),
            form_factor=dev.get("device.form-factor"),
        )
        if mclass in SOURCE_CLASSES:
            for i, (_pid, pos, pname) in enumerate(sorted(ports.get(o.get("id"), []))):
                node.channels.append(Channel(position=pos, port=pname, index=i))
            sources.append(node)
        else:
            sinks.append(node)

    default_sink, default_source = _default_names(dump)
    sources.sort(key=lambda n: n.description.lower())
    sinks.sort(key=lambda n: (n.name != default_sink, n.description.lower()))
    return Graph(sources=sources, sinks=sinks,
                 default_sink=default_sink, default_source=default_source)


def snapshot(timeout: float = 10) -> Graph | None:
    """Current graph, or None when PipeWire can't be reached."""
    try:
        p = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        return parse(json.loads(p.stdout))
    except (ValueError, AttributeError, TypeError):
        return None


def links(text: str) -> set[tuple[str, str]]:
    """Parse `pw-link -l` into (output port, input port) pairs."""
    out: set[tuple[str, str]] = set()
    current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            current = line.strip()
            continue
        s = line.strip()
        if s.startswith("|->") and current:
            out.add((current, s[3:].strip()))
        elif s.startswith("|<-") and current:
            out.add((s[3:].strip(), current))
    return out
