"""Unit tests for services/database.py — SQLite connection management.

All file I/O is sandboxed: the static ``_get_db_directory`` is re-pointed
at a fresh tmp_path per test, and the class-level connection registry is
reset so no test sees another test's open handles.  Deleting DB files
uses the canonical ``delete_playlist_db`` / ``delete_platform_databases``
paths — against temp dirs only.
"""

import hashlib
from pathlib import Path

import pytest

from services.database import DatabaseManager, _db_stem, _sanitize_name


@pytest.fixture
def dbman(monkeypatch, tmp_path):
    monkeypatch.setattr(
        DatabaseManager,
        "_get_db_directory",
        staticmethod(lambda platform: tmp_path / "db" / platform),
    )
    monkeypatch.setattr(DatabaseManager, "_connections", {})
    return DatabaseManager()


class TestSanitizeName:
    def test_basic(self):
        assert _sanitize_name("Chill Mix") == "Chill_Mix"

    def test_special_chars(self):
        assert _sanitize_name("Mix: Vol. 1 [Sup]") == "Mix__Vol__1__Sup_"

    def test_unicode_kept(self):
        # isalnum() accepts unicode letters — only non-alnum/punct chars
        # are replaced.
        assert _sanitize_name(" 日本語 Playlist! ") == "_日本語_Playlist__"

    def test_empty(self):
        assert _sanitize_name("") == ""


class TestDbStem:
    def test_with_playlist_id(self):
        stem = _db_stem("Chill Mix", "pl1")
        expected_hash = hashlib.md5(b"pl1").hexdigest()[:8]
        assert stem == f"Chill_Mix_{expected_hash}"

    def test_without_playlist_id(self):
        assert _db_stem("Chill Mix", "") == "Chill_Mix"

    def test_same_name_different_ids_distinct(self):
        stem1 = _db_stem("Mix", "id1")
        stem2 = _db_stem("Mix", "id2")
        assert stem1 != stem2

    def test_sanitized_collisions_disambiguated(self):
        # "A/B" and "A B" both sanitize to "A_B"; their databases stay
        # apart via their *own* differently-hashed ids.
        assert _db_stem("A/B", "x") != _db_stem("A B", "y")


class TestDbPath:
    def test_path_shape(self, dbman, tmp_path):
        path = DatabaseManager.get_playlist_db_path_static("Chill Mix", "spotify", "pl1")
        assert path.parent == tmp_path / "db" / "spotify"
        assert path.name == f"Chill_Mix_{hashlib.md5(b'pl1').hexdigest()[:8]}.db"

    def test_legacy_no_id_matches_stem(self, dbman):
        path = DatabaseManager.get_playlist_db_path_static("Chill Mix", "spotify", "")
        assert path.name == "Chill_Mix.db"


class TestLegacyMigration:
    def test_migrate_legacy_to_hashed(self, dbman, tmp_path):
        # Simulate a pre-hash database sitting at the plain name.
        db_dir = tmp_path / "db" / "spotify"
        db_dir.mkdir(parents=True)
        legacy = db_dir / "Chill_Mix.db"
        legacy.write_bytes(b"sqlite")
        DatabaseManager._migrate_legacy_db_file(db_dir, "Chill Mix", "pl1")
        hashed = db_dir / f"Chill_Mix_{hashlib.md5(b'pl1').hexdigest()[:8]}.db"
        assert hashed.exists() and not legacy.exists()

    def test_migrate_moves_wal_sidecars(self, dbman, tmp_path):
        db_dir = tmp_path / "db" / "spotify"
        db_dir.mkdir(parents=True)
        legacy = db_dir / "Chill_Mix.db"
        legacy.write_bytes(b"a")
        Path(f"{legacy}-wal").write_bytes(b"wal")
        DatabaseManager._migrate_legacy_db_file(db_dir, "Chill Mix", "pl1")
        hashed = db_dir / f"Chill_Mix_{hashlib.md5(b'pl1').hexdigest()[:8]}.db"
        assert Path(f"{hashed}-wal").exists()

    def test_migrate_skips_when_hashed_exists(self, dbman, tmp_path):
        db_dir = tmp_path / "db" / "spotify"
        db_dir.mkdir(parents=True)
        legacy = db_dir / "Chill_Mix.db"
        legacy.write_bytes(b"a")
        hashed = db_dir / f"Chill_Mix_{hashlib.md5(b'pl1').hexdigest()[:8]}.db"
        hashed.write_bytes(b"b")
        DatabaseManager._migrate_legacy_db_file(db_dir, "Chill Mix", "pl1")
        assert legacy.exists()  # untouched — hashed file wins

    def test_get_playlist_db_path_triggers_migration(self, dbman, tmp_path):
        db_dir = tmp_path / "db" / "spotify"
        db_dir.mkdir(parents=True)
        legacy = db_dir / "Chill_Mix.db"
        legacy.write_bytes(b"sqlite")
        dbman.get_playlist_db_path("Chill Mix", "spotify", "pl1")
        hashed = db_dir / f"Chill_Mix_{hashlib.md5(b'pl1').hexdigest()[:8]}.db"
        assert hashed.exists() and not legacy.exists()


