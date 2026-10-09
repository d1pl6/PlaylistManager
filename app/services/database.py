import hashlib
import sqlite3
import logging
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from services import profile_store as _profile_store

logger = logging.getLogger(__name__)


def _sanitize_name(playlist_name: str) -> str:
    """
    Map a playlist name to a filesystem-safe stem.
    """
    return "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in playlist_name
    )


def _db_stem(playlist_name: str, playlist_id: str = "") -> str:
    """
    File stem for a playlist DB: sanitized name + id hash when known.
    """
    stem = _sanitize_name(playlist_name)
    if playlist_id:
        digest = hashlib.md5(playlist_id.encode("utf-8")).hexdigest()[:8]
        stem = f"{stem}_{digest}"
    return stem


class DatabaseManager:
    """
    Manages SQLite connections for per-playlist databases.

    Connections are cached per thread (keyed by thread id) so repeated
    ``get_connection()`` calls within the same thread - e.g. during bulk
    import or a keybind flow - reuse one connection instead of opening a
    new one each time.  The cache is a class-level registry guarded by a
    lock, which lets :meth:`delete_playlist_db` drop every thread's
    cached handle for a database before unlinking its files: a
    connection held by another thread (a keybind flow thread, a reload
    worker) would otherwise be handed out again on the next
    ``get_connection`` and recreate the deleted files.  Entries whose
    owning thread has exited are pruned lazily.
    """

    _connections_lock = threading.Lock()
    # key: (thread_id, playlist_name, platform, playlist_id) -> (thread_weakref, conn)
    _connections: dict = {}
    # Full-registry prune at most once every N acquires (each prune is
    # O(registry size); long-lived UI threads make most acquires hits).
    _PRUNE_INTERVAL = 64
    _prune_counter = 0

    @staticmethod
    def _get_db_directory(platform: str) -> Path:
        return _profile_store.db_dir() / platform

    @staticmethod
    def get_playlist_db_path_static(
        playlist_name: str, platform: str, playlist_id: str = ""
    ) -> Path:
        db_dir = DatabaseManager._get_db_directory(platform)
        return db_dir / f"{_db_stem(playlist_name, playlist_id)}.db"

    @staticmethod
    def _prune_dead_connections() -> None:
        """
        Drop cache entries whose owning thread has exited.
        """
        dead = [
            key
            for key, (thread_ref, _conn) in DatabaseManager._connections.items()
            if thread_ref() is None
        ]
        for key in dead:
            del DatabaseManager._connections[key]

    @staticmethod
    def _close_connection(cache_key: tuple, conn: sqlite3.Connection) -> None:
        """
        Close a connection owned by the CALLING thread.
        """
        try:
            conn.close()
            logger.debug("Closed cached connection: %s", cache_key)
        except sqlite3.Error as e:
            logger.error("Error closing cached connection %s: %s", cache_key, e)

    @staticmethod
    def delete_playlist_db(
        playlist_name: str, platform: str, playlist_id: str = ""
    ) -> None:
        """
        Delete a playlist database and its WAL sidecar files.
        """
        paths = [
            DatabaseManager.get_playlist_db_path_static(
                playlist_name, platform, playlist_id
            )
        ]
        if playlist_id:
            paths.append(
                DatabaseManager.get_playlist_db_path_static(playlist_name, platform, "")
            )

        with DatabaseManager._connections_lock:
            DatabaseManager._prune_dead_connections()
            for key in [
                key
                for key in DatabaseManager._connections
                if key[1] == playlist_name
                and key[2] == platform
                and (
                    not playlist_id
                    or key[3] in ("", playlist_id)
                )
            ]:
                # Drop without closing: the owning thread (still alive)
                # closes it itself when it exits the `with` block.
                del DatabaseManager._connections[key]

        for db_path in paths:
            for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
                try:
                    if path.exists():
                        path.unlink()
                        logger.info("Deleted database file %s", path)
                except OSError as e:
                    logger.debug("Failed deleting %s: %s", path, e)

    @staticmethod
    def delete_platform_databases(platform: str) -> int:
        """
        Delete every playlist database for *platform*.
        """
        with DatabaseManager._connections_lock:
            DatabaseManager._prune_dead_connections()
            for key in [
                key
                for key in DatabaseManager._connections
                if key[2] == platform
            ]:
                # Drop without closing.
                del DatabaseManager._connections[key]

        db_dir = DatabaseManager._get_db_directory(platform)
        if not db_dir.is_dir():
            return 0
        removed = 0
        for pattern in ("*.db", "*.db-wal", "*.db-shm"):
            for path in sorted(db_dir.glob(pattern)):
                try:
                    path.unlink()
                    logger.info("Deleted database file %s", path)
                    removed += 1
                except OSError as e:
                    logger.debug("Failed deleting %s: %s", path, e)
        try:
            db_dir.rmdir()  # only removes the directory when empty
        except OSError:
            pass
        return removed

    def get_playlist_db_path(
        self,
        playlist_name: str,
        platform: str,
        playlist_id: str = "",
    ) -> Path:
        """
        Get the database file path for a playlist.
        """
        db_dir = DatabaseManager._get_db_directory(platform)
        db_dir.mkdir(parents=True, exist_ok=True)
        if playlist_id:
            self._migrate_legacy_db_file(db_dir, playlist_name, playlist_id)
        return db_dir / f"{_db_stem(playlist_name, playlist_id)}.db"

    @staticmethod
    def _migrate_legacy_db_file(
        db_dir: Path, playlist_name: str, playlist_id: str
    ) -> None:
        """
        Rename a pre-hash database file to its id-hashed name.
        """
        legacy = db_dir / f"{_sanitize_name(playlist_name)}.db"
        new = db_dir / f"{_db_stem(playlist_name, playlist_id)}.db"
        if new.exists() or not legacy.exists():
            return
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(f"{legacy}{suffix}")
                if src.exists():
                    src.rename(Path(f"{new}{suffix}"))
            logger.info("Migrated legacy database %s -> %s", legacy.name, new.name)
        except OSError as e:
            logger.warning("Failed to migrate legacy database %s: %s", legacy.name, e)

    def _get_db_connection(
        self,
        playlist_name: str,
        platform: str,
        playlist_id: str = "",
    ) -> sqlite3.Connection:
        """
        Open a fresh (uncached) connection to the playlist's database.
        """
        db_path = self.get_playlist_db_path(
            playlist_name, platform=platform, playlist_id=playlist_id
        )

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            _set_pragmas(conn)
            self._init_playlist_database(conn)
            logger.debug("Connected to playlist database: %s", db_path)
            return conn
        except sqlite3.Error as e:
            logger.error("Failed to connect to database %s: %s", db_path, e)
            raise

    @contextmanager
    def get_connection(
        self,
        playlist_name: str,
        platform: str,
        playlist_id: str = "",
    ) -> Iterator[sqlite3.Connection]:
        """
        Context manager: yields a thread-cached connection.
        """
        cache_key = (threading.get_ident(), playlist_name, platform, playlist_id)
        current_thread = threading.current_thread()
        with DatabaseManager._connections_lock:
            # Throttled: a full-registry prune is O(registry size) and
            # long-lived threads do far more hits than registry churn.
            DatabaseManager._prune_counter = (
                DatabaseManager._prune_counter + 1
            ) % DatabaseManager._PRUNE_INTERVAL
            if DatabaseManager._prune_counter == 0:
                DatabaseManager._prune_dead_connections()
            entry = DatabaseManager._connections.get(cache_key)
            if entry is not None and entry[0]() is current_thread:
                conn = entry[1]
            else:
                if entry is not None:
                    del DatabaseManager._connections[cache_key]
                conn = self._get_db_connection(
                    playlist_name, platform=platform, playlist_id=playlist_id
                )
                DatabaseManager._connections[cache_key] = (
                    weakref.ref(current_thread),
                    conn,
                )
        yield conn

    def _init_playlist_database(self, conn: sqlite3.Connection) -> None:
        """
        Initialize the database schema if it doesn't exist.
        """
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS songs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    artists TEXT NOT NULL,
                    duration INTEGER,
                    track_id TEXT UNIQUE NOT NULL,
                    thumbnail_url TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("DROP INDEX IF EXISTS idx_track_id")
            # Single-column expression indexes need SQLite >= 3.9
            # (2020) - fine for the Python 3.10+ requirement here.
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_songs_title_norm "
                "ON songs (LOWER(TRIM(title)))"
            )
            conn.commit()
            logger.debug("Initialized playlist database schema")
        except sqlite3.Error as e:
            logger.error("Failed to initialize database schema: %s", e)
            raise

    @staticmethod
    def close_thread_connections() -> None:
        """
        Close all cached connections for the calling thread.
        """
        thread_id = threading.get_ident()
        current_thread = threading.current_thread()
        with DatabaseManager._connections_lock:
            DatabaseManager._prune_dead_connections()
            for key in [
                key
                for key in DatabaseManager._connections
                if key[0] == thread_id
                and DatabaseManager._connections[key][0]() is current_thread
            ]:
                conn = DatabaseManager._connections.pop(key)[1]
                DatabaseManager._close_connection(key, conn)


def _set_pragmas(conn: sqlite3.Connection) -> None:
    """
    Set performance and safety pragmas on a connection.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error as e:
        logger.warning("Failed to set database pragmas: %s", e)
