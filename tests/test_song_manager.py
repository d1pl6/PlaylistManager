"""Unit tests for services/song_manager.py - SQLite song CRUD.

SongManager is a thread-safe singleton whose ``db_manager`` is created in
``__new__``; the singleton and the class-level connection registry are
reset per test (sandboxed db directory via ``DatabaseManager.
_get_db_directory`` monkeypatch).

Pure functions (``_parse_duration``, the platform extractors,
``_pick_thumbnail``) are tested separately with no file I/O.
"""

import time

import pytest

from services import song_manager
from services.database import DatabaseManager


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    db_dir = tmp_path / "db"
    db_dir.mkdir()

    def _fake_db_dir(platform: str):
        d = db_dir / platform
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(DatabaseManager, "_get_db_directory", staticmethod(_fake_db_dir))
    monkeypatch.setattr(DatabaseManager, "_connections", {})
    monkeypatch.setattr(song_manager.SongManager, "_instance", None)
    yield db_dir


@pytest.fixture
def sm(sandbox):
    # Force a fresh singleton bound to the sandboxed db dir.
    song_manager.SongManager._instance = None
    return song_manager.SongManager()


class TestParseDuration:
    def test_mm_ss(self):
        assert song_manager._parse_duration("3:45") == 225

    def test_h_mm_ss(self):
        assert song_manager._parse_duration("1:02:30") == 3750

    def test_seconds_only(self):
        assert song_manager._parse_duration("59") == 59

    def test_with_spaces(self):
        assert song_manager._parse_duration(" 3:45 ") == 225

    def test_empty(self):
        assert song_manager._parse_duration("") == 0
        assert song_manager._parse_duration("   ") == 0

    def test_non_numeric(self):
        assert song_manager._parse_duration("abc") == 0

    def test_negative_component(self):
        assert song_manager._parse_duration("1:-30") == 0

    def test_too_many_parts(self):
        assert song_manager._parse_duration("1:2:3:4") == 0


class TestExtractors:
    def test_youtube_track(self):
        out = song_manager._extract_youtube_track({
            "videoId": "dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "artists": [{"name": "Rick Astley"}],
            "duration": "3:32",
            "thumbnails": [{"url": "small.jpg", "width": 60},
                           {"url": "big.jpg", "width": 320}],
        })
        assert out[0] == "Never Gonna Give You Up"
        assert out[1] == ["Rick Astley"]
        assert out[2] == 212
        assert out[3] == "dQw4w9WgXcQ"
        assert out[4] == "big.jpg"    # smallest >= 64px

    def test_youtube_no_video_id_returns_none(self):
        assert song_manager._extract_youtube_track({"title": "No ID"}) is None

    def test_youtube_missing_artists(self):
        out = song_manager._extract_youtube_track({"videoId": "x", "title": "T"})
        assert out[1] == ["Unknown Artist"]

    def test_spotify_track(self):
        out = song_manager._extract_spotify_track({
            "id": "4uLU6hMCjMI75M1A2tKUQC",
            "type": "track",
            "name": "Song X",
            "artists": [{"name": "Artist Y"}],
            "duration_ms": 213000,
            "album": {"images": [{"url": "large.jpg"}, {"url": "medium.jpg"}]},
        })
        assert out[0] == "Song X"
        assert out[2] == 213
        assert out[4] == "medium.jpg"

    def test_spotify_episode_excluded(self):
        assert song_manager._extract_spotify_track(
            {"id": "abc", "type": "episode", "name": "Pod"}
        ) is None

    def test_spotify_no_id_returns_none(self):
        assert song_manager._extract_spotify_track({"name": "No ID"}) is None

    def test_soundcloud_track_urn(self):
        out = song_manager._extract_soundcloud_track({
            "urn": "soundcloud:tracks:123",
            "id": 123,
            "title": "SC Track",
            "user": {"username": "SC User"},
            "duration": 200000,
            "artwork_url": "art.jpg",
        })
        assert out[3] == "soundcloud:tracks:123"
        assert out[2] == 200
        assert out[4] == "art.jpg"

    def test_soundcloud_no_urn_falls_back_to_id(self):
        out = song_manager._extract_soundcloud_track({"id": 456, "title": "T"})
        assert out[3] == "soundcloud:tracks:456"

    def test_deezer_track(self):
        out = song_manager._extract_deezer_track({
            "id": 789,
            "title": "DZ Track",
            "contributors": {"edges": [{"node": {"name": "DZ Artist"}}]},
            "duration": 180,
            "album": {"cover": {"urls": ["/0/500x500-1.jpg"]}},
        })
        assert out[0] == "DZ Track"
        assert out[1] == ["DZ Artist"]
        assert out[2] == 180
        assert out[3] == "789"
        assert out[4] == "/0/500x500-1.jpg"

    def test_unknown_platform_falls_back_to_youtube(self):
        # add_songs_bulk with an unknown platform warns and uses the
        # YouTube extractor (track dicts without videoId are skipped).
        sm = song_manager.SongManager()
        n = sm.add_songs_bulk("X", [{"title": "T", "videoId": "v1"}],
                              platform="unknown_platform", playlist_id="")
        assert n == 1


