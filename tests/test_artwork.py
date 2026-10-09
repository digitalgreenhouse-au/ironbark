import numpy as np
import soundfile as sf
from PIL import Image

from ironbark.artwork import cover_for, folder_art
from ironbark.tagio.flac import FLACFile
from ironbark.tagio.vorbis import Picture


def image(path, size=(40, 40), colour=(200, 50, 50)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


def flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros((441, 2), "float32"), 44100, format="FLAC")
    return path


def test_well_known_names_win(tmp_path):
    for name in ("AlbumArtSmall.jpg", "Folder.jpg", "cover.jpg", "random.jpg"):
        image(tmp_path / name)
    assert folder_art(tmp_path).name == "cover.jpg"


def test_obvious_non_covers_are_skipped(tmp_path):
    image(tmp_path / "Album [Spectro].png", size=(400, 400))
    image(tmp_path / "Album - Back.jpg", size=(400, 400))
    image(tmp_path / "Album.jpg", size=(100, 100))
    assert folder_art(tmp_path).name == "Album.jpg"


def test_scene_style_cd_names_still_count(tmp_path):
    image(tmp_path / "00-artist-album-cd-flac-2019.jpg")
    assert folder_art(tmp_path).name == "00-artist-album-cd-flac-2019.jpg"


def test_disc_scan_loses_to_a_cover(tmp_path):
    image(tmp_path / "Album - CD (1-2).jpg", size=(600, 600))
    image(tmp_path / "Album - Front.jpg", size=(300, 300))
    assert folder_art(tmp_path).name == "Album - Front.jpg"


def test_embedded_front_cover_beats_folder_image(tmp_path):
    track = flac(tmp_path / "01.flac")
    image(tmp_path / "cover.jpg", colour=(0, 0, 255))
    pic = Picture()
    pic.type, pic.mime = 3, "image/png"
    buf = tmp_path / "embedded.png"
    image(buf, colour=(0, 255, 0))
    pic.data = buf.read_bytes()
    audio = FLACFile(track)
    audio.add_picture(pic)
    audio.save()
    buf.unlink()
    thumb = cover_for(track, 24)
    assert thumb.size == (24, 24) and thumb.getpixel((12, 12)) == (0, 255, 0)


def test_disc_folder_uses_album_folder_cover(tmp_path):
    track = flac(tmp_path / "Album" / "CD1" / "01.flac")
    image(tmp_path / "Album" / "cover.jpg")
    assert cover_for(track, 16) is not None


def test_no_art_gives_none(tmp_path):
    assert cover_for(flac(tmp_path / "01.flac"), 16) is None
