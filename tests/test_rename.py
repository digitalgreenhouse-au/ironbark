"""0.26.0: DG FLAC Player was renamed Ironbark — the old settings folder is carried over once."""

import json

from ironbark import APP_NAME, OLD_APP_NAME, settings


def test_old_folder_is_copied_once_and_paths_inside_follow(tmp_path):
    old = tmp_path / OLD_APP_NAME
    (old / "Playlists").mkdir(parents=True)
    (old / "Playlists" / "Rock.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (old / "library_index.json").write_text("{}", encoding="utf-8")
    prefs = {"volume": 0.8, "tabs": [{"kind": "playlist", "path": str(old / "Playlists" / "Rock.m3u8")}],
             "library_folder": r"C:\Music"}
    (old / "settings.json").write_text(json.dumps(prefs), encoding="utf-8")

    assert settings.carry_over_old_folder(tmp_path) is True
    new = tmp_path / APP_NAME
    assert (new / "Playlists" / "Rock.m3u8").exists() and (new / "library_index.json").exists()
    data = json.loads((new / "settings.json").read_text(encoding="utf-8"))
    assert data["volume"] == 0.8 and data["library_folder"] == r"C:\Music"
    assert data["tabs"][0]["path"] == str(new / "Playlists" / "Rock.m3u8")
    assert json.loads((old / "settings.json").read_text(encoding="utf-8")) == prefs   # old left as a backup

    (new / "settings.json").write_text(json.dumps({"volume": 0.3}), encoding="utf-8")
    assert settings.carry_over_old_folder(tmp_path) is False                         # only the first time
    assert json.loads((new / "settings.json").read_text(encoding="utf-8")) == {"volume": 0.3}


def test_nothing_to_carry_over_on_a_fresh_install(tmp_path):
    assert settings.carry_over_old_folder(tmp_path) is False
    assert not (tmp_path / APP_NAME).exists()
