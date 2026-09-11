"""Unit tests for the grid sort + pin feature.

Covers ``services/playlist_sort.py`` (pure sorter) and the two new
registry methods it depends on: ``PlaylistStore.mark_used`` and
``PlaylistStore.set_pinned``.  The registry tests re-point
``playlists_json`` at a temp tree exactly like ``test_playlist_store.py``.
"""

import pytest

import services.playlist_store as ps
from services.playlist_sort import GRID_SORT_KEYS, sort_playlists


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "playlists_json", tmp_path / "db" / "playlists.json")
    monkeypatch.setattr(ps, "_playlist_cache", None)
    monkeypatch.setattr(ps, "_cache_timestamp", 0.0)
    yield ps


def _entry(name, platform="spotify", playlist_id=None, **extra):
    e = {"name": name, "platform": platform}
    if playlist_id is not None:
        e["playlist_id"] = playlist_id
    e.update(extra)
    return e


class TestSortName:
    def test_asc_case_insensitive(self):
        entries = [
            _entry("b", playlist_id="1"),
            _entry("A", playlist_id="2"),
            _entry("c", playlist_id="3"),
        ]
        out = sort_playlists(entries, "name", "asc")
        assert [e["name"] for e in out] == ["A", "b", "c"]

    def test_desc(self):
        entries = [
            _entry("a", playlist_id="1"),
            _entry("c", playlist_id="2"),
            _entry("b", playlist_id="3"),
        ]
        out = sort_playlists(entries, "name", "desc")
        assert [e["name"] for e in out] == ["c", "b", "a"]


class TestSortPlatform:
    def test_asc(self):
        entries = [
            _entry("x", "youtube_music"),
            _entry("y", "spotify"),
            _entry("z", "deezer"),
        ]
        out = sort_playlists(entries, "platform", "asc")
        assert [e["platform"] for e in out] == ["deezer", "spotify", "youtube_music"]

    def test_desc(self):
        entries = [
            _entry("x", "youtube_music"),
            _entry("y", "spotify"),
            _entry("z", "deezer"),
        ]
        out = sort_playlists(entries, "platform", "desc")
        assert [e["platform"] for e in out] == ["youtube_music", "spotify", "deezer"]


class TestSortAdded:
    def test_asc_oldest_first(self):
        # Input order IS the insertion order: x(first), z(second), y(third)
        entries = [
            _entry("x-first", playlist_id="1"),
            _entry("z-second", playlist_id="2"),
            _entry("y-third", playlist_id="3"),
        ]
        out = sort_playlists(entries, "added", "asc")
        assert [e["name"] for e in out] == ["x-first", "z-second", "y-third"]

    def test_desc_newest_first(self):
        entries = [
            _entry("x-first", playlist_id="1"),
            _entry("z-second", playlist_id="2"),
            _entry("y-third", playlist_id="3"),
        ]
        out = sort_playlists(entries, "added", "desc")
        assert [e["name"] for e in out] == ["y-third", "z-second", "x-first"]


class TestSortUsed:
    def test_desc_last_used_first(self):
        entries = [
            _entry("old", playlist_id="1", last_used_at=100),
            _entry("new", playlist_id="2", last_used_at=300),
            _entry("mid", playlist_id="3", last_used_at=200),
        ]
        out = sort_playlists(entries, "used", "desc")
        assert [e["name"] for e in out] == ["new", "mid", "old"]

    def test_asc_old_used_first(self):
        entries = [
            _entry("new", playlist_id="2", last_used_at=300),
            _entry("old", playlist_id="1", last_used_at=100),
            _entry("mid", playlist_id="3", last_used_at=200),
        ]
        out = sort_playlists(entries, "used", "asc")
        assert [e["name"] for e in out] == ["old", "mid", "new"]

    def test_never_used_sinks_to_end_in_both_directions(self):
        entries = [
            _entry("used-old", playlist_id="1", last_used_at=100),
            _entry("never-b", playlist_id="2"),
            _entry("used-new", playlist_id="3", last_used_at=300),
            _entry("never-a", playlist_id="4"),
        ]
        out_desc = sort_playlists(entries, "used", "desc")
        assert [e["name"] for e in out_desc] == [
            "used-new",
            "used-old",
            "never-b",
            "never-a",
        ]
        out_asc = sort_playlists(entries, "used", "asc")
        assert [e["name"] for e in out_asc] == [
            "used-old",
            "used-new",
            "never-b",
            "never-a",
        ]

    def test_never_used_is_stable(self):
        entries = [
            _entry("n2", playlist_id="2"),
            _entry("n1", playlist_id="1"),
        ]
        out = sort_playlists(entries, "used", "desc")
        assert [e["name"] for e in out] == ["n2", "n1"]


