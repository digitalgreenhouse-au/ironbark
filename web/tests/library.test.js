// The web player's grouping of picked files into albums.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Lib = require("../js/library.js");

const f = (path) => ({ file: new File([new Uint8Array(1)], path.split("/").pop()), path });

test("songs and cover pictures are told apart; the best cover per folder wins", () => {
  const { songs, covers } = Lib.splitEntries([
    f("Music/A/01.flac"), f("Music/A/back.jpg"), f("Music/A/Cover.JPG"), f("Music/A/folder.png"),
    f("Music/B/CD1/01.flac"), f("Music/B/folder.jpg"), f("Music/C/scan.jpg"), f("Music/C/1.mp3"), f("notes.txt"),
  ]);
  assert.equal(songs.length, 3);
  assert.equal(covers.get("Music/A").name, "Cover.JPG");
  assert.equal(covers.get("Music/B/CD1").name, "folder.jpg");   // a disc folder uses its album's cover
  assert.equal(covers.has("Music/C"), false);                     // "scan.jpg" isn't a cover name
});

test("albums: by tag within a folder, sorted by disc, track, then name", () => {
  const { songs, covers } = Lib.splitEntries([
    f("M/X/b.flac"), f("M/X/a.flac"), f("M/X/c.flac"), f("M/Y/CD2/01.flac"), f("M/Y/CD1/01.flac"), f("M/Y/CD1/02.flac"), f("M/Z/loose 10.mp3"), f("M/Z/loose 9.mp3"),
  ]);
  const tag = (path, t) => { songs.find((s) => s.path === path).tags = t; };
  tag("M/X/a.flac", { album: "Ex", artist: "Ann", track: 2, bitsPerSample: 16, sampleRate: 44100 });
  tag("M/X/b.flac", { album: "Ex", artist: "Ann", track: 1, bitsPerSample: 16, sampleRate: 44100 });
  tag("M/X/c.flac", { album: "Ex", artist: "Bob", track: 3, bitsPerSample: 16, sampleRate: 44100 });
  tag("M/Y/CD1/01.flac", { album: "Why", albumArtist: "Cee", disc: 1, track: 1, bitsPerSample: 24, sampleRate: 96000 });
  tag("M/Y/CD1/02.flac", { album: "Why", albumArtist: "Cee", disc: 1, track: 2, bitsPerSample: 24, sampleRate: 96000 });
  tag("M/Y/CD2/01.flac", { album: "Why", albumArtist: "Cee", disc: 2, track: 1, bitsPerSample: 24, sampleRate: 88200 });
  const albums = Lib.buildAlbums(songs, covers);
  assert.deepEqual(albums.map((a) => a.title), ["Ex", "Why", "Z"]);
  assert.deepEqual(albums[0].songs.map((s) => s.name), ["b.flac", "a.flac", "c.flac"]);
  assert.equal(albums[0].artist, "Various artists");
  assert.equal(albums[0].format, "16/44.1");
  assert.deepEqual(albums[1].songs.map((s) => s.path), ["M/Y/CD1/01.flac", "M/Y/CD1/02.flac", "M/Y/CD2/01.flac"]);
  assert.equal(albums[1].artist, "Cee");
  assert.equal(albums[1].format, "mixed");
  assert.deepEqual(albums[2].songs.map((s) => s.name), ["loose 9.mp3", "loose 10.mp3"]);   // numbers in names sort as numbers
});

test("an untagged song joins the one album in its folder; with two albums it stays apart", () => {
  const { songs, covers } = Lib.splitEntries([f("M/A/1.flac"), f("M/A/2.wav"), f("M/B/1.flac"), f("M/B/2.flac"), f("M/B/3.wav")]);
  const tag = (path, t) => { songs.find((s) => s.path === path).tags = t; };
  tag("M/A/1.flac", { album: "Aye", track: 1 });
  tag("M/A/2.wav", { title: "Wave" });
  tag("M/B/1.flac", { album: "Bee" });
  tag("M/B/2.flac", { album: "Sea" });
  tag("M/B/3.wav", {});
  const albums = Lib.buildAlbums(songs, covers);
  assert.deepEqual(albums.map((a) => [a.title, a.songs.length]), [["Aye", 2], ["B", 1], ["Bee", 1], ["Sea", 1]]);
});

test("sample rates read the way the desktop player writes them", () => {
  assert.equal(Lib.formatRate(44100), "44.1");
  assert.equal(Lib.formatRate(96000), "96");
  assert.equal(Lib.formatRate(352800), "352.8");
});

test("discs in CD1 / CD2 folders play disc by disc when the files have no disc tag", () => {
  const { songs, covers } = Lib.splitEntries([f("M/A/CD2/01.wav"), f("M/A/CD1/02.wav"), f("M/A/CD1/01.wav"), f("M/A/CD2/02.flac")]);
  songs.find((s) => s.path === "M/A/CD2/02.flac").tags = { track: 2 };
  const albums = Lib.buildAlbums(songs, covers);
  assert.equal(albums.length, 1);
  assert.deepEqual(albums[0].songs.map((s) => s.path), ["M/A/CD1/01.wav", "M/A/CD1/02.wav", "M/A/CD2/01.wav", "M/A/CD2/02.flac"]);
});

test("a number at the start of a file name stands in for a missing track tag", () => {
  assert.equal(Lib.leadNumber("03 Song.wav"), 3);
  assert.equal(Lib.leadNumber("12-Intro.flac"), 12);
  assert.equal(Lib.leadNumber("1999 Live.flac"), null);     // four digits: a year, not a track
  assert.equal(Lib.leadNumber("Song 03.wav"), null);
  assert.equal(Lib.leadNumber(""), null);
});
