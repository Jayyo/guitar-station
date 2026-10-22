"""Config validation, monitoring plans and the systemd-run command line."""
import copy
import json
from pathlib import Path

import pytest

from guitarstation import core, monitor, pw

DUMP = json.loads((Path(__file__).parent / "fixtures" / "pw-dump.json").read_text())
IFACE = "alsa_input.usb-Example_Audio_Interface_00000000-00.analog-stereo"


@pytest.fixture
def graph():
    return pw.parse(copy.deepcopy(DUMP))


@pytest.mark.parametrize("raw", [
    None, [], "x", 3, {"input": "x", "latency": 99, "a4": "440"},
    {"tuner_preset": []}, {"tuner_preset": {"a": 1}}, {"a4": 10 ** 400}, {"a4": True},
    {"latency": True}, {"channel_index": -1}, {"channel_index": 10 ** 9},
    {"input": {"node": ""}}, {"output": {"node": 5}},
])
def test_bad_config_falls_back_to_defaults(raw):
    assert core.validate_config(raw) == core.DEFAULT_CONFIG


def test_config_keeps_valid_fields():
    raw = {"input": {"node": "n", "device": "d", "pcm": 1, "junk": 1}, "channel": "FR",
           "channel_index": 1, "output": {"node": "s"}, "latency": 512,
           "auto_stop": True, "tuner_preset": "guitar6", "a4": 432}
    cfg = core.validate_config(raw)
    assert cfg["input"] == {"node": "n", "device": "d", "pcm": 1}
    assert (cfg["channel"], cfg["channel_index"], cfg["latency"]) == ("FR", 1, 512)
    assert cfg["output"] == {"node": "s"} and cfg["auto_stop"] is True
    assert (cfg["tuner_preset"], cfg["a4"]) == ("guitar6", 432.0)


@pytest.mark.parametrize("a4", [float("nan"), 1e9, 100])
def test_out_of_range_reference_pitch_is_dropped(a4):
    assert core.validate_config({"a4": a4})["a4"] == 440.0


def test_save_and_load_use_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = dict(core.DEFAULT_CONFIG, latency=128)
    core.save_config(cfg)
    assert (tmp_path / "guitar-station" / "config.json").exists()
    assert core.load_config()["latency"] == 128
    (tmp_path / "guitar-station" / "config.json").write_text("null")
    assert core.load_config() == core.DEFAULT_CONFIG


def test_plan_errors(graph):
    base = dict(core.DEFAULT_CONFIG)
    with pytest.raises(core.PlanError, match="no-pipewire"):
        core.make_plan(base, None)
    with pytest.raises(core.PlanError, match="no-input"):
        core.make_plan(base, graph)
    with pytest.raises(core.PlanError, match="no-channel"):
        core.make_plan(dict(base, input={"node": IFACE}, channel="AUX9", channel_index=9), graph)
    with pytest.raises(core.PlanError, match="no-output"):
        core.make_plan(dict(base, input={"node": IFACE}, channel="FL",
                            output={"node": "unplugged"}), graph)


def test_plan_for_the_second_input(graph):
    cfg = dict(core.DEFAULT_CONFIG, input={"node": IFACE}, channel="FR",
               output={"node": "alsa_output.usb-Example_Headset-00.analog-stereo"}, latency=128)
    plan = core.make_plan(cfg, graph)
    assert (plan.node, plan.port, plan.channel, plan.channel_index) == (IFACE, "capture_FR", "FR", 1)
    assert plan.sink == "alsa_output.usb-Example_Headset-00.analog-stereo"
    assert plan.device == "alsa_card.usb-Example_Audio_Interface_00000000-00" and plan.pcm == 0


def test_unit_command_line(graph, monkeypatch):
    monkeypatch.setenv("PIPEWIRE_RUNTIME_DIR", "/run/test-pw")
    plan = core.make_plan(dict(core.DEFAULT_CONFIG, input={"node": IFACE}, channel="FL"), graph)
    argv = core.unit_argv(plan)
    assert argv[:2] == ["systemd-run", "--user"]
    assert "--collect" in argv and "--service-type=exec" in argv
    assert "--setenv=PIPEWIRE_RUNTIME_DIR=/run/test-pw" in argv
    assert any(a.startswith("--setenv=PYTHONPATH=") for a in argv)
    tail = argv[argv.index("--"):]
    assert tail[2:4] == ["-m", "guitarstation.monitor"]
    assert tail[tail.index("--port") + 1] == "capture_FL"
    assert tail[tail.index("--latency") + 1] == "256"
    assert "--sink" not in tail
    assert not any("force-quantum" in a for a in argv)


def test_capture_command_keeps_device_channels(graph):
    node = graph.find_source({"node": IFACE})
    argv = core.capture_argv(node)
    assert argv[argv.index("--channels") + 1] == "2"
    assert argv[argv.index("--channel-map") + 1] == "FL,FR"
    assert any("dont-remix" in a for a in argv)


@pytest.mark.parametrize("db,verdict", [
    (-60, "idle"), (-30, "low"), (-14, "good"), (-6, "good"), (-5, "warm"), (-3, "hot"), (0, "hot")])
def test_gain_verdict(db, verdict):
    assert core.gain_verdict(db) == verdict


def test_link_plan_adds_and_removes():
    current = {("dev:capture_FL", "input.gm:input_FL"), ("dev:capture_FR", "input.gm:input_FR"),
               ("other:out", "speakers:in")}
    add, remove = monitor.plan_links(current, "dev:capture_FL", "gm")
    assert add == [("dev:capture_FL", "input.gm:input_FR")]
    assert remove == [("dev:capture_FR", "input.gm:input_FR")]
    add, remove = monitor.plan_links(current, None, "gm")
    assert add == [] and len(remove) == 2


def test_loopback_props():
    argv = monitor.loopback_argv("gm", None, 256)
    cap = next(a for a in argv if a.startswith("--capture-props="))
    play = next(a for a in argv if a.startswith("--playback-props="))
    assert "node.autoconnect=false" in cap and "node.latency=256/48000" in cap
    assert "target.object" not in play
    pinned = next(a for a in monitor.loopback_argv("gm", "hp", 0) if a.startswith("--playback-props="))
    assert 'target.object="hp"' in pinned and "node.dont-reconnect=true" in pinned
    assert "node.latency" not in pinned


def test_property_values_are_quoted():
    assert monitor.spa_string('my "sink"') == '"my \\"sink\\""'
    play = next(a for a in monitor.loopback_argv("gm", 'odd name "x"', 0)
                if a.startswith("--playback-props="))
    assert 'target.object="odd name \\"x\\""' in play


class FakeArgs:
    name, node, port, channel, channel_index = "gm", "dev", "capture_FL", "FL", 0
    device = pcm = latency = None
    sink = "hp"


def test_router_stops_when_the_pinned_output_is_gone(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10):
        calls.append(argv)
        if argv[:2] == ["pw-link", "-i"]:
            return 0, "speakers:playback_FL\nspeakers:playback_FR\n"
        return 0, ""
    monkeypatch.setattr(monitor, "_run", fake_run)
    r = monitor.Router(FakeArgs())
    assert r.tick() is True          # one miss could be a hiccup
    assert r.tick() is False         # two in a row: stop


def test_stop_failure_is_reported(monkeypatch):
    monkeypatch.setattr(core, "_run", lambda argv, timeout=15: (1, "", "Access denied\n"))
    ok, msg = core.stop()
    assert not ok and msg == "stop-failed:Access denied"
