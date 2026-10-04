from ironbark import devices


class FakePortAudio:
    """Stands in for PortAudio's device list, which only changes on a re-scan."""

    def __init__(self, devices_now, devices_after_rescan=None, default=0):
        self.listed = devices_now
        self.after_rescan = devices_after_rescan or devices_now
        self.default = default
        self.rescans = 0

    def install(self, monkeypatch, system_default_name):
        monkeypatch.setattr(devices, "_portaudio_outputs", lambda: list(enumerate(self.listed)))
        monkeypatch.setattr(devices, "_portaudio_default", lambda: self.default)
        monkeypatch.setattr(devices, "default_output_name", lambda: system_default_name)
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
    pa = FakePortAudio(["Headphones"])
    pa.install(monkeypatch, "Headphones")
    assert devices.find_device("DAC") is None
    assert pa.rescans == 1


def test_system_default_follows_the_live_default(monkeypatch):
    # Windows default changed to the DAC after launch; PortAudio's own default is stale.
    pa = FakePortAudio(["Headphones", "DAC"], default=0)
    pa.install(monkeypatch, "DAC")
    assert devices.find_device(None) == 1


def test_system_default_falls_back_to_portaudio_default(monkeypatch):
    pa = FakePortAudio(["Headphones"], default=0)
    pa.install(monkeypatch, "Something PortAudio never sees")
    assert devices.find_device(None) == 0
