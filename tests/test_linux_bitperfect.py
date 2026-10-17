"""Bit-perfect on Linux (devices.py, session.py): only a sound card's own ALSA device (hw:) is held,
past the sound server — never another device than the one chosen; a sound-server output plays
shared and says why; the bits the card was opened with are read from ALSA's own status file. Runs
anywhere: PortAudio's device list and /proc/asound are stood in for."""

import pytest
import sounddevice as sd

from ironbark import devices, platforms

DEVICES = {
    0: {"name": "HDA Intel PCH: ALC257 Analog (hw:0,0)", "max_output_channels": 2, "default_samplerate": 48000.0},
    1: {"name": "Topping E30 II: USB Audio (hw:2,0)", "max_output_channels": 2, "default_samplerate": 44100.0},
    2: {"name": "pipewire", "max_output_channels": 64, "default_samplerate": 48000.0},
    3: {"name": "default", "max_output_channels": 64, "default_samplerate": 48000.0},
}


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(devices, "IS_LINUX", True)
    monkeypatch.setattr(devices, "IS_WINDOWS", False)
    monkeypatch.setattr(devices, "IS_MAC", False)
    monkeypatch.setattr(devices, "_tests_name_devices", True, raising=False)   # (conftest: the real claim)
    monkeypatch.setattr(platforms, "EXCLUSIVE_OUTPUT", True)
    monkeypatch.setattr(sd, "query_devices", lambda index=None, kind=None: DEVICES[index])


@pytest.mark.parametrize("name, card", [
    ("HDA Intel PCH: ALC257 Analog (hw:0,0)", (0, 0)),
    ("Topping E30 II: USB Audio (hw:2,0)", (2, 0)),
    ("HDA Intel PCH: HDMI 3 (hw:0,9)", (0, 9)),
    ("pipewire", None), ("default", None), ("sysdefault", None), ("", None),
    ("Something (hw:1,0) else", None),   # (only at the end of the name)
])
def test_a_sound_cards_own_device_is_told_by_its_name(name, card):
    assert devices.alsa_card(name) == card


def test_bit_perfect_holds_a_sound_card_and_refuses_the_sound_server(linux):
    assert isinstance(devices.claim(1, 96000), devices.Claim)
    for server in (2, 3):
        with pytest.raises(devices.Refused, match=r"^choose .*\(hw:.* under Output"):
            devices.claim(server, 44100)


def test_a_hw_stream_has_no_platform_settings(linux):
    assert devices.exclusive_settings() is None


@pytest.mark.parametrize("status, bits", [
    ("access: RW_INTERLEAVED\nformat: S24_3LE\nsubformat: STD\nchannels: 2\nrate: 96000 (96000/1)\n", 24),
    ("access: MMAP_INTERLEAVED\nformat: S32_LE\nchannels: 2\nrate: 44100 (44100/1)\n", 32),
    ("format: S16_LE\n", 16),
])
def test_the_bits_the_card_was_opened_with_come_from_alsa(tmp_path, status, bits):
    sub = tmp_path / "proc" / "asound" / "card2" / "pcm0p" / "sub0"
    sub.mkdir(parents=True)
    (sub / "hw_params").write_text(status, encoding="ascii")
    assert devices.alsa_open_bits(2, 0, root=str(tmp_path)) == bits


def test_a_card_not_open_or_not_there_tells_nothing(tmp_path):
    sub = tmp_path / "proc" / "asound" / "card2" / "pcm0p" / "sub0"
    sub.mkdir(parents=True)
    (sub / "hw_params").write_text("closed\n", encoding="ascii")
    assert devices.alsa_open_bits(2, 0, root=str(tmp_path)) is None
    assert devices.alsa_open_bits(7, 0, root=str(tmp_path)) is None


