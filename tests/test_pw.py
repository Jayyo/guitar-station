"""Graph parsing from a trimmed, synthetic pw-dump."""
import copy
import json
from pathlib import Path

import pytest

from guitarstation import pw

DUMP = json.loads((Path(__file__).parent / "fixtures" / "pw-dump.json").read_text())
IFACE = "alsa_input.usb-Example_Audio_Interface_00000000-00.analog-stereo"


@pytest.fixture
def graph():
    return pw.parse(copy.deepcopy(DUMP))


def test_sources_and_sinks(graph):
    assert {n.name for n in graph.sources} == {
        IFACE, "alsa_input.usb-Example_Webcam_1234-02.analog-stereo",
        "alsa_input.usb-Example_Headset-00.mono-fallback", "virtual_source"}
    assert [s.name for s in graph.sinks][0] == graph.default_sink
    assert graph.default_sink == "alsa_output.pci-0000_0a_00.4.analog-stereo"


def test_channels_follow_port_order(graph):
    webcam = graph.find_source({"node": "alsa_input.usb-Example_Webcam_1234-02.analog-stereo"})
    assert [(c.position, c.port, c.number) for c in webcam.channels] == [
        ("FL", "capture_FL", 1), ("FR", "capture_FR", 2)]
    headset = graph.find_source({"node": "alsa_input.usb-Example_Headset-00.mono-fallback"})
    assert [c.position for c in headset.channels] == ["MONO"]


def test_aux_channels_are_numbered_from_one():
    assert pw.Channel("AUX2", "capture_AUX2", 2).number == 3
    assert pw.Channel("FR", "capture_FR", 1).number == 2


def test_selector_survives_a_renamed_node(graph):
    iface = graph.find_source({"node": IFACE})
    sel = iface.selector
    assert sel == {"device": "alsa_card.usb-Example_Audio_Interface_00000000-00",
                   "pcm": 0, "node": IFACE}
    renamed = copy.deepcopy(DUMP)
    for o in renamed:
        if o.get("id") == 40:
            o["info"]["props"]["node.name"] = IFACE.replace("analog-stereo", "pro-input-0")
    again = pw.parse(renamed).find_source(sel)
    assert again is not None and again.name.endswith("pro-input-0")


def test_virtual_nodes_fall_back_to_the_node_name(graph):
    v = graph.find_source({"node": "virtual_source"})
    assert v.selector == {"node": "virtual_source"}
    assert graph.find_source({"node": "nope"}) is None
    assert graph.find_source(None) is None
    assert graph.find_source("garbage") is None


def test_suggestion_skips_webcams_and_headsets(graph):
    assert graph.suggested_source().name == IFACE
    no_iface = [o for o in DUMP if o.get("id") not in (40, 50, 51)]
    assert pw.parse(copy.deepcopy(no_iface)).suggested_source() is None


def test_channel_lookup_falls_back_to_index(graph):
    iface = graph.find_source({"node": IFACE})
    assert iface.channel("FR").port == "capture_FR"
    assert iface.channel("AUX1", 1).position == "FR"
    assert iface.channel("AUX7", 9) is None


def test_link_listing():
    text = """alsa_input.x:capture_FL
  |-> input.gm:input_FL
  |-> input.gm:input_FR
output.gm:output_FL
  |-> speakers:playback_FL
input.gm:input_FL
  |<- alsa_input.x:capture_FL
"""
    assert pw.links(text) == {
        ("alsa_input.x:capture_FL", "input.gm:input_FL"),
        ("alsa_input.x:capture_FL", "input.gm:input_FR"),
        ("output.gm:output_FL", "speakers:playback_FL"),
    }
