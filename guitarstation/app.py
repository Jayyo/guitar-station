"""GTK4/libadwaita front-end for guitar-station.

One window: a big on/off for monitoring, live meters for every channel of the
chosen input (so the gain knob can be set without guessing), the tuner, and
the few settings that matter.

Threads: a poller thread takes a PipeWire snapshot + unit state every 1.5 s,
one control thread runs start/stop jobs one at a time, and the level reader
thread feeds the meters and the tuner. GTK is only touched on the main loop.
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__, core, pw, tuner  # noqa: E402
from .i18n import _  # noqa: E402

POLL_S = 1.5
STALE_S = 3.0          # no audio for this long from a live reader = restart it

CSS = """
levelbar.meter > trough { min-height: 10px; border-radius: 5px; }
levelbar.meter > trough > block.filled { border-radius: 5px; }
levelbar.meter.good > trough > block.filled { background-color: #33d17a; }
levelbar.meter.warm > trough > block.filled { background-color: #f5c211; }
levelbar.meter.hot  > trough > block.filled { background-color: #e01b24; }
levelbar.meter.low  > trough > block.filled { background-color: #62a0ea; }
levelbar.meter.idle > trough > block.filled { background-color: #77767b; }
label.db { font-family: monospace; font-size: 0.9em; }
label.verdict { font-size: 0.85em; }
label.chan.selected { font-weight: bold; }
.big-toggle { font-size: 1.15em; font-weight: bold; padding: 14px 0; }
label.tune-note { font-size: 3.2em; font-weight: bold; }
label.tune-note.ok { color: #33d17a; }
label.tune-info { font-family: monospace; }
label.tune-advice { font-size: 1.1em; font-weight: bold; }
label.tune-advice.ok { color: #33d17a; }
label.tune-advice.off { color: #e5a50a; }
"""


def preset_label(pid: str) -> str:
    return {"bass4": _("Bass, 4 strings"), "bass5": _("Bass, 5 strings"),
            "guitar6": _("Guitar, 6 strings"),
            "chromatic": _("Chromatic (nearest note)")}[pid]


def channel_label(ch: pw.Channel) -> str:
    if ch.position == "MONO":
        return _("Mono")
    return _("Input {n} ({position})").format(n=ch.number, position=ch.position)


def verdict_label(v: str) -> str:
    return {"idle": "", "low": _("gain low"), "good": _("good"),
            "warm": _("a bit hot"), "hot": _("too hot")}[v]


def latency_label(q: int) -> str:
    if not q:
        return _("PipeWire default")
    return _("{n} samples ({ms} ms)").format(n=q, ms=round(q / core.RATE * 1000, 1))


def plan_error_text(err: str) -> str:
    return {"no-pipewire": _("Can't reach PipeWire."),
            "no-input": _("Pick an input to monitor."),
            "no-channel": _("Pick the channel your instrument is on."),
            "no-output": _("The pinned output isn't connected.")}.get(err, err)


def start_error_text(msg: str) -> str:
    if msg.startswith("start-failed:"):
        return _("Couldn't start monitoring: {reason}").format(reason=msg.split(":", 1)[1])
    if msg.startswith("stop-failed:"):
        return _("Couldn't stop monitoring: {reason}").format(reason=msg.split(":", 1)[1])
    return {"exited": _("Monitoring stopped right after starting. "
                        "See: journalctl --user -u {unit}").format(unit=core.UNIT),
            "not-routed": _("Monitoring started, but the input or the output could not be "
                            "connected, so it was turned off again.")
            }.get(msg, msg)


class Needle(Gtk.DrawingArea):
    """Cents scale, -50..+50, with a needle. Green band = in tune."""

    def __init__(self):
        super().__init__()
        self.set_content_height(64)
        self.set_hexpand(True)
        self.cents = None
        self.set_draw_func(self._draw)

    def set_cents(self, cents):
        self.cents = None if cents is None else max(-50.0, min(50.0, cents))
        self.queue_draw()

    def _draw(self, _area, cr, w, h):
        pad = 18
        x0, x1 = pad, w - pad
        y = h * 0.55
        fg = self.get_color()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
        cr.set_line_width(1.5)
        cr.move_to(x0, y)
        cr.line_to(x1, y)
        cr.stroke()

        def xc(c):
            return x0 + (c + 50) / 100 * (x1 - x0)

        cr.set_source_rgba(0.2, 0.82, 0.48, 0.25)
        band = tuner.IN_TUNE_CENTS
        cr.rectangle(xc(-band), y - 14, xc(band) - xc(-band), 28)
        cr.fill()

        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
        for c in range(-50, 51, 10):
            tall = c % 50 == 0
            cr.move_to(xc(c), y - (12 if tall else 6))
            cr.line_to(xc(c), y + (12 if tall else 6))
        cr.stroke()
        cr.set_font_size(11)
        for c, txt in ((-50, "-50"), (0, "0"), (50, "+50")):
            ext = cr.text_extents(txt)
            cr.move_to(xc(c) - ext.width / 2, h - 4)
            cr.show_text(txt)

        if self.cents is None:
            return
        ok = abs(self.cents) <= tuner.IN_TUNE_CENTS
        cr.set_source_rgb(*((0.2, 0.82, 0.48) if ok else (0.9, 0.65, 0.04)))
        cr.set_line_width(3)
        cr.move_to(xc(self.cents), y - 22)
        cr.line_to(xc(self.cents), y + 22)
        cr.stroke()
        cr.arc(xc(self.cents), y - 24, 4, 0, 6.2832)
        cr.fill()


class Meter(Gtk.Box):
    """One channel: name + bar + dB readout + plain-language verdict."""

    def __init__(self, label: str, selected: bool):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.selected = selected
        name = Gtk.Label(label=label, xalign=0.0, width_chars=14)
        name.add_css_class("chan")
        name.add_css_class("selected" if selected else "dim-label")
        self.append(name)

        self.bar = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True,
                                valign=Gtk.Align.CENTER)
        self.bar.add_css_class("meter")
        self.bar.add_css_class("idle")
        self.append(self.bar)

        self.db = Gtk.Label(label="—", xalign=1.0, width_chars=8)
        self.db.add_css_class("db")
        self.db.add_css_class("dim-label")
        self.append(self.db)

        self.verdict = Gtk.Label(label="", xalign=0.0, width_chars=9)
        self.verdict.add_css_class("verdict")
        self.verdict.add_css_class("dim-label")
        self.append(self.verdict)
        self._shown = 0.0

    def update(self, db: float | None):
        if db is None:
            self._shown = 0.0
            self.bar.set_value(0.0)
            self._css("idle")
            self.db.set_text("—")
            self.verdict.set_text("")
            return
        target = core.db_to_fraction(db)
        # Fall back gently so a decaying note reads as a level, not a flicker.
        self._shown = target if target >= self._shown else max(target, self._shown - 0.06)
        self.bar.set_value(self._shown)
        v = core.gain_verdict(db)
        self._css(v)
        self.db.set_text(_("silent") if v == "idle" else f"{db:5.1f} dB")
        self.verdict.set_text(verdict_label(v) if self.selected else "")

    def _css(self, cls: str):
        for c in ("idle", "good", "warm", "hot", "low"):
            self.bar.remove_css_class(c)
        self.bar.add_css_class(cls)


class Poller(threading.Thread):
    """Takes a graph snapshot and the unit state off the main thread."""

    def __init__(self, deliver):
        super().__init__(daemon=True)
        self.deliver = deliver
        self.wake = threading.Event()
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            graph = pw.snapshot()
            state = core.unit_state()
            GLib.idle_add(self.deliver, graph, state)
            self.wake.wait(POLL_S)
            self.wake.clear()


class Window(Adw.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title=_("Guitar Station"),
                         default_width=600, default_height=900)
        self.cfg = core.load_config()
        self.graph: pw.Graph | None = None
        self.unit_state = "unknown"
        self._loading = True          # suppress combo signals during rebuilds
        self._sig = None
        self._source_ids: list = []   # what each input combo row stands for
        self._sink_ids: list = []
        self._channels: list[pw.Channel] = []
        self._channel_offset = 0      # 1 when a "(not available)" row sits on top
        self.meters: list[Meter] = []

        self.reader: core.LevelReader | None = None
        self.reader_key = None
        self._reader_gen = 0          # callbacks from replaced readers are ignored
        self.tracker = tuner.PitchTracker(self.cfg["tuner_preset"], self.cfg["a4"])
        self._peaks = None
        self._clips = None
        self._reading = None
        self._silence_since = None

        self._control = ThreadPoolExecutor(max_workers=1)
        self._busy = None             # None, "starting" or "stopping"
        self._queued_plan = None
        self._restart_queued = False

        self._build()
        self.connect("close-request", self.on_close)
        self._loading = False

        self.poller = Poller(self.on_poll)
        self.poller.start()
        GLib.timeout_add(80, self.tick)

    # ---- layout -----------------------------------------------------------

    def _build(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append(_("About Guitar Station"), "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        view.add_top_bar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.banner = Adw.Banner(revealed=False)
        self.banner.set_button_label(_("Dismiss"))
        self.banner.connect("button-clicked", lambda b: b.set_revealed(False))
        root.append(self.banner)

        page = Adw.PreferencesPage(vexpand=True)
        root.append(page)
        view.set_content(root)
        self.set_content(view)

        # power
        g = Adw.PreferencesGroup()
        self.toggle = Gtk.Button(label=_("Start monitoring"))
        self.toggle.add_css_class("big-toggle")
        self.toggle.add_css_class("suggested-action")
        self.toggle.connect("clicked", self.on_toggle)
        g.add(self.toggle)
        self.state_lbl = Gtk.Label(xalign=0.5, wrap=True, margin_top=6,
                                   justify=Gtk.Justification.CENTER)
        self.state_lbl.add_css_class("dim-label")
        g.add(self.state_lbl)
        page.add(g)

        # input
        g = Adw.PreferencesGroup(
            title=_("Input"),
            description=_("Play and turn the gain knob on the interface until the "
                          "selected channel reads “good” (-14 to -5 dB)."))
        self.input_row = Adw.ComboRow(title=_("Device"), model=Gtk.StringList(),
                                      use_subtitle=True)
        self.input_row.connect("notify::selected", self.on_input_changed)
        g.add(self.input_row)
        self.channel_row = Adw.ComboRow(title=_("Channel"),
                                        subtitle=_("Played on both sides of the output"),
                                        model=Gtk.StringList())
        self.channel_row.connect("notify::selected", self.on_channel_changed)
        g.add(self.channel_row)
        page.add(g)

        g = Adw.PreferencesGroup()
        self.meter_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                                 margin_top=4, margin_bottom=4)
        g.add(self.meter_box)
        self.clip_lbl = Gtk.Label(xalign=0.0)
        self.clip_lbl.add_css_class("verdict")
        g.add(self.clip_lbl)
        page.add(g)

        # tuner
        g = Adw.PreferencesGroup(
            title=_("Tuner"),
            description=_("Play one string at a time. Green means in tune."))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6)
        self.tune_note = Gtk.Label(label="—")
        self.tune_note.add_css_class("tune-note")
        box.append(self.tune_note)
        self.tune_advice = Gtk.Label(label=_("Play a string"))
        self.tune_advice.add_css_class("tune-advice")
        self.tune_advice.add_css_class("dim-label")
        box.append(self.tune_advice)
        self.needle = Needle()
        box.append(self.needle)
        self.tune_info = Gtk.Label(label="")
        self.tune_info.add_css_class("tune-info")
        self.tune_info.add_css_class("dim-label")
        box.append(self.tune_info)
        g.add(box)
        page.add(g)

        g = Adw.PreferencesGroup()
        self._preset_ids = list(tuner.PRESETS)
        self.preset_row = Adw.ComboRow(
            title=_("Instrument"), subtitle=_("Readings snap to the nearest open string"),
            model=Gtk.StringList.new([preset_label(p) for p in self._preset_ids]))
        self.preset_row.set_selected(self._preset_ids.index(self.cfg["tuner_preset"]))
        self.preset_row.connect("notify::selected", self.on_tuner_changed)
        g.add(self.preset_row)
        lo, hi = tuner.A4_RANGE
        self.a4_row = Adw.SpinRow.new_with_range(lo, hi, 1)
        self.a4_row.set_title(_("Reference A4"))
        self.a4_row.set_subtitle(_("Usually 440 Hz"))
        self.a4_row.set_value(self.cfg["a4"])
        self.a4_row.connect("notify::value", self.on_tuner_changed)
        g.add(self.a4_row)
        page.add(g)

        # output
        g = Adw.PreferencesGroup(title=_("Output"))
        self.output_row = Adw.ComboRow(title=_("Play through"), model=Gtk.StringList(),
                                       use_subtitle=True)
        self.output_row.connect("notify::selected", self.on_output_changed)
        g.add(self.output_row)
        self.latency_row = Adw.ComboRow(
            title=_("Buffer size"),
            subtitle=_("Requested per PipeWire cycle. Smaller is less delay, "
                       "but may crackle on a busy system."),
            model=Gtk.StringList.new([latency_label(q) for q in core.LATENCY_CHOICES]))
        self.latency_row.set_selected(core.LATENCY_CHOICES.index(self.cfg["latency"]))
        self.latency_row.connect("notify::selected", self.on_latency_changed)
        g.add(self.latency_row)
        self.auto_row = Adw.SwitchRow(
            title=_("Turn off after {min} minutes of silence").format(min=core.AUTO_STOP_MIN),
            subtitle=_("A forgotten monitor keeps pickup hum in your headphones. "
                       "Works while this window is open."))
        self.auto_row.set_active(self.cfg["auto_stop"])
        self.auto_row.connect("notify::active", self.on_auto_changed)
        g.add(self.auto_row)
        page.add(g)

        self._render_state()

    # ---- polling ------------------------------------------------------------

    def on_poll(self, graph, state):
        """Main loop: fresh PipeWire snapshot and unit state from the poller."""
        was_active = self.unit_state in ("active", "activating")
        self.graph, self.unit_state = graph, state
        if (was_active and state not in ("active", "activating") and not self._busy
                and graph is not None and self.cfg["output"]
                and graph.find_sink(self.cfg["output"]) is None):
            self._banner(_("The pinned output disappeared, so monitoring was turned off."))
        if graph is not None:
            sig = ([(n.name, n.description, tuple(c.position for c in n.channels))
                    for n in graph.sources],
                   [(n.name, n.description) for n in graph.sinks], graph.default_sink)
            if sig != self._sig:
                self._sig = sig
                self._rebuild_lists()
        self._ensure_reader()
        self._render_state()
        return False

    def _rebuild_lists(self):
        g = self.graph
        self._loading = True
        try:
            # inputs
            if self.cfg["input"] is None:
                suggestion = g.suggested_source()
                if suggestion is not None:
                    self.cfg["input"] = suggestion.selector
                    core.save_config(self.cfg)
            current = g.find_source(self.cfg["input"])
            labels, ids = [], []
            if self.cfg["input"] is None:
                labels.append(_("Choose an input…"))
                ids.append(None)
            elif current is None:
                labels.append(_("{name} (not connected)").format(name=self.cfg["input"]["node"]))
                ids.append("missing")
            for n in g.sources:
                labels.append(n.description)
                ids.append(n)
            self._source_ids = ids
            self.input_row.set_model(Gtk.StringList.new(labels))
            self.input_row.set_selected(
                next((i for i, n in enumerate(ids) if n is current and current), 0))
            self._rebuild_channels(current)

            # outputs
            default = next((s for s in g.sinks if s.name == g.default_sink), None)
            follow = _("Follow system output ({name})").format(
                name=default.description if default else "?")
            labels, ids = [follow], [None]
            pinned = g.find_sink(self.cfg["output"])
            if self.cfg["output"] and pinned is None:
                labels.append(_("{name} (not connected)").format(name=self.cfg["output"]["node"]))
                ids.append("missing")
            for s in g.sinks:
                labels.append(s.description)
                ids.append(s)
            self._sink_ids = ids
            self.output_row.set_model(Gtk.StringList.new(labels))
            if not self.cfg["output"]:
                sel = 0
            elif pinned is None:
                sel = 1
            else:
                sel = ids.index(pinned)
            self.output_row.set_selected(sel)
        finally:
            self._loading = False

    def _rebuild_channels(self, node: pw.Node | None):
        self._channels = list(node.channels) if node else []
        labels = [channel_label(c) for c in self._channels]
        self._channel_offset = 0
        ch = node.channel(self.cfg["channel"], self.cfg["channel_index"]) if node else None
        if node is not None and ch is None:
            if self.cfg["channel"] is None and self._channels:
                # first time with this input: take its first channel
                ch = self._channels[0]
                self.cfg["channel"], self.cfg["channel_index"] = ch.position, ch.index
                core.save_config(self.cfg)
            elif self.cfg["channel"] is not None:
                # The saved channel is gone (profile change?). Say so instead of
                # quietly switching to another one.
                labels.insert(0, _("{name} (not available)").format(name=self.cfg["channel"]))
                self._channel_offset = 1
        self.channel_row.set_model(Gtk.StringList.new(labels))
        self.channel_row.set_sensitive(len(labels) > 1)
        if ch is not None:
            self.channel_row.set_selected(self._channels.index(ch) + self._channel_offset)
        self._rebuild_meters(ch)

    def _rebuild_meters(self, selected: pw.Channel | None):
        while (child := self.meter_box.get_first_child()) is not None:
            self.meter_box.remove(child)
        self.meters = []
        for c in self._channels:
            m = Meter(channel_label(c), selected is not None and c.index == selected.index)
            self.meters.append(m)
            self.meter_box.append(m)
        if not self.meters:
            lbl = Gtk.Label(label=_("No input selected"), xalign=0.0)
            lbl.add_css_class("dim-label")
            self.meter_box.append(lbl)

    # ---- level reader -------------------------------------------------------

    def _selected(self) -> tuple[pw.Node | None, pw.Channel | None]:
        node = self.graph.find_source(self.cfg["input"]) if self.graph else None
        ch = node.channel(self.cfg["channel"], self.cfg["channel_index"]) if node else None
        return node, ch

    def _ensure_reader(self):
        node, ch = self._selected()
        key = (node.name, ch.index, tuple(c.position for c in node.channels)) if node and ch else None
        r = self.reader
        healthy = r is not None and r.alive and time.monotonic() - r.last_data < STALE_S
        if key == self.reader_key and (healthy or key is None):
            return
        self._stop_reader()
        self.reader_key = key
        if key is None:
            return
        self.tracker = tuner.PitchTracker(self.cfg["tuner_preset"], self.cfg["a4"])
        tracker, gen = self.tracker, self._reader_gen
        reader = core.LevelReader(
            node, ch.index,
            lambda peaks, clips: self._on_level(gen, peaks, clips),
            on_samples=lambda chunk: self._on_samples(gen, tracker, chunk))
        if reader.start():
            self.reader = reader

    def _stop_reader(self):
        r, self.reader = self.reader, None
        self._reader_gen += 1
        self._peaks = self._clips = self._reading = None
        if r is not None:
            threading.Thread(target=r.stop, daemon=True).start()

    def _on_level(self, gen, peaks, clips):
        """Reader thread. Store only; the GTK timeout renders."""
        if gen != self._reader_gen:
            return
        self._peaks = peaks
        if any(clips):
            self._clips = clips

    def _on_samples(self, gen, tracker, chunk):
        """Reader thread: 1-2 ms of numpy per chunk, so it runs right here."""
        reading = tracker.feed(chunk)
        if gen == self._reader_gen:
            self._reading = reading

    # ---- per-frame rendering ------------------------------------------------

    def tick(self):
        peaks = self._peaks
        for i, m in enumerate(self.meters):
            m.update(peaks[i] if peaks and i < len(peaks) else None)
        clips = self._clips
        if clips:
            self._clips = None
            names = [channel_label(c) for c, hit in zip(self._channels, clips) if hit]
            self.clip_lbl.set_markup('<span foreground="#e01b24">{}</span>'.format(
                GLib.markup_escape_text(_("Clipping on {channels}. Turn the gain down.")
                                        .format(channels=", ".join(names)))))
        elif self.clip_lbl.get_text():
            self.clip_lbl.set_text("")
        self._render_tuner(self._reading)
        self._check_auto_stop()
        return True

    def _render_tuner(self, r: tuner.Reading | None):
        if r is None:
            self.tune_note.set_text("—")
            self.tune_note.remove_css_class("ok")
            self.tune_advice.set_text(_("Play a string"))
            for c in ("ok", "off"):
                self.tune_advice.remove_css_class(c)
            self.tune_advice.add_css_class("dim-label")
            self.needle.set_cents(None)
            self.tune_info.set_text("")
            return
        self.tune_note.set_text(r.target)
        if r.in_tune:
            self.tune_note.add_css_class("ok")
        else:
            self.tune_note.remove_css_class("ok")
        advice = {"ok": _("In tune"), "far": _("Different note"),
                  "up": _("Tune up"), "down": _("Tune down")}[r.direction]
        self.tune_advice.set_text(advice)
        self.tune_advice.remove_css_class("dim-label")
        for c in ("ok", "off"):
            self.tune_advice.remove_css_class(c)
        self.tune_advice.add_css_class("ok" if r.in_tune else "off")
        self.needle.set_cents(r.cents)
        info = _("{freq} Hz   {cents}¢   target {target} Hz").format(
            freq=f"{r.freq:.2f}", cents=f"{r.cents:+.1f}", target=f"{r.target_freq:.2f}")
        if r.target != r.note:
            info += "   " + _("nearest note {note} {cents}¢").format(
                note=r.note, cents=f"{r.note_cents:+.0f}")
        self.tune_info.set_text(info)

    def _check_auto_stop(self):
        if not self.cfg["auto_stop"] or self.unit_state != "active" or self._busy:
            self._silence_since = None
            return
        node, ch = self._selected()
        peaks = self._peaks
        level = peaks[ch.index] if peaks and ch and ch.index < len(peaks) else None
        if level is not None and level > core.METER_FLOOR_DB + 0.5:
            self._silence_since = None
            return
        now = time.monotonic()
        if self._silence_since is None:
            self._silence_since = now
        elif now - self._silence_since > core.AUTO_STOP_MIN * 60:
            self._silence_since = None
            self._submit_stop()
            self._banner(_("No signal for {min} minutes, so monitoring was turned off.")
                         .format(min=core.AUTO_STOP_MIN))

    def _render_state(self):
        active = self.unit_state in ("active", "activating")
        plan_err = None
        try:
            core.make_plan(self.cfg, self.graph)
        except core.PlanError as e:
            plan_err = str(e)

        for c in ("suggested-action", "destructive-action"):
            self.toggle.remove_css_class(c)
        if self._busy == "starting":
            self.toggle.set_label(_("Starting…"))
        elif self._busy == "stopping":
            self.toggle.set_label(_("Stopping…"))
        elif active:
            self.toggle.set_label(_("Stop monitoring"))
            self.toggle.add_css_class("destructive-action")
        else:
            self.toggle.set_label(_("Start monitoring"))
            self.toggle.add_css_class("suggested-action")
        # Stopping must always be possible, even when the input is gone.
        self.toggle.set_sensitive(not self._busy and (active or plan_err is None))

        if active:
            node, ch = self._selected()
            out = self._output_name()
            what = (_("{channel} of {device}").format(channel=channel_label(ch), device=node.description)
                    if node and ch else _("the selected input"))
            text = _("Playing {what} through {output}.").format(what=what, output=out)
            text += "\n" + _("Keeps playing after this window closes. "
                             "Turn it off here or with “guitar-monitor off”.")
        elif self.unit_state == "failed":
            text = _("Monitoring stopped unexpectedly. "
                     "See: journalctl --user -u {unit}").format(unit=core.UNIT)
        elif plan_err:
            text = plan_error_text(plan_err)
        else:
            text = _("Off. The meters and the tuner work anyway, so you can set the gain first.")
        self.state_lbl.set_text(text)

    def _output_name(self) -> str:
        g = self.graph
        if g is None:
            return "?"
        node = g.find_sink(self.cfg["output"]) if self.cfg["output"] else \
            next((s for s in g.sinks if s.name == g.default_sink), None)
        return node.description if node else "?"

    def _banner(self, text):
        self.banner.set_title(text)
        self.banner.set_revealed(True)

    # ---- control --------------------------------------------------------------

    def on_toggle(self, _btn):
        if self._busy:
            return
        if self.unit_state in ("active", "activating"):
            self._submit_stop()
            return
        try:
            plan = core.make_plan(self.cfg, self.graph)
        except core.PlanError as e:
            self._banner(plan_error_text(str(e)))
            return
        self._submit_start(plan)

    def _submit_start(self, plan):
        self._run_control("starting", lambda: core.start(plan))

    def _submit_stop(self):
        self._run_control("stopping", core.stop)

    def _run_control(self, busy, job):
        """Run one start/stop job on the control thread; report back on the main loop."""
        self._busy = busy
        self._render_state()

        def work():
            try:
                ok, msg = job()
            except Exception as e:      # never leave the button stuck on "Starting…"
                ok, msg = False, repr(e)
            GLib.idle_add(self._control_done, ok, msg, core.unit_state())
        self._control.submit(work)

    def _control_done(self, ok, msg, state):
        self._busy = None
        self.unit_state = state
        if not ok:
            self._banner(start_error_text(msg))
        self._render_state()
        self.poller.wake.set()
        if self._restart_queued:
            self._restart_queued = False
            self._restart_if_active()
        return False

    def _restart_if_active(self):
        """Settings only reach a running monitor by restarting it."""
        if self._busy:
            # Coalesce: one restart with the latest settings once this job ends.
            self._restart_queued = True
            return
        if self.unit_state not in ("active", "activating"):
            self._render_state()
            return
        try:
            plan = core.make_plan(self.cfg, self.graph)
        except core.PlanError as e:
            self._banner(plan_error_text(str(e)))
            return
        self._submit_start(plan)

    # ---- settings -------------------------------------------------------------

    def on_input_changed(self, *_a):
        if self._loading:
            return
        i = self.input_row.get_selected()
        if not (0 <= i < len(self._source_ids)) or not isinstance(self._source_ids[i], pw.Node):
            return
        node = self._source_ids[i]
        self.cfg["input"] = node.selector
        self.cfg["channel"] = self.cfg["channel_index"] = None
        core.save_config(self.cfg)
        self._loading = True
        try:
            self._sig = None
            self._rebuild_lists()
        finally:
            self._loading = False
        self._ensure_reader()
        self._restart_if_active()

    def on_channel_changed(self, *_a):
        if self._loading:
            return
        i = self.channel_row.get_selected() - self._channel_offset
        if not (0 <= i < len(self._channels)):
            return
        ch = self._channels[i]
        self.cfg["channel"], self.cfg["channel_index"] = ch.position, ch.index
        core.save_config(self.cfg)
        self._rebuild_meters(ch)
        self._ensure_reader()
        self._restart_if_active()

    def on_output_changed(self, *_a):
        if self._loading:
            return
        i = self.output_row.get_selected()
        if not (0 <= i < len(self._sink_ids)) or self._sink_ids[i] == "missing":
            return
        sink = self._sink_ids[i]
        self.cfg["output"] = sink.selector if sink else None
        core.save_config(self.cfg)
        self._restart_if_active()

    def on_latency_changed(self, *_a):
        if self._loading:
            return
        self.cfg["latency"] = core.LATENCY_CHOICES[self.latency_row.get_selected()]
        core.save_config(self.cfg)
        self._restart_if_active()

    def on_auto_changed(self, *_a):
        self.cfg["auto_stop"] = self.auto_row.get_active()
        core.save_config(self.cfg)

    def on_tuner_changed(self, *_a):
        if self._loading:
            return
        pid = self._preset_ids[self.preset_row.get_selected()]
        a4 = float(self.a4_row.get_value())
        self.tracker.request(preset=pid, a4=a4)
        self.cfg["tuner_preset"], self.cfg["a4"] = pid, a4
        core.save_config(self.cfg)

    def on_close(self, *_a):
        self.poller.stopped.set()
        self.poller.wake.set()
        self._stop_reader()
        self._control.shutdown(wait=False)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=core.APP_ID)

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):      # GTK 4.12+
            provider.load_from_string(CSS)
        else:
            provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self.on_about)
        self.add_action(about)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()

    def on_about(self, *_a):
        kw = dict(application_name=_("Guitar Station"), application_icon=core.APP_ID,
                  version=__version__, developer_name="Jay Cho",
                  license_type=Gtk.License.MIT_X11,
                  website="https://github.com/Jayyo/guitar-station",
                  comments=_("Monitor and tune a guitar or bass through a USB "
                             "interface on PipeWire."))
        if hasattr(Adw, "AboutDialog"):                  # libadwaita 1.5+
            Adw.AboutDialog(**kw).present(self.props.active_window)
        else:
            Adw.AboutWindow(transient_for=self.props.active_window, **kw).present()


def main():
    return App().run(sys.argv)
