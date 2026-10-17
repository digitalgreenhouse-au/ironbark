"""Bit-perfect on a Mac: hold the device, pick its deepest format at the file's rate, fall back
honestly, let go and put the device back as it was. No real device is touched: Core Audio's
answers are stood in for."""
import ctypes

import numpy as np
import pytest
import soundfile as sf

from ironbark import coreaudio, devices, engine
from ironbark.engine import OutputInfo, Player
from ironbark.library import Track
from ironbark.proof import Proof
from ironbark.statustext import output_line
from ironbark.tools.details import describe_proof

INT, FLOAT, NON_MIXABLE = 0xC, 0x9, 0x4C


def fmt(rate, bits, flags=INT, channels=2):
    f = coreaudio.Format()
    f.rate, f.format_id, f.flags, f.bits, f.channels = rate, coreaudio.LINEAR_PCM, flags, bits, channels
    return f


def ranged(f):
    r = coreaudio._RangedFormat()
    r.format, r.low, r.high = f, f.rate, f.rate
    return r


class FakeCoreAudio:
    """One device (id 7) with one output stream (id 8), the way a USB DAC answers."""

    def __init__(self, monkeypatch, offered, current, hog=-1):
        self.offered, self.current, self.hog, self.sets = offered, current, hog, []
        monkeypatch.setattr(coreaudio, "_get", self.get)
        monkeypatch.setattr(coreaudio, "_set", self.set)
        monkeypatch.setattr(coreaudio, "_original", {})
        monkeypatch.setattr(coreaudio.os, "getpid", lambda: 4242)

    def get(self, obj, selector, ctype, scope=coreaudio.SCOPE_GLOBAL, element=0):
        if selector == coreaudio.STREAMS:
            return [8]
        if selector == coreaudio.HOG_MODE:
            return [self.hog]
        if selector == coreaudio.PHYSICAL_FORMAT:
            return [self.current]
        if selector == coreaudio.PHYSICAL_FORMATS:
            return [ranged(f) for f in self.offered]
        return None

    def set(self, obj, selector, value, scope=coreaudio.SCOPE_GLOBAL, element=0):
        self.sets.append((obj, selector))
        if selector == coreaudio.HOG_MODE:
            if self.hog not in (-1, 4242):
                return False   # someone else has it
            self.hog = value.value
        elif selector == coreaudio.PHYSICAL_FORMAT:
            self.current = coreaudio.Format.from_buffer_copy(value)
        return True


DAC = [fmt(r, b) for r in (44100, 48000, 96000) for b in (16, 24)] + [fmt(96000, 24, NON_MIXABLE)]


def test_deepest_mixable_format_at_the_files_rate(monkeypatch):
    FakeCoreAudio(monkeypatch, DAC, fmt(48000, 16))
    best = coreaudio.best_format(8, 96000)
    assert (best.rate, best.bits, best.flags) == (96000, 24, INT)
    assert coreaudio.best_format(8, 88200) is None


def test_no_device_keeps_more_than_24_bits(monkeypatch):
    # Core Audio carries every sample as 32-bit float: 24 bits exact, a 32-bit file cut short.
    FakeCoreAudio(monkeypatch, [fmt(48000, 32, FLOAT), fmt(48000, 16)], fmt(48000, 16))
    assert coreaudio.best_format(8, 48000).usable_bits == 24
    FakeCoreAudio(monkeypatch, [fmt(48000, 32), fmt(48000, 24)], fmt(48000, 16))
    assert coreaudio.best_format(8, 48000).usable_bits == 24


def test_claim_hogs_switches_format_and_restore_puts_it_back(monkeypatch):
    ca = FakeCoreAudio(monkeypatch, DAC, fmt(48000, 16))
    claim = coreaudio.Claim(7, 44100)
    assert ca.hog == 4242 and (ca.current.rate, ca.current.bits) == (44100, 24) and claim.bits == 24
    claim.release()
    assert ca.hog == -1 and coreaudio.changed()              # the format stays until bit-perfect lets go
    coreaudio.Claim(7, 96000).release()                      # a second track: remembered format unchanged
    coreaudio.restore(keep=7)
    assert coreaudio.changed()                               # still in use bit-perfect
    coreaudio.restore()
    assert (ca.current.rate, ca.current.bits) == (48000, 16) and not coreaudio.changed()


def test_device_already_right_is_left_alone(monkeypatch):
    ca = FakeCoreAudio(monkeypatch, DAC, fmt(44100, 24))
    coreaudio.Claim(7, 44100).release()
    assert not any(sel == coreaudio.PHYSICAL_FORMAT for _, sel in ca.sets) and not coreaudio.changed()


def test_another_app_holding_the_device_is_refused(monkeypatch):
    FakeCoreAudio(monkeypatch, DAC, fmt(48000, 16), hog=999)
    with pytest.raises(coreaudio.Busy):
        coreaudio.Claim(7, 44100)


def test_rate_the_device_lacks_is_refused_and_let_go(monkeypatch):
    ca = FakeCoreAudio(monkeypatch, DAC, fmt(48000, 16))
    with pytest.raises(coreaudio.Unsupported, match="88.2 kHz"):
        coreaudio.Claim(7, 88200)
    assert ca.hog == -1


