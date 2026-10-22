"""GTK4/libadwaita front-end for guitar-station.

One window: a big on/off for guitar monitoring, live input meters so the gain
knob can be set without guessing, and the handful of settings that actually
matter (where the sound goes, mono mirroring, latency).

Everything that touches PipeWire lives in core; this file only renders it and
marshals worker results back onto the GTK main loop.
"""

from __future__ import annotations

import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import core, tuner  # noqa: E402

FOLLOW_DEFAULT = "기본 출력 따라가기"
PRESET_LABELS = {"bass4": "베이스 4현", "bass5": "베이스 5현", "guitar6": "기타 6현",
                 "chromatic": "크로매틱 (가까운 음)"}
ADVICE = {"ok": "맞음", "far": "다른 음", "up": "조여", "down": "풀어"}
TUNER_HOLD_S = 1.5      # keep the last reading on screen this long after the note dies

CSS = b"""
levelbar.meter > trough { min-height: 12px; border-radius: 6px; }
levelbar.meter > trough > block.filled { background-color: #33d17a; border-radius: 6px; }
levelbar.meter.good > trough > block.filled { background-color: #33d17a; }
levelbar.meter.warm > trough > block.filled { background-color: #f5c211; }
levelbar.meter.hot  > trough > block.filled { background-color: #e01b24; }
levelbar.meter.low  > trough > block.filled { background-color: #62a0ea; }
levelbar.meter.idle > trough > block.filled { background-color: #5e5c64; }
label.db { font-family: monospace; font-size: 0.9em; }
label.verdict { font-size: 0.85em; }
.big-toggle { font-size: 1.15em; font-weight: bold; padding: 14px 0; }
label.tune-note { font-size: 3.2em; font-weight: bold; }
label.tune-note.ok { color: #33d17a; }
label.tune-info { font-family: monospace; font-size: 1.0em; }
label.tune-advice { font-size: 1.1em; font-weight: bold; }
label.tune-advice.ok { color: #33d17a; }
label.tune-advice.off { color: #f5c211; }
"""


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

        # in-tune band
        cr.set_source_rgba(0.2, 0.82, 0.48, 0.25)
        cr.rectangle(xc(-tuner.IN_TUNE_CENTS), y - 14, xc(tuner.IN_TUNE_CENTS) - xc(-tuner.IN_TUNE_CENTS), 28)
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
        cr.set_source_rgb(*((0.2, 0.82, 0.48) if ok else (0.96, 0.76, 0.07)))
        cr.set_line_width(3)
        cr.move_to(xc(self.cents), y - 22)
        cr.line_to(xc(self.cents), y + 22)
        cr.stroke()
        cr.arc(xc(self.cents), y - 24, 4, 0, 6.2832)
        cr.fill()


class Meter(Gtk.Box):
    """One channel: bar + dB readout + plain-language verdict."""

    def __init__(self, label: str):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        name = Gtk.Label(label=label, xalign=0.0, width_chars=11)
        name.add_css_class("dim-label")
        self.append(name)

        self.bar = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True,
                                valign=Gtk.Align.CENTER)
        self.bar.add_css_class("meter")
        self.bar.add_css_class("idle")
        self.append(self.bar)

        self.db = Gtk.Label(label="—", xalign=1.0, width_chars=9)
        self.db.add_css_class("db")
        self.db.add_css_class("dim-label")
        self.append(self.db)

        self.verdict = Gtk.Label(label="", xalign=0.0, width_chars=11)
        self.verdict.add_css_class("verdict")
        self.verdict.add_css_class("dim-label")
        self.append(self.verdict)

        self._shown = 0.0

    def update(self, db: float | None, show_verdict: bool = True):
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

        verdict, cls = core.gain_verdict(db)
        self._css(cls)
        self.db.set_text("무음" if cls == "idle" else f"{db:5.1f} dB")
        self.verdict.set_text(verdict if show_verdict and cls != "idle" else "")

    def _css(self, cls: str):
        for c in ("idle", "good", "warm", "hot", "low"):
            self.bar.remove_css_class(c)
        self.bar.add_css_class(cls)


