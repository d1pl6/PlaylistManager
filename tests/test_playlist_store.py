"""Unit tests for services/playlist_store.py - the playlist registry.

The module binds ``playlists_json`` at import (conftest already pointed
it at the temp tree); each test here re-points it at a fresh tmp_path
and resets the in-memory cache, so no test ever touches the real
``db/playlists.json``.  The destructive API (``delete_playlists_for_platform``,
``delete_playlist``) is exercised only against the sandboxed path.
"""

import json

import pytest

import services.playlist_store as ps


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "playlists_json", tmp_path / "db" / "playlists.json")
    monkeypatch.setattr(ps, "_playlist_cache", None)
    monkeypatch.setattr(ps, "_cache_timestamp", 0.0)
    yield ps


class TestAddPlaylist:
    def test_add_new(self, store):
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        playlists = store.PlaylistStore.load_playlists()
        assert len(playlists) == 1
        assert playlists[0]["name"] == "Chill Mix"
        assert playlists[0]["platform"] == "spotify"
        assert playlists[0]["playlist_id"] == "pl1"
        assert playlists[0]["keybind"] == ""

    def test_add_duplicate_by_id_updates_in_place(self, store):
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        # Same id, changed name/thumbnail - updated, not duplicated.
        store.PlaylistStore.add_playlist(
            "Chill Mix Updated", "spotify", "pl1", thumbnail_url="t.jpg"
        )
        playlists = store.PlaylistStore.load_playlists()
        assert len(playlists) == 1
        assert playlists[0]["name"] == "Chill Mix Updated"
        assert playlists[0]["thumbnail_url"] == "t.jpg"

    def test_add_keeps_keybind_on_update(self, store):
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        store.PlaylistStore.update_keybind("Chill Mix", "spotify", "ctrl+1", "pl1")
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        playlists = store.PlaylistStore.load_playlists()
        assert len(playlists) == 1
        assert playlists[0]["keybind"] == "ctrl+1"

    def test_same_name_different_id_appends(self, store):
        # A same-named different-id playlist cannot hijack the entry.
        store.PlaylistStore.add_playlist("Playlist", "spotify", "id1")
        store.PlaylistStore.add_playlist("Playlist", "spotify", "id2")
        playlists = store.PlaylistStore.load_playlists()
        assert len(playlists) == 2

    def test_legacy_entry_upgraded_with_id(self, store):
        # Pre-id entry: adding with an id adopts the name-matched legacy
        # entry instead of appending.
        store.PlaylistStore.add_playlist("Old", "spotify", "")
        store.PlaylistStore.add_playlist("Old", "spotify", "id1")
        playlists = store.PlaylistStore.load_playlists()
        assert len(playlists) == 1
        assert playlists[0]["playlist_id"] == "id1"

    def test_legacy_with_id_entry_not_hijacked(self, store):
        # A name-matched entry that ALREADY has a different id must not
        # be adopted.
        store.PlaylistStore.add_playlist("X", "spotify", "existing")
        store.PlaylistStore.add_playlist("X", "spotify", "new_id")
        assert len(store.PlaylistStore.load_playlists()) == 2


class TestFindPlaylist:
    @pytest.fixture(autouse=True)
    def _seed(self, store):
        store.PlaylistStore.add_playlist("Chill Mix", "spotify", "pl1")
        store.PlaylistStore.add_playlist("Bangers", "youtube_music", "pl2")

    def test_find_by_id(self, store):
        p = store.PlaylistStore.find_playlist("Chill Mix", "spotify", "pl1")
        assert p is not None and p["playlist_id"] == "pl1"

    def test_find_by_name_and_platform(self, store):
        p = store.PlaylistStore.find_playlist("Bangers", "youtube_music")
        assert p is not None and p["name"] == "Bangers"

    def test_find_by_name_only(self, store):
        p = store.PlaylistStore.find_playlist("Chill Mix")
        assert p is not None and p["name"] == "Chill Mix"

    def test_not_found(self, store):
        assert store.PlaylistStore.find_playlist("Nope", "spotify", "x") is None


class TestKeybind:
    def test_update_and_persist(self, store):
        store.PlaylistStore.add_playlist("Chill", "spotify", "pl1")
        assert store.PlaylistStore.update_keybind(
            "Chill", "spotify", "ctrl+shift+a", "pl1"
        ) is True
        p = store.PlaylistStore.find_playlist("Chill", "spotify", "pl1")
        assert p["keybind"] == "ctrl+shift+a"

    def test_update_missing_returns_false(self, store):
        assert store.PlaylistStore.update_keybind("Nope", "spotify", "ctrl+a") is False