def test_exclusive_bits_reads_the_open_card(linux, monkeypatch):
    class Stream:
        device = 1
    monkeypatch.setattr(devices, "alsa_open_bits", lambda card, device: 24 if (card, device) == (2, 0) else None)
    assert devices.exclusive_bits(Stream()) == 24
    Stream.device = 2   # (the sound server: no card to read)
    assert devices.exclusive_bits(Stream()) is None


def test_a_session_on_the_sound_server_plays_shared_and_says_why(linux, monkeypatch):
    """Through the real _Session._open_stream: bit-perfect asked for on `pipewire` opens a shared
    stream (never a different device) with a note naming what to choose."""
    from ironbark import session

    opened = []

    class FakeStream:
        def __init__(self, device=None, extra_settings=None, **kwargs):
            opened.append((device, kwargs.get("dtype"), extra_settings))
            self.device = device

        def start(self):
            pass

        def close(self, ignore_errors=False):
            pass

    monkeypatch.setattr(sd, "OutputStream", FakeStream)
    monkeypatch.setattr(sd, "RawOutputStream", FakeStream)
    monkeypatch.setattr(devices, "exclusive_supported", lambda: True)
    fake = session._Session.__new__(session._Session)
    fake.samplerate, fake.channels, fake.note, fake._claim = 44100, 2, "", None
    fake._stream_kwargs = lambda dtype, callback=None: {"dtype": dtype}
    stream, exclusive = session._Session._open_stream(fake, 2, True)
    assert not exclusive and opened == [(2, "float32", None)]
    assert fake.note.startswith("Bit-perfect: choose ") and "under Output" in fake.note
    opened.clear()
    fake.note = ""
    stream, exclusive = session._Session._open_stream(fake, 1, True)
    assert exclusive and opened[0][0] == 1 and opened[0][1] == "int32" and not fake.note


# -- the card's own device when the sound server had it at start-up ---------------------------------
CARDS = """ 0 [PCH            ]: HDA-Intel - HDA Intel PCH
                      HDA Intel PCH at 0xf7d10000 irq 33
 1 [E30II          ]: USB-Audio - E30 II
                      Topping E30 II at usb-0000:00:14.0-2, high speed
"""
PCM = """00-00: CX20751/2 Analog : CX20751/2 Analog : playback 1 : capture 1
00-03: HDMI 0 : HDMI 0 : playback 1
00-02: Alt Analog : Alt Analog : capture 1
01-00: USB Audio : USB Audio : playback 1
"""


@pytest.fixture
def asound(tmp_path):
    folder = tmp_path / "proc" / "asound"
    folder.mkdir(parents=True)
    (folder / "cards").write_text(CARDS, encoding="utf-8")
    (folder / "pcm").write_text(PCM, encoding="utf-8")
    return str(tmp_path)


def test_alsas_own_list_names_each_playback_device_as_portaudio_does(asound):
    assert devices.alsa_playback_names(root=asound) == [
        "HDA Intel PCH: CX20751/2 Analog (hw:0,0)", "HDA Intel PCH: HDMI 0 (hw:0,3)", "E30 II: USB Audio (hw:1,0)"]
    assert devices.alsa_playback_names(root=asound + "/nowhere") is None


def test_a_card_the_sound_server_held_at_start_up_is_still_offered(linux, monkeypatch, asound):
    """PortAudio skipped the analog card (PipeWire had it open at start-up); ALSA lists it."""
    real = devices.alsa_playback_names
    monkeypatch.setattr(devices, "alsa_playback_names", lambda root="": real(root=asound))
    monkeypatch.setattr(devices, "_portaudio_outputs", lambda: [
        (0, "HDA Intel PCH: HDMI 0 (hw:0,3)"), (1, "Gone DAC: USB Audio (hw:2,0)"), (2, "pipewire"), (3, "default")])
    assert devices.output_names() == ["HDA Intel PCH: HDMI 0 (hw:0,3)", "pipewire", "default",
                                      "HDA Intel PCH: CX20751/2 Analog (hw:0,0)", "E30 II: USB Audio (hw:1,0)"]
    assert devices._system_lists("HDA Intel PCH: CX20751/2 Analog (hw:0,0)")   # (so choosing it re-scans)


