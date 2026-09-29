"""Unit tests for services/scrobble_log.py - db/scrobbles.json ledger.

All I/O is sandboxed: the module's ``scrobbles_json`` constant is
re-pointed at a fresh tmp_path per test.
"""

import json

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


class TestBackendField:
    """The ledger records WHICH service(s) accepted the scrobble.

    Every backend switched on gets the same listen, so the record is a LIST
    and the remove path has to delete each of them.  With more than one
    scrobble-capable plugin installed, the user can also switch services
    between the add and the remove; resolving the target at delete time
    would then address a service that never got the listen.
    """

    def test_every_backend_is_recorded_and_returned(self, ledger):
        ledger.record_scrobble(
            "spotify", "pl1", 42, 1717000000, ["lastfm", "listenbrainz"]
        )
        rec = ledger.lookup_scrobble_record("spotify", "pl1", 42)
        assert rec["backends"] == ["lastfm", "listenbrainz"]
        assert rec["timestamp"] == 1717000000

    def test_a_single_backend_is_stored_as_a_one_element_list(self, ledger):
        # Both call shapes are common enough (one backend installed) that
        # neither should be the one that breaks.
        ledger.record_scrobble("spotify", "pl1", 42, 1717000000, "lastfm")
        assert ledger.lookup_scrobble_record("spotify", "pl1", 42)["backends"] == [
            "lastfm"
        ]
        ledger.record_scrobble("spotify", "pl1", 43, 1717000000, ["listenbrainz"])
        assert ledger.lookup_scrobble_record("spotify", "pl1", 43)["backends"] == [
            "listenbrainz"
        ]

    def test_backends_default_to_empty(self, ledger):
        ledger.record_scrobble("spotify", "pl1", 42, 1717000000)
        assert ledger.lookup_scrobble_record("spotify", "pl1", 42)["backends"] == []

    def test_blank_ids_are_dropped(self, ledger):
        # An integration with no id must not become an unnameable ledger
        # entry the remove path can never resolve.
        ledger.record_scrobble("spotify", "pl1", 42, 1, ["lastfm", "", "   "])
        assert ledger.lookup_scrobble_record("spotify", "pl1", 42)["backends"] == [
            "lastfm"
        ]

    def test_a_v1_record_without_the_field_reads_as_unknown(self, ledger):
        # Written before the field existed: "which service" is genuinely
        # unknown, and the remove path reports that rather than guessing
        # (a wrong guess could erase an unrelated listen).
        ledger.scrobbles_json.parent.mkdir(parents=True, exist_ok=True)
        ledger.scrobbles_json.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scrobbles": {
                        "spotify": {
                            "pl1": {"42": {"timestamp": 1717000000, "at": "2024-01-01"}}
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        rec = ledger.lookup_scrobble_record("spotify", "pl1", 42)
        assert rec["timestamp"] == 1717000000
        assert rec["backends"] == []

    def test_a_v2_record_reads_its_single_backend(self, ledger):
        # Migration: v2 stored one id in "backend".  Losing it would strand
        # every existing record as "unknown" and orphan its listen.
        ledger.scrobbles_json.parent.mkdir(parents=True, exist_ok=True)
        ledger.scrobbles_json.write_text(
            json.dumps(
                {
                    "version": 2,
                    "scrobbles": {
                        "spotify": {
                            "pl1": {
                                "42": {
                                    "timestamp": 1717000000,
                                    "at": "2024-01-01",
                                    "backend": "lastfm",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        rec = ledger.lookup_scrobble_record("spotify", "pl1", 42)
        assert rec["backends"] == ["lastfm"]

    def test_a_misshapen_backends_field_reads_as_unknown(self, ledger):
        # A hand-edited / half-written file must never crash the ledger.
        ledger.scrobbles_json.parent.mkdir(parents=True, exist_ok=True)
        ledger.scrobbles_json.write_text(
            json.dumps(
                {
                    "version": 3,
                    "scrobbles": {
                        "spotify": {
                            "pl1": {
                                "42": {
                                    "timestamp": 1717000000,
                                    "at": "2024-01-01",
                                    "backends": {"lastfm": True},
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        assert ledger.lookup_scrobble_record("spotify", "pl1", 42)["backends"] == []

    def test_the_stored_schema_is_the_current_one(self, ledger):
        ledger.record_scrobble("spotify", "pl1", 42, 1, ["lastfm"])
        stored = json.loads(ledger.scrobbles_json.read_text(encoding="utf-8"))
        assert stored["version"] == 3
        assert stored["scrobbles"]["spotify"]["pl1"]["42"]["backends"] == ["lastfm"]

    def test_lookup_scrobble_still_returns_the_timestamp(self, ledger):
        ledger.record_scrobble("spotify", "pl1", 42, 1717000000, ["lastfm"])
        assert ledger.lookup_scrobble("spotify", "pl1", 42) == 1717000000

    def test_a_miss_is_none(self, ledger):
        assert ledger.lookup_scrobble_record("spotify", "pl1", 1) is None
        assert ledger.lookup_scrobble_record("spotify", "pl1", None) is None


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