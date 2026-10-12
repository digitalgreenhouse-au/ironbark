"""Settings → Volume on my amplifier: an output that feeds an amplifier or receiver has no volume
of its own, and that's as it should be — the amplifier's knob sets the level. Per output, by name:
the status line is green there, and the ear checks stand down; every other output with no volume
of its own keeps the warning. Through the stand-in sound card: no device is ever opened."""
import numpy as np
import pytest
import soundfile as sf
from test_fix_playback import RATE, exclusive_card, fake_card  # noqa: F401 (fixtures)

from ironbark import engine, settings, statustext
from ironbark.engine import OutputInfo, Player
from ironbark.library import Track


def test_the_line_is_green_on_an_amplifiers_output():
    alone = OutputInfo("DAC", 96000, None, exclusive=True, hardware_volume=False, device_bits=24)
    assert statustext.output_line(alone, 1.0, bits=24) == (
        "→ DAC · 96 kHz exclusive · bit-perfect ✓ · no device volume: use the player or headphone volume", "warn")
    amp = OutputInfo("DAC", 96000, None, exclusive=True, hardware_volume=False, device_bits=24, amp_volume=True)
    assert statustext.output_line(amp, 1.0, bits=24) == (
        "→ DAC · 96 kHz exclusive · bit-perfect ✓ · volume on your amplifier", "good")
    assert statustext.output_line(amp, 0.5, bits=24)[1] == "warn"            # below 100%: still not bit-perfect
    assert statustext.output_line(amp, 1.0, bits=32)[1] == "warn"            # nor with bits lost
    own = OutputInfo("DAC", 96000, None, exclusive=True, hardware_volume=True, amp_volume=True)
    assert statustext.output_line(own, 1.0)[0].endswith("volume by device ✓")  # one of its own: says so


def test_the_player_knows_which_outputs_feed_an_amplifier(tmp_path, exclusive_card, monkeypatch):
    monkeypatch.setattr(engine.devices, "has_hardware_volume", lambda name: False)
    deep = tmp_path / "deep.wav"
    sf.write(deep, np.zeros((20000, 2)), RATE, subtype="PCM_24")
    p = Player()
    p.set_amp_outputs(["Test DAC"])                   # the system default's name (see exclusive_card)
    assert not p.amp_volume                           # (the default not checked yet)
    assert p.refresh_hardware_volume() is False and p.amp_volume
    p.set_exclusive(True)
    p.set_tracks([Track(deep, "deep")])
    p.play(0)
    status = p.status()
    assert status.output.amp_volume and status.output.hardware_volume is False
    assert statustext.output_line(status.output, 1.0, bits=24)[1] == "good"
    p.set_amp_outputs(["Another amp"])                # not this output
    assert not p.amp_volume and not p.status().output.amp_volume
    p.set_amp_outputs(["Test DAC"])
    p.set_exclusive(False)
    p.play(0)
    assert not p.status().output.amp_volume           # shared mode: the Windows volume works anyway
    p.close()


def test_a_chosen_output_is_checked_by_its_own_name(monkeypatch):
    monkeypatch.setattr(engine.devices, "has_hardware_volume", lambda name: False)
    monkeypatch.setattr(engine.devices, "default_output_name", lambda: "Laptop speakers")
    p = Player()
    p.set_amp_outputs(["USB DAC"])
    assert p.refresh_hardware_volume("USB DAC") is False and p.amp_volume       # about to be chosen
    p.set_output_device("USB DAC")
    assert p.output_name() == "USB DAC"
    assert p.refresh_hardware_volume(None) is False and not p.amp_volume        # the default: another output
    p.set_output_device(None)
    assert p.output_name() == "Laptop speakers"
    p.close()


def test_a_new_default_output_is_not_taken_for_the_amplifiers(monkeypatch):
    names = iter(["USB DAC", "Headphones"])
    monkeypatch.setattr(engine.devices, "default_output_name", lambda: next(names))
    monkeypatch.setattr(engine.devices, "has_hardware_volume", lambda name: False)
    p = Player()
    p.set_amp_outputs(["USB DAC"])
    p.refresh_hardware_volume()
    assert p.amp_volume
    with pytest.raises(engine.devices.DefaultChanged):
        p.check_default()                             # Windows made the headphones the default
    assert not p.amp_volume and p.hardware_volume is None
    p.close()


def test_the_setting_keeps_only_names():
    assert settings.DEFAULTS["amp_volume_outputs"] == []
    assert settings._checked({"amp_volume_outputs": ["USB DAC", 3, "", None]}) == {"amp_volume_outputs": ["USB DAC"]}
    assert settings._checked({"amp_volume_outputs": "USB DAC"}) == {}
