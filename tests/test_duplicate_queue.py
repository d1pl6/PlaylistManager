"""Unit tests for services/duplicate_queue.py - db/extra.json state.

All I/O is sandboxed: the module's ``extra_json`` constant is re-pointed
at a fresh tmp_path per test.  The default prune-on-read path is
exercised with the playlist registry also sandboxed (via
playlist_store.playlists_json monkeypatch).
"""

import pytest

from services import duplicate_queue as dq
from services import playlist_store as ps


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(dq, "extra_json", tmp_path / "db" / "extra.json")
    monkeypatch.setattr(ps, "playlists_json", tmp_path / "db" / "playlists.json")
    monkeypatch.setattr(ps, "_playlist_cache", None)
    yield dq


class TestMakePairKey:
    def test_order_stable(self):
        k1 = dq.make_pair_key("spotify", "pl1", "trackB", "trackA")
        k2 = dq.make_pair_key("spotify", "pl1", "trackA", "trackB")
        assert k1 == k2

    def test_empty_tracks_allowed(self):
        assert dq.make_pair_key("yt", "", "", "abc") == "yt|||abc"


class TestPending:
    def test_add_then_find(self, sandbox):
        rid = dq.add_pending({"playlist_id": "pl1", "track_id": "t1",
                              "playlist_name": "Chill", "platform": "spotify"})
        rec = dq.find_pending("pl1", "t1")
        assert rec is not None
        assert rec["id"] == rid

    def test_re_add_replaces(self, sandbox):
        dq.add_pending({"playlist_id": "pl1", "track_id": "t1"})
        rid2 = dq.add_pending({"playlist_id": "pl1", "track_id": "t1"})
        pend = dq.list_pending(prune_unregistered=False)
        assert len(pend) == 1
        assert pend[0]["id"] == rid2

    def test_remove_pending(self, sandbox):
        rid = dq.add_pending({"playlist_id": "pl1", "track_id": "t1"})
        assert dq.remove_pending(rid) is True
        assert dq.find_pending("pl1", "t1") is None

    def test_dedupe_same_playlist_track(self, sandbox):
        dq.add_pending({"playlist_id": "pl1", "track_id": "t1"})
        dq.add_pending({"playlist_id": "pl1", "track_id": "t1"})
        dq.add_pending({"playlist_id": "pl1", "track_id": "t2"})
        assert len(dq.list_pending(prune_unregistered=False)) == 2

    def test_prune_unregistered_playlist(self, sandbox):
        ps.PlaylistStore.add_playlist("Registered", "spotify", "pl1")
        dq.add_pending({"playlist_id": "pl1", "track_id": "t1",
                        "playlist_name": "Registered", "platform": "spotify"})
        dq.add_pending({"playlist_id": "gone", "track_id": "t2",
                        "playlist_name": "Deleted", "platform": "spotify"})
        remaining = dq.list_pending()  # prune_unregistered=True default
        assert len(remaining) == 1
        assert remaining[0]["playlist_id"] == "pl1"


class TestSongMemory:
    def test_set_get_roundtrip(self, sandbox):
        key = dq.make_pair_key("spotify", "pl1", "a", "b")
        dq.set_song(key, "added")
        rec = dq.get_song(key)
        assert rec["song"] == "added"

    def test_invalid_value_raises(self, sandbox):
        with pytest.raises(ValueError):
            dq.set_song("k", "bogus")

    def test_delete_song(self, sandbox):
        key = dq.make_pair_key("spotify", "pl1", "a", "b")
        dq.set_song(key, "dismissed")
        assert dq.delete_song(key) is True
        assert dq.get_song(key) is None

    def test_list_songs(self, sandbox):
        dq.set_song(dq.make_pair_key("s", "p", "x", "y"), "added")
        assert len(dq.list_songs()) == 1


class TestErrors:
    def test_record_and_list_newest_first(self, sandbox):
        dq.record_error("Chill", "spotify", "boom 1")
        dq.record_error("Chill", "spotify", "boom 2")
        errors = dq.list_errors()
        assert [e["message"] for e in errors] == ["boom 2", "boom 1"]

    def test_clear_errors(self, sandbox):
        dq.record_error("Chill", "spotify", "boom")
        assert dq.clear_errors() == 1
        assert dq.list_errors() == []


class TestPurge:
    def test_purge_platform(self, sandbox):
        dq.add_pending({"playlist_id": "p1", "track_id": "t1", "platform": "spotify"})
        dq.add_pending({"playlist_id": "p2", "track_id": "t2", "platform": "yt"})
        dq.set_song(dq.make_pair_key("spotify", "p1", "a", "b"), "added")
        dq.record_error("X", "spotify", "e")
        counts = dq.purge_platform("spotify")
        assert counts == (1, 1, 1)
        assert dq.find_pending("p2", "t2") is not None
        assert dq.list_songs() == {}

    def test_purge_playlist(self, sandbox):
        dq.add_pending({"playlist_id": "p1", "track_id": "t1",
                        "platform": "spotify", "playlist_name": "Chill"})
        dq.add_pending({"playlist_id": "p2", "track_id": "t2",
                        "platform": "spotify", "playlist_name": "Other"})
        dq.set_song(dq.make_pair_key("spotify", "p1", "a", "b"), "added")
        counts = dq.purge_playlist("spotify", "p1", "Chill")
        assert counts[0] == 1  # pending removed
        assert counts[1] == 1  # song memory removed
        assert dq.find_pending("p2", "t2") is not None
        assert dq.list_songs() == {}


class TestStampAndCount:
    def test_stamp_zero_when_missing(self, sandbox):
        assert dq.stamp() == 0.0

    def test_stamp_changes_after_write(self, sandbox):
        s1 = dq.stamp()
        dq.add_pending({"playlist_id": "p", "track_id": "t"})
        s2 = dq.stamp()
        assert s2 != s1

    def test_activity_count(self, sandbox):
        dq.add_pending({"playlist_id": "p1", "track_id": "t1"})
        dq.record_error("Chill", "spotify", "e")
        assert dq.activity_count() == 2