class TestDelete:
    def test_delete_by_id(self, store):
        store.PlaylistStore.add_playlist("Chill", "spotify", "pl1")
        store.PlaylistStore.delete_playlist("Chill", "spotify", "pl1")
        assert store.PlaylistStore.find_playlist("Chill", "spotify", "pl1") is None

    def test_delete_legacy_by_name(self, store):
        store.PlaylistStore.add_playlist("Legacy", "youtube_music", "")
        store.PlaylistStore.delete_playlist("Legacy", "youtube_music")
        assert store.PlaylistStore.find_playlist("Legacy", "youtube_music") is None

    def test_delete_playlists_for_platform(self, store):
        store.PlaylistStore.add_playlist("A", "spotify", "1")
        store.PlaylistStore.add_playlist("B", "spotify", "2")
        store.PlaylistStore.add_playlist("C", "youtube_music", "3")
        removed = store.PlaylistStore.delete_playlists_for_platform("spotify")
        assert removed == 2
        remaining = store.PlaylistStore.load_playlists()
        assert [p["platform"] for p in remaining] == ["youtube_music"]


class TestQueries:
    def test_get_existing_names(self, store):
        store.PlaylistStore.add_playlist("A", "spotify", "1")
        store.PlaylistStore.add_playlist("B", "youtube_music", "2")
        assert store.PlaylistStore.get_existing_names() == {"A", "B"}
        assert store.PlaylistStore.get_existing_names("spotify") == {"A"}

    def test_get_existing_ids_by_platform(self, store):
        store.PlaylistStore.add_playlist("A", "spotify", "id-a")
        store.PlaylistStore.add_playlist("B", "spotify", "id-b")
        store.PlaylistStore.add_playlist("C", "youtube_music", "id-c")
        assert store.PlaylistStore.get_existing_ids_by_platform("spotify") == {
            "id-a", "id-b"
        }


class TestMisc:
    def test_ensure_playlists_file(self, store):
        store.PlaylistStore.ensure_playlists_file()
        assert store.playlists_json.exists()

    def test_still_registered(self, store):
        store.PlaylistStore.add_playlist("Chill", "spotify", "pl1")
        assert store.PlaylistStore.playlist_still_registered("Chill", "spotify", "pl1") is True
        assert store.PlaylistStore.playlist_still_registered("Gone", "spotify", "nope") is False

    def test_module_level_alias_still_imports(self, store):
        # Plugin repos are separate and downloaded at runtime, so a service
        # symbol their flow.py imports is part of the plugin API.  The
        # free function was the pre-staticmethod spelling; removing it broke
        # every published plugin at IMPORT time (killing the whole hotkey
        # path), not just the liveness check.
        store.PlaylistStore.add_playlist("Chill", "spotify", "pl1")
        assert store.playlist_still_registered("Chill", "spotify", "pl1") is True
        assert store.playlist_still_registered("Gone", "spotify", "nope") is False

    def test_module_level_alias_matches_the_staticmethod(self, store):
        store.PlaylistStore.add_playlist("Chill", "spotify", "pl1")
        for args in (("Chill", "spotify", "pl1"), ("Gone", "spotify", "nope")):
            assert store.playlist_still_registered(*args) == (
                store.PlaylistStore.playlist_still_registered(*args)
            )

    def test_reload_after_external_change(self, store, monkeypatch):
        import time
        # Simulate another process rewriting the file - cache TTL expiry
        # must pick it up.
        store.PlaylistStore.add_playlist("A", "spotify", "1")
        store.playlists_json.write_text(
            '[{"name": "B", "platform": "youtube_music", "keybind": "", '
            '"playlist_id": "2", "thumbnail_url": ""}]',
            encoding="utf-8",
        )
        monkeypatch.setattr(store, "_cache_timestamp", time.monotonic() - 5)
        assert store.PlaylistStore.get_existing_names() == {"B"}

    def test_corrupt_file_returns_empty_not_crash(self, store, monkeypatch):
        import time
        monkeypatch.setattr(store, "_cache_timestamp", time.monotonic() - 5)
        store.playlists_json.parent.mkdir(parents=True, exist_ok=True)
        store.playlists_json.write_text("{corrupt json", encoding="utf-8")
        # First load keeps previous cache; force a cold start.
        monkeypatch.setattr(store, "_playlist_cache", None)
        assert store.PlaylistStore.load_playlists() == []

