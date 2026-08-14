import sqlite3
import logging
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from constants import PLATFORM_YOUTUBE_MUSIC

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages SQLite connections for per-playlist databases.

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
    # key: (thread_id, playlist_name, platform) -> (thread_weakref, conn)
    _connections: dict = {}

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
    def _prune_dead_connections() -> None:
        """Drop cache entries whose owning thread has exited.

        The connection is NOT closed here - CPython's sqlite3 forbids
        closing a connection from any thread other than the one that
        created it.  Dropping the last registry reference lets the
        connection be garbage-collected (and closed) once the owning
        thread is gone.
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
        """Close a connection owned by the CALLING thread.

        Only used from :meth:`close_thread_connections` - sqlite3 raises
        ProgrammingError when close() runs on a different thread than
        connect().
        """
        try:
            conn.close()
            logger.debug("Closed cached connection: %s", cache_key)
        except sqlite3.Error as e:
            logger.error("Error closing cached connection %s: %s", cache_key, e)

    @staticmethod
    def delete_playlist_db(playlist_name: str, platform: str) -> None:
        """Delete a playlist database and its WAL sidecar files.

        Removes ``<name>.db`` together with ``<name>.db-wal`` and
        ``<name>.db-shm``, which SQLite leaves behind when a connection
        is still (or was recently) open in WAL mode.  Every thread's
        cached connection for this database is dropped from the registry
        first - not just the calling thread's - so a flow thread or
        reload worker that still holds a handle can neither recreate the
        files on a later ``get_connection`` nor keep the inode alive
        beyond its own lifetime (each owner closes its own handle when
        its ``with`` block exits).  Errors are logged and ignored so
        callers can treat this as best-effort cleanup.
        """
        db_path = DatabaseManager.get_playlist_db_path_static(
            playlist_name, platform
        )

        with DatabaseManager._connections_lock:
            DatabaseManager._prune_dead_connections()
            for key in [
                key
                for key in DatabaseManager._connections
                if key[1] == playlist_name and key[2] == platform
            ]:
                # Drop without closing: the owning thread (still alive)
                # closes it itself when it exits the `with` block.
                del DatabaseManager._connections[key]

        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            try:
                if path.exists():
                    path.unlink()
                    logger.info("Deleted database file %s", path)
            except OSError as e:
                logger.debug("Failed deleting %s: %s", path, e)

    @staticmethod
    def delete_platform_databases(platform: str) -> int:
        """Delete every playlist database for *platform*.

        Closes every thread's cached connections first (they would
        otherwise recreate the deleted files on the next write) then
        removes every ``*.db``, ``*.db-wal`` and ``*.db-shm`` under
        ``db/<platform>/`` and rmdirs the platform directory when it is
        empty.  Best-effort: per-file errors are logged and skipped.
        Returns the number of files removed.
        """
        with DatabaseManager._connections_lock:
            DatabaseManager._prune_dead_connections()
            for key in [
                key
                for key in DatabaseManager._connections
                if key[2] == platform
            ]:
                # Drop without closing - see delete_playlist_db.
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

    def get_playlist_db_path(self, playlist_name: str, platform: str = PLATFORM_YOUTUBE_MUSIC) -> Path:
        """Get the database file path for a playlist.

        *platform* is required - callers always know which platform the
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
        :meth:`close_thread_connections` to release the calling thread's
        connections, or :meth:`delete_playlist_db` to release every
        thread's connection for one database.
        """
        cache_key = (threading.get_ident(), playlist_name, platform)
        current_thread = threading.current_thread()
        with DatabaseManager._connections_lock:
            DatabaseManager._prune_dead_connections()
            entry = DatabaseManager._connections.get(cache_key)
            if entry is not None and entry[0]() is current_thread:
                conn = entry[1]
            else:
                if entry is not None:
                    # Stale entry: the owning thread has exited but its
                    # Thread object is still referenced somewhere (so
                    # _prune_dead_connections kept it), and this thread
                    # now reuses the same OS thread id.  Handing out that
                    # connection would raise ProgrammingError on first
                    # use - drop it instead; the connection is finalized
                    # with the entry.
                    del DatabaseManager._connections[cache_key]
                conn = self.get_db_connection(playlist_name, platform=platform)
                DatabaseManager._connections[cache_key] = (
                    weakref.ref(current_thread),
                    conn,
                )
        yield conn

    def _init_playlist_database(self, conn: sqlite3.Connection) -> None:
        """Initialize the database schema if it doesn't exist.

        Uses ``IF NOT EXISTS`` so concurrent threads opening the same
        brand-new database (e.g. a keybind flow thread racing a reload
        worker) cannot fail on a duplicate CREATE.
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
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_track_id ON songs(track_id)"
            )
            conn.commit()
            logger.debug("Initialized playlist database schema")
        except sqlite3.Error as e:
            logger.error(f"Failed to initialize database schema: {e}")
            raise

    @staticmethod
    def close_thread_connections() -> None:
        """Close all cached connections for the calling thread."""
        thread_id = threading.get_ident()
        current_thread = threading.current_thread()
        with DatabaseManager._connections_lock:
            DatabaseManager._prune_dead_connections()
            for key in [
                key
                for key in DatabaseManager._connections
                if key[0] == thread_id
                # Ownership is thread *identity*, not just id: a dead
                # thread whose Thread object is still referenced can have
                # its id reused, and closing its connection from here
                # would raise ProgrammingError (see get_connection).
                and DatabaseManager._connections[key][0]() is current_thread
            ]:
                conn = DatabaseManager._connections.pop(key)[1]
                DatabaseManager._close_connection(key, conn)


def _set_pragmas(conn: sqlite3.Connection) -> None:
    """Set performance and safety pragmas on a connection.

    WAL mode provides better concurrency (readers don't block writers) and
    is more resilient on removable filesystems (exFAT).  synchronous=NORMAL
    paired with WAL gives a good safety/performance balance.

    busy_timeout is per-connection, so it must be set on every connection:
    without it sqlite falls back to its 5s default and a keybind flow that
    collides with a reload's write lock raises "database is locked" after
    5s - an error that would also poison the thread-cached connection (see
    song_manager's rollback-on-error).  30s is comfortably longer than a
    bulk re-import holds the write lock.

    These are safe to call on every connection - WAL mode is persistent
    in the database file so the second call is a no-op.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error as e:
        logger.warning("Failed to set database pragmas: %s", e)