class Window(Adw.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title="기타 스테이션",
                         default_width=560, default_height=800)

        self.cfg = core.load_config()
        self._loading = True   # suppress combo signals during rebuilds
        self.sinks = []
        self.reader = None
        self._peaks = (None, None)
        self._clipping = False
        self._silence_since = None
        self._busy = False
        self.tracker = tuner.PitchTracker(preset=self.cfg.get("tuner_preset"),
                                          a4=self.cfg.get("a4", tuner.DEFAULT_A4))
        self._reading = None
        self._reading_at = 0.0

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        root.append(header)

        self.banner = Adw.Banner(revealed=False)
        root.append(self.banner)

        page = Adw.PreferencesPage()
        page.set_vexpand(True)
        root.append(page)

        # ---- power ----------------------------------------------------
        g_power = Adw.PreferencesGroup()
        self.toggle = Gtk.Button()
        self.toggle.add_css_class("big-toggle")
        self.toggle.add_css_class("suggested-action")
        self.toggle.connect("clicked", self.on_toggle)
        g_power.add(self.toggle)

        self.state_lbl = Gtk.Label(xalign=0.5)
        self.state_lbl.add_css_class("dim-label")
        self.state_lbl.set_margin_top(6)
        self.state_lbl.set_wrap(True)
        g_power.add(self.state_lbl)
        page.add(g_power)

        # ---- meters ---------------------------------------------------
        g_meter = Adw.PreferencesGroup(title="입력 레벨",
                                       description="기타를 치면서 인터페이스의 Gain 노브를 "
                                                   "돌려 '적정'(-14 ~ -5 dB)에 맞춰")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        self.m1 = Meter("Input 1")
        self.m2 = Meter("Input 2")
        box.append(self.m1)
        box.append(self.m2)
        self.clip_lbl = Gtk.Label(xalign=0.0)
        self.clip_lbl.add_css_class("verdict")
        box.append(self.clip_lbl)
        g_meter.add(box)
        page.add(g_meter)

        # ---- tuner ----------------------------------------------------
        g_tune = Adw.PreferencesGroup(
            title="튜너", description="현을 하나만 울려. 바늘이 가운데 초록 구간에 들어오면 맞은 거야")
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        tbox.set_margin_top(6)
        tbox.set_margin_bottom(6)
        self.tune_note = Gtk.Label(label="—")
        self.tune_note.add_css_class("tune-note")
        tbox.append(self.tune_note)
        self.tune_advice = Gtk.Label(label="현을 쳐봐")
        self.tune_advice.add_css_class("tune-advice")
        self.tune_advice.add_css_class("dim-label")
        tbox.append(self.tune_advice)
        self.needle = Needle()
        tbox.append(self.needle)
        self.tune_info = Gtk.Label(label="")
        self.tune_info.add_css_class("tune-info")
        self.tune_info.add_css_class("dim-label")
        tbox.append(self.tune_info)
        g_tune.add(tbox)
        page.add(g_tune)

        g_tune_set = Adw.PreferencesGroup()
        self.preset_row = Adw.ComboRow(title="악기", subtitle="가장 가까운 개방현 기준으로 알려줘")
        self.preset_model = Gtk.StringList()
        self._preset_ids = list(tuner.PRESETS)
        for pid in self._preset_ids:
            self.preset_model.append(PRESET_LABELS[pid])
        self.preset_row.set_model(self.preset_model)
        self.preset_row.set_selected(self._preset_ids.index(self.tracker.preset))
        self.preset_row.connect("notify::selected", self.on_tuner_changed)
        g_tune_set.add(self.preset_row)

        self.a4_row = Adw.SpinRow.new_with_range(415, 466, 1)
        self.a4_row.set_title("기준음 A4")
        self.a4_row.set_subtitle("보통 440 Hz")
        self.a4_row.set_value(self.tracker.a4)
        self.a4_row.connect("notify::value", self.on_tuner_changed)
        g_tune_set.add(self.a4_row)
        page.add(g_tune_set)

        # ---- settings -------------------------------------------------
        g_set = Adw.PreferencesGroup(title="설정")

        self.out_row = Adw.ComboRow(title="출력 장치",
                                    subtitle="기타 소리를 내보낼 곳")
        self.out_model = Gtk.StringList()
        self.out_row.set_model(self.out_model)
        self.out_row.connect("notify::selected", self.on_output_changed)
        g_set.add(self.out_row)

        self.mono_row = Adw.SwitchRow(
            title="Input 1을 양쪽으로",
            subtitle="Input 1은 왼쪽 채널로만 들어와. 끄면 왼쪽에서만 들려")
        self.mono_row.set_active(bool(self.cfg["mono"]))
        self.mono_row.connect("notify::active", self.on_setting_changed)
        g_set.add(self.mono_row)

        self.q_row = Adw.ComboRow(title="지연", subtitle="")
        self.q_model = Gtk.StringList()
        for q in core.QUANTUM_CHOICES:
            self.q_model.append(f"{q} 샘플 · {core.quantum_latency_ms(q)} ms")
        self.q_row.set_model(self.q_model)
        self.q_row.set_selected(core.QUANTUM_CHOICES.index(self.cfg["quantum"]))
        self.q_row.connect("notify::selected", self.on_setting_changed)
        g_set.add(self.q_row)

        self.auto_row = Adw.SwitchRow(
            title="10분 무신호면 자동으로 끄기",
            subtitle="켜둔 채 잊으면 기타 험이 헤드폰에 계속 섞여. 앱이 열려 있는 동안만 동작")
        self.auto_row.set_active(bool(self.cfg["auto_stop_min"]))
        self.auto_row.connect("notify::active", self.on_setting_changed)
        g_set.add(self.auto_row)
        page.add(g_set)

        self.set_content(root)

        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.connect("close-request", self.on_close)

        self._loading = False
        self.refresh_devices()
        self.refresh_state()
        GLib.timeout_add(1500, self.tick_devices)
        GLib.timeout_add(80, self.tick_meters)
        self.start_reader()

    # ---- device / state -----------------------------------------------

    def refresh_devices(self):
        """Rebuild the sink list, keeping the saved choice selected.

        Rebuilding tears the model down row by row, and every removal emits
        notify::selected against a list that no longer matches. So the whole
        rebuild runs behind _loading, and is skipped entirely when nothing
        changed -- which is the common case on the 1.5 s poll.
        """
        sinks = core.list_sinks()
        signature = [(s.fingerprint, s.nick, s.is_default) for s in sinks]
        if signature == getattr(self, "_sink_sig", None):
            return
        self._sink_sig = signature

        self._loading = True
        try:
            self.sinks = sinks
            want = self.cfg.get("output")
            while self.out_model.get_n_items():
                self.out_model.remove(0)
            self.out_model.append(FOLLOW_DEFAULT)
            sel = 0
            for i, s in enumerate(self.sinks, start=1):
                mark = " (현재 기본)" if s.is_default else ""
                self.out_model.append(f"{s.nick}{mark}")
                if want and s.fingerprint == want:
                    sel = i
            self.out_row.set_selected(sel)
        finally:
            self._loading = False

    def refresh_state(self):
        st = core.status()
        connected, active = st["connected"], st["active"]

        self.toggle.set_sensitive(connected and not self._busy)
        self.toggle.set_label("기타 모드 끄기" if active else "기타 모드 켜기")
        if active:
            self.toggle.remove_css_class("suggested-action")
            self.toggle.add_css_class("destructive-action")
        else:
            self.toggle.remove_css_class("destructive-action")
            self.toggle.add_css_class("suggested-action")

        if not connected:
            self.banner.set_title("오디오 인터페이스가 연결되지 않았어 — USB를 꽂아줘")
            self.banner.set_revealed(True)
            self.state_lbl.set_text("")
            return

        self.banner.set_revealed(False)
        if active:
            sink = core.resolve_sink(self.cfg.get("output")) or st["default_sink"]
            nick = next((s.nick for s in self.sinks if s.name == sink), sink or "?")
            q = st["quantum"] or self.cfg["quantum"]
            self.state_lbl.set_text(
                f"{nick} 로 출력 중 · 지연 {core.quantum_latency_ms(q)} ms")
        else:
            self.state_lbl.set_text("꺼져 있음 — 레벨 미터는 계속 동작해서 게인을 미리 맞출 수 있어")

    def tick_devices(self):
        self._loading = False
        self.refresh_devices()
        self.refresh_state()
        if core.interface_connected() and (self.reader is None):
            self.start_reader()
        return True

    # ---- meters ---------------------------------------------------------

    def start_reader(self):
        if self.reader or not core.interface_connected():
            return
        self.reader = core.LevelReader(self.on_level, on_samples=self.on_samples)
        if not self.reader.start():
            self.reader = None

    def on_level(self, left_db, right_db, clipping):
        """Worker thread. Store only; the GTK timeout renders."""
        self._peaks = (left_db, right_db)
        if clipping:
            self._clipping = True

    def on_samples(self, left):
        """Worker thread: ~1 ms of numpy per chunk, so it runs here directly."""
        r = self.tracker.feed(left)
        self._reading = r
        self._reading_at = time.monotonic()

    def render_tuner(self):
        r = self._reading
        if r is None or time.monotonic() - self._reading_at > TUNER_HOLD_S:
            self._reading = None
            self.tune_note.set_text("—")
            self.tune_note.remove_css_class("ok")
            self.tune_advice.set_text("현을 쳐봐")
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
        self.tune_advice.set_text(ADVICE[r.direction])
        self.tune_advice.remove_css_class("dim-label")
        for c in ("ok", "off"):
            self.tune_advice.remove_css_class(c)
        self.tune_advice.add_css_class("ok" if r.in_tune else "off")
        self.needle.set_cents(r.cents)
        extra = "" if r.target == r.note else f"   가까운 음 {r.note} {r.note_cents:+.0f}¢"
        self.tune_info.set_text(f"{r.freq:.2f} Hz   {r.cents:+.1f}¢  (목표 {r.target_freq:.2f} Hz){extra}")

    def tick_meters(self):
        left, right = self._peaks
        self.m1.update(left)
        self.m2.update(right, show_verdict=False)
        self.render_tuner()

        if self._clipping:
            self.clip_lbl.set_markup(
                '<span foreground="#e01b24">클리핑 — 게인을 낮춰</span>')
            self._clipping = False
        else:
            self.clip_lbl.set_text("")

        self.check_auto_stop(left)
        # Levels stop arriving if the box is unplugged mid-session.
        if self.reader and self.reader._proc and self.reader._proc.poll() is not None:
            self.reader = None
            self._peaks = (None, None)
        return True

    def check_auto_stop(self, left_db):
        if not self.auto_row.get_active() or not core.is_active():
            self._silence_since = None
            return
        quiet = left_db is None or left_db <= core.METER_FLOOR_DB + 0.5
        if not quiet:
            self._silence_since = None
            return
        now = time.monotonic()
        if self._silence_since is None:
            self._silence_since = now
        elif now - self._silence_since > 600:
            self._silence_since = None
            core.stop()
            self.refresh_state()
            self.banner.set_title("10분 동안 신호가 없어서 기타 모드를 껐어")
            self.banner.set_revealed(True)

    # ---- actions ---------------------------------------------------------

    def on_toggle(self, _btn):
        if self._busy:
            return
        self._busy = True
        self.toggle.set_sensitive(False)
        turning_on = not core.is_active()

        def work():
            if turning_on:
                ok, msg = core.start(
                    sink_name=core.resolve_sink(self.cfg.get("output")),
                    mono=self.mono_row.get_active(),
                    quantum=core.QUANTUM_CHOICES[self.q_row.get_selected()])
            else:
                ok, msg = core.stop()
            GLib.idle_add(self.done, ok, msg)

        import threading
        threading.Thread(target=work, daemon=True).start()

    def done(self, ok, msg):
        self._busy = False
        self.refresh_state()
        if not ok:
            self.banner.set_title(msg)
            self.banner.set_revealed(True)
        return False

    def on_output_changed(self, *_):
        if getattr(self, "_loading", False):
            return
        i = self.out_row.get_selected()
        if i == Gtk.INVALID_LIST_POSITION or i > len(self.sinks):
            return
        self.cfg["output"] = None if i == 0 else self.sinks[i - 1].fingerprint
        core.save_config(self.cfg)
        self.reapply()

    def on_tuner_changed(self, *_):
        if getattr(self, "_loading", False):
            return
        pid = self._preset_ids[self.preset_row.get_selected()]
        a4 = float(self.a4_row.get_value())
        self.tracker.request(preset=pid, a4=a4)
        self.cfg["tuner_preset"] = pid
        self.cfg["a4"] = a4
        core.save_config(self.cfg)

    def on_setting_changed(self, *_):
        if getattr(self, "_loading", False):
            return
        self.cfg["mono"] = self.mono_row.get_active()
        self.cfg["quantum"] = core.QUANTUM_CHOICES[self.q_row.get_selected()]
        self.cfg["auto_stop_min"] = 10 if self.auto_row.get_active() else 0
        core.save_config(self.cfg)
        self.reapply()

    def reapply(self):
        """Settings only take effect on a live loopback by rebuilding it."""
        if not core.is_active():
            self.refresh_state()
            return
        self.on_toggle_restart()

    def on_toggle_restart(self):
        import threading

        def work():
            core.start(sink_name=core.resolve_sink(self.cfg.get("output")),
                       mono=self.mono_row.get_active(),
                       quantum=core.QUANTUM_CHOICES[self.q_row.get_selected()])
            GLib.idle_add(self.refresh_state)

        threading.Thread(target=work, daemon=True).start()

    def on_close(self, *_):
        if self.reader:
            self.reader.stop()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=core.APP_ID)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main():
    return App().run(sys.argv)