class TestLegacyPlatformMigration:
    """Rows written before the "platform" field existed.

    The field is stamped once, at load, so no call site carries a
    YouTube Music fallback anymore (that fallback is exactly what was
    deleted from keybind_registry, song_manager, cli, ...).
    """

    @staticmethod
    def _write_registry(store, rows):
        store.playlists_json.parent.mkdir(parents=True, exist_ok=True)
        store.playlists_json.write_text(
            __import__("json").dumps(rows), encoding="utf-8"
        )

    def test_missing_platform_stamped_on_load(self, store):
        self._write_registry(
            store, [{"name": "Old Mix", "keybind": "ctrl+1", "playlist_id": ""}]
        )
        playlists = store.PlaylistStore.load_playlists()
        assert playlists[0]["platform"] == "youtube_music"
        assert playlists[0]["keybind"] == "ctrl+1"  # untouched

    def test_stamp_is_persisted(self, store):
        self._write_registry(store, [{"name": "Old Mix"}])
        store.PlaylistStore.load_playlists()
        on_disk = store.playlists_json.read_text(encoding="utf-8")
        assert '"platform": "youtube_music"' in on_disk

    def test_migrated_row_is_addressable_by_platform(self, store):
        # The read sites that used to fall back per call: a platform
        # filter and the platform-wide delete must both see the row.
        self._write_registry(store, [{"name": "Old Mix"}, {"name": "Kept", "platform": "spotify"}])
        assert store.PlaylistStore.get_existing_names("youtube_music") == {"Old Mix"}
        assert store.PlaylistStore.delete_playlists_for_platform("youtube_music") == 1
        assert [p["name"] for p in store.PlaylistStore.load_playlists()] == ["Kept"]

    def test_existing_platform_is_left_alone(self, store):
        self._write_registry(store, [{"name": "S", "platform": "spotify"}])
        playlists = store.PlaylistStore.load_playlists()
        assert playlists[0]["platform"] == "spotify"

    def test_empty_platform_is_stamped(self, store):
        # Same semantics the deleted `or "youtube_music"` fallback had:
        # a falsy platform meant "written before platforms existed".
        self._write_registry(store, [{"name": "Old Mix", "platform": ""}])
        playlists = store.PlaylistStore.load_playlists()
        assert playlists[0]["platform"] == "youtube_music"


class TestRegistryNumbers:
    """``registry_numbers()`` - the ``playlistmanager --list`` number map.

    The card grid shows these numbers (Settings > show_playlist_numbers),
    so the map must mirror ``cli.run_list`` exactly: 1-based STORAGE order,
    every row counted, keyed the way a caller holding a card's identity
    (platform, id-or-name) looks itself up.
    """

    @staticmethod
    def _rows():
        return [
            {"name": "Chill", "platform": "spotify", "playlist_id": "pl1"},
            {"name": "Bangers", "platform": "youtube_music", "playlist_id": "pl2"},
            # Legacy row: no id -> falls back to the name key.
            {"name": "Old Mix", "platform": "spotify", "playlist_id": ""},
        ]

    def test_numbers_follow_registry_order(self, store):
        store.playlists_json.parent.mkdir(parents=True, exist_ok=True)
        store.playlists_json.write_text(json.dumps(self._rows()), encoding="utf-8")
        assert store.registry_numbers() == {
            ("spotify", "pl1"): 1,
            ("youtube_music", "pl2"): 2,
            ("spotify", "Old Mix"): 3,
        }

    def test_matches_run_list_position(self, store):
        # Same rows, same numbers as `run_list` prints: enumerate the
        # registry 1-based, no sorting, no filtering.
        store.playlists_json.parent.mkdir(parents=True, exist_ok=True)
        store.playlists_json.write_text(json.dumps(self._rows()), encoding="utf-8")
        playlists = store.PlaylistStore.load_playlists()
        numbers = store.registry_numbers(playlists)
        for i, entry in enumerate(playlists, 1):
            key = (
                entry.get("platform", ""),
                entry.get("playlist_id") or entry.get("name"),
            )
            assert numbers[key] == i

    def test_hidden_platform_rows_keep_their_slots(self, store):
        # --list numbers EVERY registry row, including one whose plugin is
        # not installed (its card is hidden from the grid): visible cards
        # then show non-contiguous numbers, matching the CLI.
        rows = [
            {"name": "A", "platform": "spotify", "playlist_id": "a"},
            {"name": "Gone", "platform": "not_installed", "playlist_id": "d"},
            {"name": "B", "platform": "spotify", "playlist_id": "b"},
        ]
        numbers = store.registry_numbers(rows)
        assert numbers[("spotify", "a")] == 1
        assert numbers[("spotify", "b")] == 3

    def test_duplicate_key_keeps_first_occurrence(self, store):
        # Two legacy same-name rows are indistinguishable everywhere else
        # too (cli.resolve_targets dedups on the same key): first wins.
        rows = [
            {"name": "Same", "platform": "spotify", "playlist_id": ""},
            {"name": "Same", "platform": "spotify", "playlist_id": ""},
        ]
        assert store.registry_numbers(rows)[("spotify", "Same")] == 1

    def test_same_name_different_id_are_distinct(self, store):
        rows = [
            {"name": "Same", "platform": "spotify", "playlist_id": "id1"},
            {"name": "Same", "platform": "spotify", "playlist_id": "id2"},
        ]
        numbers = store.registry_numbers(rows)
        assert numbers[("spotify", "id1")] == 1
        assert numbers[("spotify", "id2")] == 2
