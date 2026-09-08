"""Unit tests for services/scrobble_log.py - db/scrobbles.json ledger.

All I/O is sandboxed: the module's ``scrobbles_json`` constant is
re-pointed at a fresh tmp_path per test.
"""

import pytest

from services import scrobble_log as sl


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(sl, "scrobbles_json", tmp_path / "db" / "scrobbles.json")
    yield sl


class TestRecordAndLookup:
    def test_roundtrip(self, ledger):
        ledger.record_scrobble("spotify", "pl1", 42, 1717000000)
        assert ledger.lookup_scrobble("spotify", "pl1", 42) == 1717000000

    def test_no_song_id_is_noop(self, ledger):
        ledger.record_scrobble("spotify", "pl1", None, 1717000000)
        assert ledger.lookup_scrobble("spotify", "pl1", None) is None

    def test_missing_entry_returns_none(self, ledger):
        assert ledger.lookup_scrobble("spotify", "nope", 1) is None

    def test_scoped_per_playlist(self, ledger):
        ledger.record_scrobble("spotify", "pl1", 1, 100)
        assert ledger.lookup_scrobble("spotify", "pl2", 1) is None

    def test_scoped_per_platform(self, ledger):
        ledger.record_scrobble("spotify", "pl1", 1, 100)
        assert ledger.lookup_scrobble("youtube_music", "pl1", 1) is None


class TestClear:
    def test_clear_scrobble(self, ledger):
        ledger.record_scrobble("spotify", "pl1", 42, 100)
        ledger.clear_scrobble("spotify", "pl1", 42)
        assert ledger.lookup_scrobble("spotify", "pl1", 42) is None

    def test_clear_missing_is_silent(self, ledger):
        ledger.clear_scrobble("spotify", "pl1", 999)  # must not raise


class TestBulkRemoval:
    def test_remove_playlist_entries(self, ledger):
        ledger.record_scrobble("spotify", "pl1", 1, 100)
        ledger.record_scrobble("spotify", "pl1", 2, 200)
        ledger.record_scrobble("spotify", "pl2", 3, 300)
        ledger.remove_playlist_entries("spotify", "pl1")
        assert ledger.lookup_scrobble("spotify", "pl1", 1) is None
        assert ledger.lookup_scrobble("spotify", "pl2", 3) == 300

    def test_remove_platform_entries(self, ledger):
        ledger.record_scrobble("spotify", "pl1", 1, 100)
        ledger.record_scrobble("youtube_music", "pl1", 2, 200)
        ledger.remove_platform_entries("spotify")
        assert ledger.lookup_scrobble("spotify", "pl1", 1) is None
        assert ledger.lookup_scrobble("youtube_music", "pl1", 2) == 200