class TestPinned:
    """Pinned playlists always float to the top; the active key applies
    within each group, and "added" keeps the true insertion order."""

    def _mixed(self):
        return [
            _entry("z-unpinned", playlist_id="2"),
            _entry("a-pinned", playlist_id="1", pinned=True),
            _entry("m-pinned", playlist_id="3", pinned=True),
            _entry("b-unpinned", playlist_id="4"),
        ]

    def test_pins_first_by_name(self):
        out = sort_playlists(self._mixed(), "name", "asc")
        assert [e["name"] for e in out] == [
            "a-pinned",
            "m-pinned",
            "b-unpinned",
            "z-unpinned",
        ]

    def test_pins_first_in_desc_too(self):
        out = sort_playlists(self._mixed(), "name", "desc")
        assert [e["name"] for e in out] == [
            "m-pinned",
            "a-pinned",
            "z-unpinned",
            "b-unpinned",
        ]

    def test_pins_first_by_added(self):
        # insertion order: z(0), a(1), m(2), b(3)
        out = sort_playlists(self._mixed(), "added", "asc")
        assert [e["name"] for e in out] == [
            "a-pinned",
            "m-pinned",
            "z-unpinned",
            "b-unpinned",
        ]

    def test_pinned_without_other_fields_is_fine(self):
        entries = [
            _entry("plain", playlist_id="1"),
            {"pinned": True, "name": "pin-only"},
        ]
        out = sort_playlists(entries, "name", "asc")
        assert [e["name"] for e in out] == ["pin-only", "plain"]


class TestRobustness:
    def test_empty_list(self):
        assert sort_playlists([], "name", "asc") == []

    def test_missing_name_goes_first_in_asc(self):
        entries = [_entry("b", playlist_id="1"), {"platform": "spotify"}]
        out = sort_playlists(entries, "name", "asc")
        assert out[0]["platform"] == "spotify"
        assert out[1]["name"] == "b"

    def test_unknown_key_falls_back_to_name(self):
        entries = [_entry("b", playlist_id="1"), _entry("a", playlist_id="2")]
        out = sort_playlists(entries, "bogus", "asc")
        assert [e["name"] for e in out] == ["a", "b"]

    def test_unknown_direction_falls_back_to_asc(self):
        entries = [_entry("b", playlist_id="1"), _entry("a", playlist_id="2")]
        out = sort_playlists(entries, "name", "sideways")
        assert [e["name"] for e in out] == ["a", "b"]

    def test_stability_equal_keys_keep_input_order(self):
        entries = [_entry("same", playlist_id="1"), _entry("same", playlist_id="2")]
        out = sort_playlists(entries, "name", "asc")
        assert [e["playlist_id"] for e in out] == ["1", "2"]

    def test_keys_tuple(self):
        assert GRID_SORT_KEYS == ("name", "platform", "added", "used")


class TestMarkUsed:
    def test_stamps_unix_time(self, store):
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        assert store.PlaylistStore.mark_used("Chill Mix", "spotify", "pl1")
        entry = store.PlaylistStore.find_playlist("Chill Mix", "spotify", playlist_id="pl1")
        assert isinstance(entry["last_used_at"], int)

    def test_name_only_lookup_works(self, store):
        store.PlaylistStore.add_playlist("Mix", "deezer", "dz1")
        assert store.PlaylistStore.mark_used("Mix", "deezer")
        entry = store.PlaylistStore.find_playlist("Mix", "deezer")
        assert "last_used_at" in entry

    def test_missing_entry_is_noop(self, store):
        assert not store.PlaylistStore.mark_used("Ghost", "spotify", "")


class TestSetPinned:
    def test_unpin_removes_flag(self, store):
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        store.PlaylistStore.set_pinned("Chill Mix", "spotify", True, "pl1")
        assert store.PlaylistStore.set_pinned("Chill Mix", "spotify", False, "pl1")
        entry = store.PlaylistStore.find_playlist("Chill Mix", "spotify", playlist_id="pl1")
        # unpin removes the field entirely (absent == unpinned)
        assert entry.get("pinned", False) is False

    def test_unpinned_entries_have_no_flag(self, store):
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        entry = store.PlaylistStore.find_playlist("Chill Mix", "spotify", playlist_id="pl1")
        assert not entry.get("pinned", False)

    def test_missing_entry_is_noop(self, store):
        assert not store.PlaylistStore.set_pinned("Ghost", "spotify", True)

    def test_pinned_survives_readd_in_place(self, store):
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        store.PlaylistStore.set_pinned("Chill Mix", "spotify", True, "pl1")
        # same (platform, playlist_id) re-add updates in place, keeps flag
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        entries = store.PlaylistStore.load_playlists()
        assert len(entries) == 1
        assert entries[0]["pinned"] is True

    def test_pinned_flag_participates_in_sort(self, store):
        store.PlaylistStore.add_playlist("B", "spotify", "b1")
        store.PlaylistStore.add_playlist("A", "spotify", "a1")
        store.PlaylistStore.set_pinned("B", "spotify", True, "b1")
        entries = store.PlaylistStore.load_playlists()
        out = sort_playlists(entries, "name", "asc")
        assert [e["name"] for e in out] == ["B", "A"]