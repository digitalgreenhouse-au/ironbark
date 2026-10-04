"""0.9.0: the CUE sheet parser (shared by the pre-emphasis flag now and CUE playback later)."""

import numpy as np
import pytest
import soundfile as sf
from ironbark.tagio.flac import CueSheet as FlacCueSheet
from ironbark.tagio.flac import CueSheetIndex as CueSheetTrackIndex
from ironbark.tagio.flac import CueSheetTrack, FLACFile

from ironbark import cuesheet
from ironbark.cuesheet import format_time, frames_to_samples, parse, parse_time

# A disc ripped to one file, as rippers write it: disc-level CD-Text and REM lines, a pregap
# taken from the file (INDEX 00), a hidden PREGAP of silence, flags, ISRC.
IMAGE = """REM GENRE Rock
REM DATE 1985
REM DISCID 8A0B6C0D
REM COMMENT "ExactAudioCopy v1.6"
CATALOG 0077774600125
PERFORMER "The Band"
TITLE "Live at the Hall"
FILE "The Band - Live at the Hall.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Opening"
    PERFORMER "The Band"
    ISRC USABC8500001
    FLAGS PRE
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Song"
    SONGWRITER "A. Writer"
    FLAGS DCP PRE
    INDEX 00 04:10:30
    INDEX 01 04:12:00
  TRACK 03 AUDIO
    TITLE "Encore"
    PREGAP 00:02:00
    INDEX 01 08:00:74
"""

# EAC's "noncompliant" sheet for separate files with gaps appended to the previous track:
# track 2's INDEX 00 is under file 1, its INDEX 01 under file 2.
SEPARATE = """FILE "01 - Opening.wav" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    INDEX 00 04:10:30
FILE "02 - Second Song.wav" WAVE
    INDEX 01 00:00:00
  TRACK 03 AUDIO
    INDEX 00 03:59:00
FILE "03 - Encore.wav" WAVE
    INDEX 01 00:00:00
"""


# -- times ---------------------------------------------------------------------------------
def test_times_are_cd_frames_of_a_75th_of_a_second():
    assert parse_time("00:00:00") == 0
    assert parse_time("00:01:00") == 75
    assert parse_time("00:00:74") == 74
    assert parse_time("04:12:00") == (4 * 60 + 12) * 75
    assert parse_time("120:00:00") == 120 * 60 * 75     # long files: minutes past 99 are fine
    assert format_time(parse_time("08:00:74")) == "08:00:74"


@pytest.mark.parametrize("bad", ["00:60:00", "00:00:75", "1:2", "aa:bb:cc", ""])
def test_bad_times_are_refused(bad):
    with pytest.raises(ValueError):
        parse_time(bad)


def test_frames_to_samples_is_exact_at_common_rates():
    assert frames_to_samples(1, 44100) == 588            # one CD sector
    assert frames_to_samples(75, 44100) == 44100
    assert frames_to_samples(parse_time("04:12:00"), 44100) == 252 * 44100
    for rate in (48000, 88200, 96000, 176400, 192000):
        assert frames_to_samples(75, rate) == rate
        assert frames_to_samples(1, rate) * 75 == rate   # whole samples per frame


# -- the text format -----------------------------------------------------------------------
def test_single_file_image():
    sheet = parse(IMAGE)
    assert sheet.warnings == []
    assert (sheet.performer, sheet.title, sheet.catalog) == ("The Band", "Live at the Hall", "0077774600125")
    assert sheet.rem == {"GENRE": "Rock", "DATE": "1985", "DISCID": "8A0B6C0D", "COMMENT": "ExactAudioCopy v1.6"}
    assert sheet.files == ["The Band - Live at the Hall.flac"]
    one, two, three = sheet.tracks
    assert [t.number for t in sheet.tracks] == [1, 2, 3]
    assert one.title == "Opening" and one.isrc == "USABC8500001" and one.pre_emphasis
    assert two.flags == {"DCP", "PRE"} and two.songwriter == "A. Writer"
    assert two.indexes == {0: parse_time("04:10:30"), 1: parse_time("04:12:00")}
    assert two.start == parse_time("04:12:00")
    assert three.pregap == 150 and not three.pre_emphasis and three.start == parse_time("08:00:74")
    assert all(t.file == "The Band - Live at the Hall.flac" and t.file_type == "WAVE" for t in sheet.tracks)


def test_track_ends_where_the_next_one_starts():
    sheet = parse(IMAGE)
    one, two, three = sheet.tracks
    # The gap before track 2 (INDEX 00 -> 01) stays at the end of track 1: nothing skipped.
    assert sheet.end_sample(one, 44100) == two.start_sample(44100) == 252 * 44100
    assert sheet.end_sample(two, 44100) == frames_to_samples(parse_time("08:00:74"), 44100)
    assert sheet.end_sample(three, 44100) is None          # to the end of the file
    assert two.index_sample(0, 44100) == frames_to_samples(parse_time("04:10:30"), 44100)