def test_choosing_it_rescans_once_the_card_is_free(linux, monkeypatch, asound):
    real = devices.alsa_playback_names
    monkeypatch.setattr(devices, "alsa_playback_names", lambda root="": real(root=asound))
    listed = [(0, "pipewire")]
    rescans = []
    monkeypatch.setattr(devices, "_portaudio_outputs", lambda: list(listed))
    monkeypatch.setattr(devices, "_streams", __import__("weakref").WeakSet())

    def rescan():
        rescans.append(1)
        listed.append((1, "HDA Intel PCH: CX20751/2 Analog (hw:0,0)"))   # (the sound server has let go)

    monkeypatch.setattr(devices, "rescan", rescan)
    assert devices.find_device("HDA Intel PCH: CX20751/2 Analog (hw:0,0)") == 1 and rescans == [1]


def test_a_card_still_busy_says_so_rather_than_not_connected(linux, monkeypatch, asound):
    real = devices.alsa_playback_names
    monkeypatch.setattr(devices, "alsa_playback_names", lambda root="": real(root=asound))
    busy = devices.missing("HDA Intel PCH: CX20751/2 Analog (hw:0,0)")
    assert isinstance(busy, devices.DeviceBusy) and "is busy" in str(busy)
    gone = devices.missing("Gone DAC: USB Audio (hw:2,0)")
    assert type(gone) is devices.DeviceMissing and "isn't connected" in str(gone)
    assert type(devices.missing("pipewire")) is devices.DeviceMissing


def test_without_alsas_list_portaudios_is_all_there_is(linux, monkeypatch):
    monkeypatch.setattr(devices, "alsa_playback_names", lambda root="": None)
    monkeypatch.setattr(devices, "_portaudio_outputs", lambda: [(0, "pipewire")])
    assert devices.output_names() == ["pipewire"] and devices._system_lists("anything")


# -- the note on a sound-server output: what to choose comes first ----------------------------------
def test_the_note_names_the_card_to_choose_first(linux, monkeypatch, asound):
    real = devices.alsa_playback_names
    monkeypatch.setattr(devices, "alsa_playback_names", lambda root="": real(root=asound))
    with pytest.raises(devices.Refused) as refused:
        devices.claim(2, 44100)
    text = str(refused.value)   # (two cards besides HDMI here: the general words)
    assert refused.value.advice and text.startswith("choose a sound card ending in (hw:…) under Output")
    one = "00-00: ALC257 Analog : ALC257 Analog : playback 1 : capture 1\n00-03: HDMI 0 : HDMI 0 : playback 1\n"
    (__import__("pathlib").Path(asound) / "proc" / "asound" / "pcm").write_text(one, encoding="utf-8")
    with pytest.raises(devices.Refused) as refused:
        devices.claim(2, 44100)
    assert str(refused.value).startswith("choose “HDA Intel PCH: ALC257 Analog (hw:0,0)” under Output")


def test_the_status_line_keeps_what_to_choose_when_it_is_narrow(linux, monkeypatch, asound):
    """At the side player's default width the line is cut after two lines: the card's name, which
    comes first now, is never what gets cut."""
    from ironbark import ui

    one = "00-00: CX20751/2 Analog : CX20751/2 Analog : playback 1 : capture 1\n"
    (__import__("pathlib").Path(asound) / "proc" / "asound" / "pcm").write_text(one, encoding="utf-8")
    real = devices.alsa_playback_names
    monkeypatch.setattr(devices, "alsa_playback_names", lambda root="": real(root=asound))
    with pytest.raises(devices.Refused) as refused:
        devices.claim(2, 44100)
    note = f"Bit-perfect: {refused.value} — playing shared for now"
    upper, lower = ui.split_status(note, 330, lambda text: 7 * len(text))   # (≈ 386 px window, 7 px a letter)
    assert "(hw:0,0)" in upper + lower