class TestConnection:
    def test_connection_wal_mode(self, dbman):
        with dbman.get_connection("Chill Mix", "spotify", "pl1") as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_busy_timeout_set(self, dbman):
        with dbman.get_connection("Chill Mix", "spotify", "pl1") as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000

    def test_schema_created(self, dbman):
        with dbman.get_connection("Chill Mix", "spotify", "pl1") as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "songs" in tables

    def test_shared_handle_within_thread(self, dbman):
        with dbman.get_connection("Chill Mix", "spotify") as c1:
            with dbman.get_connection("Chill Mix", "spotify") as c2:
                assert c1 is c2

    def test_close_thread_connections(self, dbman):
        with dbman.get_connection("Chill Mix", "spotify"):
            pass
        n_before = len(DatabaseManager._connections)
        dbman.close_thread_connections()
        assert len(DatabaseManager._connections) == n_before - 1


class TestDeletePlaylistDb:
    def test_removes_db_and_sidecars(self, dbman, tmp_path):
        path = DatabaseManager.get_playlist_db_path_static("Mix", "spotify", "pl1")
        # Create the WAL-sidecar trio deterministically.
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").write_bytes(b"x")
        dbman.delete_playlist_db("Mix", "spotify", "pl1")
        assert not path.exists()
        assert not Path(f"{path}-wal").exists()
        assert not Path(f"{path}-shm").exists()

    def test_removes_legacy_variant_too(self, dbman, tmp_path):
        hashed = DatabaseManager.get_playlist_db_path_static("Mix", "spotify", "pl1")
        legacy = DatabaseManager.get_playlist_db_path_static("Mix", "spotify", "")
        hashed.parent.mkdir(parents=True, exist_ok=True)
        for s in ("", "-wal", "-shm"):
            Path(f"{hashed}{s}").write_bytes(b"x")
            Path(f"{legacy}{s}").write_bytes(b"x")
        dbman.delete_playlist_db("Mix", "spotify", "pl1")
        assert not hashed.exists()
        assert not legacy.exists()

    def test_cached_conn_dropped_for_other_thread_simulated(self, dbman):
        # Deleting must drop the registry entry even when another
        # "thread" would hold it: verify only by key inspection via the
        # registry contents after delete.
        with dbman.get_connection("Mix", "spotify", "pl1"):
            pass
        dbman.delete_playlist_db("Mix", "spotify", "pl1")
        assert all(
            key[1] != "Mix" or key[2] != "spotify"
            for key in DatabaseManager._connections
        )

    def test_missing_db_is_noop(self, dbman):
        dbman.delete_playlist_db("Ghost", "spotify", "nope")  # must not raise


class TestDeletePlatformDatabases:
    def test_removes_all_and_directory(self, dbman, tmp_path):
        db_dir = tmp_path / "db" / "spotify"
        db_dir.mkdir(parents=True)
        (db_dir / "A.db").write_bytes(b"x")
        (db_dir / "B.db-wal").write_bytes(b"x")
        count = dbman.delete_platform_databases("spotify")
        assert count == 2
        assert not db_dir.exists()

    def test_removes_cached_conn_entries(self, dbman):
        with dbman.get_connection("Mix", "spotify", "pl1"):
            pass
        dbman.delete_platform_databases("spotify")
        assert all(key[2] != "spotify" for key in DatabaseManager._connections)

    def test_missing_platform_is_noop(self, dbman):
        assert dbman.delete_platform_databases("ghost") == 0