def test_several_files_with_gaps_under_the_previous_file():
    sheet = parse(SEPARATE)
    assert sheet.warnings == []
    assert sheet.files == ["01 - Opening.wav", "02 - Second Song.wav", "03 - Encore.wav"]
    one, two, three = sheet.tracks
    assert two.file == "02 - Second Song.wav"          # where INDEX 01 is
    assert two.index_files == {0: "01 - Opening.wav", 1: "02 - Second Song.wav"}
    assert two.start == 0 and two.indexes[0] == parse_time("04:10:30")
    assert [t.number for t in sheet.tracks_in("01 - Opening.wav")] == [1]
    assert [t.number for t in sheet.tracks_in(r"C:\Music\Album\02 - SECOND song.WAV")] == [2]
    assert sheet.end_sample(one, 44100) is None          # whole of file 1, gap included
    assert three.file == "03 - Encore.wav"


def test_forgiving_with_odd_lines():
    text = ('\ufeffPERFORMER Unquoted Name\nFILE Track One.wav WAVE\n  TRACK 01 AUDIO\n'
            '    FLAGS PRE SOMETHING\n    INDEX 01 00:00:00\n  TRACK 03 AUDIO\n    INDEX 01 00:99:00\n'
            '    INDEX 01 01:00:00\n  BOGUS line here\n  TRACK xx AUDIO\n')
    sheet = parse(text)
    assert sheet.performer == "Unquoted Name"
    assert sheet.files == ["Track One.wav"] and sheet.tracks[0].file_type == "WAVE"
    assert sheet.tracks[0].flags == {"PRE", "SOMETHING"}
    assert sheet.tracks[1].indexes == {1: 75 * 60}       # the bad time skipped, the good one kept
    notes = " | ".join(sheet.warnings)
    assert "unknown flag" in notes and "doesn't follow" in notes and "BOGUS" in notes
    assert "time out of range" in notes and "line 10" in notes


def test_windows_path_and_quotes_kept_whole():
    sheet = parse('FILE "C:\\Rips\\My Album\\disc.wav" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n')
    assert sheet.files == ["C:\\Rips\\My Album\\disc.wav"]
    assert sheet.tracks_in("disc.wav") == sheet.tracks


def test_data_track_is_not_audio():
    sheet = parse('FILE "x.bin" BINARY\nTRACK 01 MODE1/2352\nINDEX 01 00:00:00\n'
                  'TRACK 02 AUDIO\nINDEX 01 10:00:00\n')
    assert not sheet.tracks[0].audio and sheet.tracks[1].audio


def test_encodings(tmp_path):
    text = 'TITLE "Café Ångström"\nFILE "a.wav" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n'
    for name, data in (("utf8.cue", text.encode("utf-8")), ("bom.cue", "\ufeff".encode() + text.encode()),
                       ("ansi.cue", text.encode("cp1252")), ("utf16.cue", text.encode("utf-16"))):
        (tmp_path / name).write_bytes(data)
        sheet = cuesheet.read(tmp_path / name)
        assert sheet.title == "Café Ångström", name
        assert sheet.source == tmp_path / name
    assert [p.name for p in cuesheet.cue_files(tmp_path)] == ["ansi.cue", "bom.cue", "utf16.cue", "utf8.cue"]


# -- the FLAC CUESHEET block -----------------------------------------------------------------
def test_flac_embedded_cuesheet(tmp_path):
    path = tmp_path / "image.flac"
    sf.write(path, np.zeros((44100 * 3, 2)), 44100, format="FLAC", subtype="PCM_16")
    block = FlacCueSheet()
    block.media_catalog_number, block.lead_in_samples, block.compact_disc = "0077774600125", 88200, True
    first = CueSheetTrack(1, 0, isrc="USABC8500001")
    first.indexes.append(CueSheetTrackIndex(1, 0))
    second = CueSheetTrack(2, 588 * 75, pre_emphasis=True)       # starts 1 s in (INDEX 01)…
    second.indexes += [CueSheetTrackIndex(0, 0), CueSheetTrackIndex(1, 588 * 10)]  # …gap 10 frames
    block.tracks += [first, second, CueSheetTrack(170, 44100 * 3)]
    tags = FLACFile(str(path))
    tags.cuesheet = block
    tags.save()

    sheet = cuesheet.from_flac(path)
    assert sheet.embedded and sheet.catalog == "0077774600125" and sheet.lead_out == 44100 * 3
    one, two = sheet.tracks
    assert one.isrc == "USABC8500001" and not one.pre_emphasis
    assert two.pre_emphasis and two.samples == {0: 44100, 1: 44100 + 5880}
    assert two.indexes == {0: 75, 1: 85}                           # also in CD frames
    assert sheet.end_sample(one, 44100) == 44100 + 5880
    assert sheet.end_sample(two, 44100) == 44100 * 3               # the lead-out
    assert sheet.tracks_in("image.flac") == [one, two]


def test_flac_without_cuesheet(tmp_path):
    path = tmp_path / "plain.flac"
    sf.write(path, np.zeros((100, 2)), 44100, format="FLAC", subtype="PCM_16")
    assert cuesheet.from_flac(path) is None
    assert cuesheet.from_flac(tmp_path / "missing.flac") is None
