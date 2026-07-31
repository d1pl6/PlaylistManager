import sqlite3
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from constants import PLATFORM_YOUTUBE_MUSIC

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages SQLite connections for per-playlist databases.

    Connections are cached per-thread via ``threading.local()`` so that
    repeated ``get_connection()`` calls within the same thread (e.g.
    during bulk import) reuse the same connection instead of opening a
    new one each time.  Connections are closed automatically when the
    thread exits or when :meth:`close_thread_connections` is called.
    """

    _tls = threading.local()

    @staticmethod
    def _get_db_directory(platform: str) -> Path:
        """Get the database directory path for a given platform."""
        return Path(__file__).resolve().parents[2] / "db" / platform

    @staticmethod
    def get_playlist_db_path_static(playlist_name: str, platform: str) -> Path:
        db_dir = DatabaseManager._get_db_directory(platform)
        safe_name = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in playlist_name
        )
        return db_dir / f"{safe_name}.db"

    @staticmethod
    def delete_playlist_db(playlist_name: str, platform: str) -> None:
        """Delete a playlist database and its WAL sidecar files.

        Removes ``<name>.db`` together with ``<name>.db-wal`` and
        ``<name>.db-shm``, which SQLite leaves behind when a connection
        is still (or was recently) open in WAL mode.  Any connection
        cached for this database in the current thread is closed first
        so it cannot recreate the files.  Errors are logged and ignored
        so callers can treat this as best-effort cleanup.
        """
        db_path = DatabaseManager.get_playlist_db_path_static(
            playlist_name, platform
        )

        connections = DatabaseManager._get_thread_connections()
        cached = connections.pop(f"{playlist_name}:{platform}", None)
        if cached is not None:
            try:
                cached.close()
            except sqlite3.Error as e:
                logger.debug(
                    "Error closing cached connection for %s: %s", playlist_name, e
                )

        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            try:
                if path.exists():
                    path.unlink()
                    logger.info("Deleted database file %s", path)
            except OSError as e:
                logger.debug("Failed deleting %s: %s", path, e)

    def get_playlist_db_path(self, playlist_name: str, platform: str = PLATFORM_YOUTUBE_MUSIC) -> Path:
        """Get the database file path for a playlist.

        *platform* is required — callers always know which platform the
        playlist belongs to.  The old fallback-to-PlaylistStore lookup
        (which created a circular dependency) has been removed.
        """
        safe_name = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in playlist_name
        )
        db_dir = DatabaseManager._get_db_directory(platform)
        db_dir.mkdir(parents=True, exist_ok=True)
        return db_dir / f"{safe_name}.db"

    def get_db_connection(self, playlist_name: str, platform: str = PLATFORM_YOUTUBE_MUSIC) -> sqlite3.Connection:
        """Get a connection to the playlist's database, creating it if needed."""
        db_path = self.get_playlist_db_path(playlist_name, platform=platform)

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row  # Enable column access by name
            self._init_playlist_database(conn)
            _set_pragmas(conn)
            logger.debug(f"Connected to playlist database: {db_path}")
            return conn
        except sqlite3.Error as e:
            logger.error(f"Failed to connect to database {db_path}: {e}")
            raise

    @contextmanager
    def get_connection(self, playlist_name: str, platform: str = PLATFORM_YOUTUBE_MUSIC) -> Iterator[sqlite3.Connection]:
        """Context manager: yields a thread-cached connection.

        The connection is opened once per (thread, playlist, platform)
        combination and reused for the lifetime of the thread.  Call
        :meth:`close_thread_connections` to release resources.
        """
        cache_key = f"{playlist_name}:{platform}"
        connections = DatabaseManager._get_thread_connections()
        conn = connections.get(cache_key)
        if conn is None:
            conn = self.get_db_connection(playlist_name, platform=platform)
            connections[cache_key] = conn
        yield conn

    def _init_playlist_database(self, conn: sqlite3.Connection) -> None:
        """Initialize the database schema if it doesn't exist."""
        try:
            cursor = conn.cursor()

            # Check if songs table exists
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='songs'"
            )

            if cursor.fetchone() is None:
                # Create songs table
                cursor.execute("""
                    CREATE TABLE songs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        artists TEXT NOT NULL,
                        duration INTEGER,
                        track_id TEXT UNIQUE NOT NULL,
                        thumbnail_url TEXT,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Create index on track_id for faster lookups
                cursor.execute("CREATE INDEX idx_track_id ON songs(track_id)")

                conn.commit()
                logger.debug("Initialized new playlist database schema")
            else:
                logger.debug("Database schema already exists")

        except sqlite3.Error as e:
            logger.error(f"Failed to initialize database schema: {e}")
            raise

    @staticmethod
    def _get_thread_connections() -> dict:
        """Get the current thread's connection cache dict."""
        if not hasattr(DatabaseManager._tls, "_db_connections"):
            DatabaseManager._tls._db_connections = {}
        return DatabaseManager._tls._db_connections

    @staticmethod
    def close_thread_connections() -> None:
        """Close all cached connections for the calling thread."""
        connections = getattr(DatabaseManager._tls, "_db_connections", None)
        if connections is None:
            return
        for cache_key, conn in list(connections.items()):
            try:
                conn.close()
                logger.debug("Closed cached connection: %s", cache_key)
            except sqlite3.Error as e:
                logger.error("Error closing cached connection %s: %s", cache_key, e)
        DatabaseManager._tls._db_connections = {}

    @staticmethod
    def close_connection(conn: sqlite3.Connection) -> None:
        """Close a database connection."""
        try:
            if conn:
                conn.close()
                logger.debug("Closed database connection")
        except sqlite3.Error as e:
            logger.error(f"Error closing database connection: {e}")


def _set_pragmas(conn: sqlite3.Connection) -> None:
    """Set performance and safety pragmas on a connection.

    WAL mode provides better concurrency (readers don't block writers) and
    is more resilient on removable filesystems (exFAT).  synchronous=NORMAL
    paired with WAL gives a good safety/performance balance.

    These are safe to call on every connection — WAL mode is persistent
    in the database file so the second call is a no-op.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as e:
        logger.warning("Failed to set database pragmas: %s", e)
