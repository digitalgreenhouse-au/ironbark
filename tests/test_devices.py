import ctypes
import weakref

from ironbark import devices


class FakePortAudio:
    """Stands in for PortAudio's device list, which only changes on a re-scan."""

    def __init__(self, devices_now, devices_after_rescan=None, default=0):
        self.listed = devices_now
        self.after_rescan = devices_after_rescan or devices_now
        self.default = default
        self.rescans = 0

    def install(self, monkeypatch, system_default_name):
        monkeypatch.setattr(devices, "_streams", weakref.WeakSet())  # (no other test's streams)
        monkeypatch.setattr(devices, "_portaudio_outputs", lambda: list(enumerate(self.listed)))
        monkeypatch.setattr(devices, "_portaudio_default", lambda: self.default)
        monkeypatch.setattr(devices, "default_output_name", lambda: system_default_name)
        monkeypatch.setattr(devices, "_system_lists", lambda name: name in self.after_rescan)  # Windows' live list
        monkeypatch.setattr(devices, "rescan", self._rescan)

    def _rescan(self):
        self.rescans += 1
        self.listed = self.after_rescan


def test_named_device_found_without_rescan(monkeypatch):
    pa = FakePortAudio(["Headphones", "DAC"])
    pa.install(monkeypatch, "Headphones")
    assert devices.find_device("DAC") == 1
    assert pa.rescans == 0


def test_device_plugged_in_after_launch_triggers_one_rescan(monkeypatch):
    pa = FakePortAudio(["Headphones"], ["Headphones", "DAC"])
    pa.install(monkeypatch, "Headphones")
    assert devices.find_device("DAC") == 1
    assert pa.rescans == 1


def test_missing_named_device_returns_none(monkeypatch):
    # Windows doesn't list it either: a re-scan wouldn't find it, so there isn't one.
    pa = FakePortAudio(["Headphones"])
    pa.install(monkeypatch, "Headphones")
    assert devices.find_device("DAC") is None
    assert pa.rescans == 0


def test_no_rescan_while_a_stream_is_open(monkeypatch):
    # A re-scan would pull the open stream (an ABX test's, say) out from under it.
    pa = FakePortAudio(["Headphones"], ["Headphones", "DAC"])
    pa.install(monkeypatch, "Headphones")

    class Stream:
        pass

    stream = Stream()
    devices.holding(stream)
    try:
        assert devices.find_device("DAC") is None and pa.rescans == 0
    finally:
        devices.released(stream)
    assert not devices.streams_open()
    assert devices.find_device("DAC") == 1 and pa.rescans == 1
    assert devices.find_device("DAC", rescan_ok=False) == 1


def test_system_default_follows_the_live_default(monkeypatch):
    # Windows default changed to the DAC after launch; PortAudio's own default is stale.
    pa = FakePortAudio(["Headphones", "DAC"], default=0)
    pa.install(monkeypatch, "DAC")
    assert devices.find_device(None) == 1


def test_system_default_falls_back_to_portaudio_default(monkeypatch):
    pa = FakePortAudio(["Headphones"], default=0)
    pa.install(monkeypatch, "Something PortAudio never sees")
    assert devices.find_device(None) == 0


def test_a_slow_volume_query_keeps_the_last_answer(monkeypatch):
    """A query that times out mustn't turn a known "has a volume control" into "don't know"
    (the player would then turn the volume down for nothing)."""
    monkeypatch.setattr(devices, "IS_WINDOWS", True)
    monkeypatch.setattr(devices, "_volume_answers", {})
    monkeypatch.setattr(devices, "_com_call", lambda fn, default=None, timeout=3.0: True)
    assert devices.has_hardware_volume("DAC") is True
    monkeypatch.setattr(devices, "_com_call", lambda fn, default=None, timeout=3.0: default)  # timed out
    assert devices.has_hardware_volume("DAC") is True
    assert devices.has_hardware_volume("Never asked about") is None


def _wave(tag, bits, valid=0, sub=None):
    wave = devices._WaveFormat(wFormatTag=tag, nChannels=2, nSamplesPerSec=44100, wBitsPerSample=bits,
                               cbSize=22 if tag == 0xFFFE else 0, wValidBitsPerSample=valid)
    if sub is not None:
        wave.SubFormat[0] = sub
    raw = bytes(wave)
    return raw if tag == 0xFFFE else raw[:18]


def test_what_an_exclusive_stream_sends_the_device():
    assert devices.sample_bits(_wave(1, 16)) == 16                        # plain 16-bit PCM
    assert devices.sample_bits(_wave(0xFFFE, 32, 24, sub=1)) == 24        # 24 bits in a 32-bit container
    assert devices.sample_bits(_wave(0xFFFE, 32, 32, sub=1)) == 32
    assert devices.sample_bits(_wave(0xFFFE, 32, 32, sub=3)) == devices.FLOAT32_BITS  # 32-bit float
    assert devices.sample_bits(_wave(3, 32)) == devices.FLOAT32_BITS
    assert devices.sample_bits(b"") is None
    assert devices.exclusive_bits(object()) is None                       # a stand-in stream: can't tell
    assert ctypes.sizeof(devices._WaveFormat) == 40                       # WAVEFORMATEXTENSIBLE's size