def test_same_named_displays_map_to_their_own_device(monkeypatch):
    monkeypatch.setattr(devices, "_portaudio_outputs", lambda: [(0, "Display"), (1, "Display"), (2, "DAC")])
    seen = []
    monkeypatch.setattr(coreaudio, "find_device", lambda name, nth=0: seen.append((name, nth)) or 50 + nth)
    assert devices._core_audio_id(1) == 51 and seen[-1] == ("Display", 1)
    assert devices._core_audio_id(2) == 50 and seen[-1] == ("DAC", 0)


# -- what the player says --------------------------------------------------------------------------
def test_a_device_keeping_every_bit_is_bit_perfect():
    out = OutputInfo("DAC", 96000, None, exclusive=True, device_bits=24)
    assert output_line(out, 1.0, bits=24) == ("→ DAC · 96 kHz exclusive · bit-perfect ✓", "good")


def test_device_with_fewer_bits_than_the_file_is_not_bit_perfect():
    out = OutputInfo("DAC", 44100, None, exclusive=True, device_bits=16)
    text, tone = output_line(out, 1.0, bits=24)
    assert "takes 16-bit samples" in text and "24-bit" in text and tone == "warn"
    text, tone = describe_proof(Proof(sent=10.0, checked=10.0), out, 1.0, 1.0, bits=24)
    assert "takes 16-bit samples" in text and tone == "warn"
    assert describe_proof(Proof(sent=10.0, checked=10.0), out, 1.0, 1.0, bits=16)[1] == "good"


# -- the engine holds and lets go --------------------------------------------------------------------
class Stream:
    made: list = []

    def __init__(self, **kwargs):
        self.kwargs, self.latency, self.device = kwargs, 0.1, 0
        Stream.made.append(self)

    def start(self):
        pass

    stop = abort = start

    def close(self, ignore_errors=False):
        pass


class Held:
    def __init__(self):
        self.bits, self.device, self.released = 24, 7, False

    def release(self):
        self.released = True


@pytest.fixture
def mac_card(monkeypatch, tmp_path):
    Stream.made = []
    monkeypatch.setattr(engine.sd, "OutputStream", Stream)
    monkeypatch.setattr(engine.devices, "find_device", lambda name, rescan_ok=True: 3)
    monkeypatch.setattr(engine.devices, "mixer_rate", lambda index: 48000)
    monkeypatch.setattr(engine.devices, "exclusive_supported", lambda: True)
    monkeypatch.setattr(engine.devices, "default_output_name", lambda: "DAC")
    monkeypatch.setattr(engine.devices, "has_hardware_volume", lambda name: True)
    monkeypatch.setattr(engine.sd, "query_devices", lambda device: {"name": "DAC"})
    restored = []
    monkeypatch.setattr(engine.devices, "restore_formats", lambda keep=None: restored.append(keep))
    path = tmp_path / "t.wav"
    sf.write(path, np.zeros((44100 * 5, 2)), 44100, subtype="PCM_24")
    player = Player()
    player.set_tracks([Track(path=path, title="t", duration=5.0)])
    player.refresh_hardware_volume()   # (the window's check, as bit-perfect goes on)
    player.set_exclusive(True)
    return player, restored


def test_engine_holds_the_device_and_lets_go_on_stop(monkeypatch, mac_card):
    player, restored = mac_card
    held = Held()
    monkeypatch.setattr(engine.devices, "claim", lambda index, rate: held)
    player.play(0)
    status = player.status()
    assert status.output.exclusive and status.output.device_bits == 24
    assert Stream.made[-1].kwargs["dtype"] == "int32" and not held.released
    player.stop()
    assert held.released and restored[-1] is None


def test_engine_falls_back_to_shared_and_says_why(monkeypatch, mac_card):
    player, _ = mac_card

    def refuse(index, rate):
        raise devices.Refused("the device doesn't offer 44.1 kHz")
    monkeypatch.setattr(engine.devices, "claim", refuse)
    player.play(0)
    status = player.status()
    assert not status.output.exclusive and Stream.made[-1].kwargs["dtype"] == "float32"
    assert "doesn't offer 44.1 kHz" in status.message
    player.stop()


def test_claim_is_let_go_when_portaudio_refuses_the_stream(monkeypatch, mac_card):
    player, _ = mac_card
    held = Held()
    monkeypatch.setattr(engine.devices, "claim", lambda index, rate: held)

    def picky(**kwargs):
        if kwargs["dtype"] == "int32":
            raise engine.sd.PortAudioError("Invalid sample rate")
        return Stream(**kwargs)
    monkeypatch.setattr(engine.sd, "OutputStream", picky)
    player.play(0)
    assert held.released and not player.status().output.exclusive
    player.stop()


def test_core_audio_format_struct_matches_the_c_layout():
    assert ctypes.sizeof(coreaudio.Format) == 40 and ctypes.sizeof(coreaudio._RangedFormat) == 56
