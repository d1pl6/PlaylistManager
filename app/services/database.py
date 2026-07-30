import sqlite3
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from constants import PLATFORM_YOUTUBE_MUSIC

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages SQLite connections for per-playlist databases."""

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
        """Context manager: yields a connection and closes it on exit."""
        conn = None
        try:
            conn = self.get_db_connection(playlist_name, platform=platform)
            yield conn
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

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