class TestPickThumbnail:
    def test_prefers_smallest_over_64px(self):
        thumbs = [{"url": "60.jpg", "width": 60, "height": 34},
                  {"url": "320.jpg", "width": 320, "height": 180},
                  {"url": "128.jpg", "width": 128, "height": 72}]
        assert song_manager._pick_thumbnail(thumbs) == "128.jpg"

    def test_falls_back_to_smallest(self):
        thumbs = [{"url": "30.jpg", "width": 30, "height": 30},
                  {"url": "50.jpg", "width": 50, "height": 50}]
        assert song_manager._pick_thumbnail(thumbs) == "30.jpg"

    def test_empty(self):
        assert song_manager._pick_thumbnail([]) is None


class TestSongCrud:
    def test_add_song_then_exists(self, sm):
        song_id = sm.add_song("Playlist A", "Song One", ["Artist"], 200,
                              "track1", platform="spotify", playlist_id="pl1")
        assert sm.song_exists("Playlist A", "track1", platform="spotify",
                              playlist_id="pl1") is True
        assert song_id > 0

    def test_add_duplicate_returns_same_id(self, sm):
        id1 = sm.add_song("Playlist A", "Song One", ["Artist"], 200,
                          "track1", platform="spotify", playlist_id="pl1")
        id2 = sm.add_song("Playlist A", "Song One", ["Artist"], 200,
                          "track1", platform="spotify", playlist_id="pl1")
        assert id1 == id2

    def test_add_song_by_info_dedupes(self, sm):
        id1 = sm.add_song_by_info("Playlist A", "Song One", ["Artist"], 200,
                                  "track1", platform="spotify", playlist_id="pl1")
        id2 = sm.add_song_by_info("Playlist A", "Song One", ["Artist"], 200,
                                  "trackX", platform="spotify", playlist_id="pl1")
        assert id1 == id2

    def test_song_exists_false(self, sm):
        assert sm.song_exists("Playlist A", "missing", platform="spotify",
                              playlist_id="pl1") is False

    def test_get_song_by_track_id(self, sm):
        sm.add_song("Playlist A", "Song One", ["Artist"], 200,
                    "track1", platform="spotify", playlist_id="pl1")
        song = sm.get_song_by_track_id("Playlist A", "track1", platform="spotify",
                                       playlist_id="pl1")
        assert song["title"] == "Song One"
        assert song["artists"] == ["Artist"]

    def test_delete_song(self, sm):
        song_id = sm.add_song("Playlist A", "Song One", ["Artist"], 200,
                              "track1", platform="spotify", playlist_id="pl1")
        assert sm.delete_song("Playlist A", song_id, platform="spotify",
                              playlist_id="pl1") is True
        assert sm.song_exists("Playlist A", "track1", platform="spotify",
                              playlist_id="pl1") is False

    def test_delete_nonexistent_song_returns_false(self, sm):
        assert sm.delete_song("Playlist A", 99999, platform="spotify",
                              playlist_id="pl1") is False

    def test_get_all_songs_newest_first(self, sm):
        sm.add_song("Playlist A", "Song One", ["Artist"], 180,
                    "track1", platform="spotify", playlist_id="pl1")
        time.sleep(1.1)  # added_at has second precision; distinct timestamps
        sm.add_song("Playlist A", "Song Two", ["Artist"], 180,
                    "track2", platform="spotify", playlist_id="pl1")
        songs = sm.get_all_songs("Playlist A", platform="spotify", playlist_id="pl1")
        assert [s["title"] for s in songs] == ["Song Two", "Song One"]

    def test_get_latest_song(self, sm):
        sm.add_song("Playlist A", "Song One", ["Artist"], 200,
                    "track1", platform="spotify", playlist_id="pl1")
        sm.add_song("Playlist A", "Song Two", ["Artist"], 220,
                    "track2", platform="spotify", playlist_id="pl1")
        latest = sm.get_latest_song("Playlist A", platform="spotify",
                                    playlist_id="pl1")
        assert latest["title"] == "Song Two"

    def test_get_latest_songs_limit(self, sm):
        for i in range(5):
            sm.add_song("Playlist A", f"Song {i}", ["Artist"], 200,
                        f"track{i}", platform="spotify", playlist_id="pl1")
        latest = sm.get_latest_songs("Playlist A", 3, platform="spotify",
                                     playlist_id="pl1")
        assert len(latest) == 3
        assert latest[0]["title"] == "Song 4"   # newest first

    def test_song_count(self, sm):
        sm.add_song("Playlist A", "Song One", ["Artist"], 200,
                    "track1", platform="spotify", playlist_id="pl1")
        sm.add_song("Playlist A", "Song Two", ["Artist"], 220,
                    "track2", platform="spotify", playlist_id="pl1")
        assert sm.get_song_count("Playlist A", platform="spotify",
                                 playlist_id="pl1") == 2

    def test_total_duration(self, sm):
        sm.add_song("Playlist A", "Song One", ["Artist"], 200,
                    "track1", platform="spotify", playlist_id="pl1")
        sm.add_song("Playlist A", "Song Two", ["Artist"], 220,
                    "track2", platform="spotify", playlist_id="pl1")
        assert sm.get_total_duration("Playlist A", platform="spotify",
                                     playlist_id="pl1") == 420

    def test_search_songs(self, sm):
        sm.add_song("Playlist A", "Bohemian Rhapsody", ["Queen"], 180,
                    "track1", platform="spotify", playlist_id="pl1")
        sm.add_song("Playlist A", "Another Brick", ["Pink Floyd"], 200,
                    "track2", platform="spotify", playlist_id="pl1")
        hits = sm.search_songs("Playlist A", "bohemian", platform="spotify",
                               playlist_id="pl1")
        assert len(hits) == 1
        assert hits[0]["title"] == "Bohemian Rhapsody"

    def test_search_escapes_wildcards(self, sm):
        # "%" must be treated literally, not as a LIKE wildcard: it
        # matches only titles CONTAINING a literal %, never everything.
        sm.add_song("Playlist A", "100% Real", ["Artist"], 180,
                    "track1", platform="spotify", playlist_id="pl1")
        sm.add_song("Playlist A", "Anything Goes", ["Artist"], 180,
                    "track2", platform="spotify", playlist_id="pl1")
        hits = sm.search_songs("Playlist A", "%", platform="spotify",
                               playlist_id="pl1")
        assert len(hits) == 1
        assert hits[0]["title"] == "100% Real"

    def test_empty_query_returns_empty(self, sm):
        assert sm.search_songs("Playlist A", "   ", platform="spotify",
                               playlist_id="pl1") == []

    def test_add_songs_bulk(self, sm):
        tracks = [
            {"videoId": "v1", "title": "T1", "artists": [{"name": "A1"}],
             "duration": "3:00", "thumbnails": []},
            {"videoId": "v2", "title": "T2", "artists": [{"name": "A2"}],
             "duration": "4:00", "thumbnails": []},
            {"no_video_id": True},   # skipped
        ]
        n = sm.add_songs_bulk("Playlist YT", tracks, platform="youtube_music",
                              playlist_id="pl_yt")
        assert n == 2
        assert sm.get_song_count("Playlist YT", platform="youtube_music",
                                 playlist_id="pl_yt") == 2