import sqlite3
import json
import logging
import re
import threading
from typing import Dict, List, Optional, Callable
# Platform ids are declared by the plugin manifests (integrations/*/
# plugin.json); these local constants mirror the built-in ids so this
# service stays importable without the loader.
PLATFORM_YOUTUBE_MUSIC = "youtube_music"
PLATFORM_SPOTIFY = "spotify"
PLATFORM_SOUNDCLOUD = "soundcloud"
PLATFORM_DEEZER = "deezer"
from services.database import DatabaseManager

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Duration parsing
# ------------------------------------------------------------------

def _parse_duration(duration_str: str) -> int:
    """Parse a duration string like '3:45' or '1:02:30' to seconds.

    Returns 0 for unparseable or suspicious values.
    """
    stripped = duration_str.strip()
    if not stripped:
        return 0
    parts = stripped.split(":")
    if len(parts) > 3:
        logger.warning("Suspicious duration '%s' (too many parts), using 0", duration_str)
        return 0
    try:
        parts_int = [int(p) for p in parts]
    except ValueError:
        logger.warning("Unparseable duration '%s', using 0", duration_str)
        return 0
    if any(p < 0 for p in parts_int):
        logger.warning("Negative duration component in '%s', using 0", duration_str)
        return 0
    if len(parts_int) == 2 and parts_int[1] >= 60:
        logger.warning(
            "Duration '%s' has seconds >=60, result may be wrong", duration_str
        )
    elif len(parts_int) == 3 and (parts_int[1] >= 60 or parts_int[2] >= 60):
        logger.warning(
            "Duration '%s' has minute/second values >=60, result may be wrong", duration_str
        )
    seconds = 0
    for p in parts_int:
        seconds = seconds * 60 + p
    return seconds

# ------------------------------------------------------------------
# Platform track extractors for bulk insert
# ------------------------------------------------------------------

def _extract_youtube_track(track: dict) -> Optional[tuple]:
    """Extract fields from a ytmusicapi track dict.

    Returns (title, artists, duration_seconds, video_id, thumbnail_url)
    or None if the track has no videoId (e.g. unavailable / header item).
    """
    track_id = track.get("videoId")
    if not track_id:
        return None

    title = track.get("title", "Unknown")
    artists = [a.get("name", "Unknown") for a in track.get("artists", [])]
    if not artists:
        artists = ["Unknown Artist"]

    duration_raw = track.get("duration_seconds", 0) or track.get("duration", "0")
    if isinstance(duration_raw, str):
        duration = _parse_duration(duration_raw)
    else:
        duration = int(duration_raw)

    thumbnail_url = _pick_thumbnail(track.get("thumbnails", []))
    return (title, artists, duration, track_id, thumbnail_url)


def _extract_spotify_track(track: dict) -> Optional[tuple]:
    """Extract fields from a Spotify track dict.

    Returns (title, artists, duration_seconds, id, thumbnail_url)
    or None if the track has no id, or if it is not a track at all:
    episodes and audiobooks are valid Spotify playlist items but cannot
    be addressed by ``spotify:track:`` URIs (add/remove would fail
    platform-side), so they are excluded from the local mirror.
    """
    track_id = track.get("id")
    if not track_id:
        return None

    if track.get("type") and track.get("type") != "track":
        logger.debug(
            "Skipping non-track Spotify item %s (type=%s)", track_id, track.get("type")
        )
        return None

    title = track.get("name", "Unknown")
    artists = [a.get("name", "Unknown") for a in track.get("artists", [])]
    if not artists:
        artists = ["Unknown Artist"]

    duration_ms = track.get("duration_ms", 0)
    duration = duration_ms // 1000 if duration_ms else 0

    images = track.get("album", {}).get("images", [])
    thumbnail_url = None
    if len(images) >= 2:
        # Spotify returns images in descending resolution; index 1 is ~300×300.
        thumbnail_url = images[1]["url"]
    elif images:
        thumbnail_url = images[0]["url"]

    return (title, artists, duration, track_id, thumbnail_url)


def _extract_soundcloud_track(track: dict) -> Optional[tuple]:
    """Extract fields from a SoundCloud track dict.

    Returns (title, artists, duration_seconds, track_urn, thumbnail_url)
    or None if the track has no usable id/urn.  Tracks carry a ``urn``
    (``soundcloud:tracks:NNNN``); the numeric ``id`` is the fallback key.
    """
    urn = track.get("urn")
    tid = track.get("id")
    if not urn and tid is None:
        return None
    track_id = urn if urn else f"soundcloud:tracks:{tid}"

    title = track.get("title", "Unknown")
    user = track.get("user") or {}
    username = user.get("username")
    artists = [username] if username else ["Unknown Artist"]

    # SoundCloud duration is milliseconds.
    duration_ms = track.get("duration", 0) or 0
    duration = duration_ms // 1000 if duration_ms else 0

    thumbnail_url = track.get("artwork_url")
    return (title, artists, duration, track_id, thumbnail_url)


def _extract_deezer_track(track: dict) -> Optional[tuple]:
    """Extract fields from a Deezer Pipe GraphQL track dict.

    Returns (title, artists, duration_seconds, track_id, thumbnail_url)
    or None if the track has no usable id.  Deezer track IDs are numeric
    strings; duration is already in seconds from the GraphQL API.
    """
    track_id = track.get("id")
    if not track_id:
        return None
    track_id = str(track_id)

    title = track.get("title", "Unknown")

    # Extract artists from contributors edges.
    artists = []
    for edge in track.get("contributors", {}).get("edges", []):
        node = edge.get("node", {})
        name = node.get("name")
        if name:
            artists.append(name)
    if not artists:
        artists = ["Unknown Artist"]

    duration = track.get("duration", 0)

    # Extract thumbnail from album cover.  Pipe returns cover.urls from
    # largest to smallest (the last entry is 56x56 - too small for the
    # 64 px showcase covers), so pick the smallest size >= 64 px instead
    # of blindly taking the last (or the first, which is a ~1200 px
    # multi-hundred-KB image that gets downscaled away anyway).
    album = track.get("album") or {}
    cover = album.get("cover") or {}
    urls = cover.get("urls") or []
    thumbnail_url = _pick_deezer_cover(urls)

    return (title, artists, duration, track_id, thumbnail_url)


# Maps platform name → extractor callable.  Extractor signature:
#   (track: dict) -> (title, artists, duration, track_id, thumbnail_url) | None
_TRACK_EXTRACTORS: dict[str, Callable[[dict], Optional[tuple]]] = {
    PLATFORM_YOUTUBE_MUSIC: _extract_youtube_track,
    PLATFORM_SPOTIFY: _extract_spotify_track,
    PLATFORM_SOUNDCLOUD: _extract_soundcloud_track,
    PLATFORM_DEEZER: _extract_deezer_track,
}

# ------------------------------------------------------------------
# Thumbnail picking
# ------------------------------------------------------------------

# Deezer cover URLs embed the size in the path, e.g.
# ".../0/500x500-000000-80-0-0.jpg".  The hash part is hex-only, so the
# first (\d+)x\d+ match is the cover size.
_COVER_SIZE_RE = re.compile(r"(\d+)x\d+")


def _pick_deezer_cover(urls: list) -> Optional[str]:
    """Pick the smallest Deezer cover URL that is at least 64 px wide.

    Pipe's ``cover.urls`` are ordered largest-first (1200px down to
    56px).  Prefers the smallest entry >= 64 px (what the 64 px showcase
    covers need), falling back to the smallest available, then to None -
    mirroring :func:`_pick_thumbnail`.
    """
    if not urls:
        return None
    smallest = None
    smallest_size = None
    best = None
    best_size = None
    for item in urls:
        link = item.get("link") if isinstance(item, dict) else item
        if not link:
            continue
        m = _COVER_SIZE_RE.search(link)
        size = int(m.group(1)) if m else 0
        if size >= 64 and (best_size is None or size < best_size):
            best, best_size = link, size
        if not size or smallest_size is None or size < smallest_size:
            smallest, smallest_size = link, size
    return best or smallest


def _pick_thumbnail(thumbnails: list) -> Optional[str]:
    """Pick the most appropriate thumbnail URL from a platform thumbnail list.

    Prefers the smallest image that is at least 64 px wide, falling back
    to the smallest available if nothing meets the threshold, then to None.
    """
    if not thumbnails:
        return None
    # Prefer the smallest image >= 64 px  (avoids downloading multi-MB covers
    # that will be downscaled to 64×64 anyway).
    candidates = [t for t in thumbnails if t.get("width", 0) >= 64]
    if not candidates:
        # Nothing meets the threshold - take the smallest available.
        picked = min(thumbnails, key=lambda t: (t.get("width", 0), t.get("height", 0)))
    else:
        picked = min(candidates, key=lambda t: (t.get("width", 0), t.get("height", 0)))
    return picked.get("url")


# Normalisation helper
def _normalize_text(text: str) -> str:
    return text.strip().lower()

# ------------------------------------------------------------------
# SongManager
# ------------------------------------------------------------------

class SongManager:
    """Manages song CRUD operations for playlists.

    Thread-safe singleton - the single instance (and its ``db_manager``)
    are created once under a class-level lock so that concurrent calls
    from background sync threads never produce a second ``DatabaseManager``.
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance_lock.acquire()
            try:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst.db_manager = DatabaseManager()
                    cls._instance = inst
            finally:
                cls._instance_lock.release()
        return cls._instance

    def __init__(self):
        if hasattr(self, '_init_done'):
            return
        self._init_done = True

    def add_songs_bulk(
        self,
        playlist_name: str,
        tracks: List[Dict],
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> int:
        """
        Bulk-insert songs into the database, dispatching by platform.

        Args:
            playlist_name: Name of the playlist
            tracks: List of track dicts from the platform API
            platform: Platform identifier - plugin-declared id
                ("youtube_music", "spotify", "soundcloud", "deezer", ...).
                An unknown platform falls back to the YouTube extractor.
            playlist_id: Stable API identifier - selects this playlist's
                own database file (see DatabaseManager._db_stem)

        Returns:
            Number of songs actually inserted (skips duplicates)
        """
        extractor = _TRACK_EXTRACTORS.get(platform)
        if extractor is None:
            logger.warning(
                "Unknown platform '%s' for bulk insert, falling back to %s",
                platform, PLATFORM_YOUTUBE_MUSIC,
            )
            extractor = _extract_youtube_track
        return self._add_songs_bulk(
            playlist_name, tracks, extractor, platform, playlist_id
        )

    def _add_songs_bulk(
        self,
        playlist_name: str,
        tracks: List[Dict],
        extractor: Callable[[dict], Optional[tuple]],
        platform: str,
        playlist_id: str = "",
    ) -> int:
        """
        Core bulk insert - shared by all platform extractors.

        Args:
            playlist_name: Name of the playlist
            tracks: List of track dicts
            extractor: Callable(track) -> (title, artists, duration, track_id, thumbnail_url) | None
            platform: Platform label for log messages
            playlist_id: Stable API identifier - selects this playlist's
                own database file (see DatabaseManager._db_stem)

        Returns:
            Number of songs actually inserted (skips duplicates)
        """
        conn = None
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()

                rows = []
                skipped = 0
                for track in tracks:
                    song = extractor(track)
                    if song is None:
                        skipped += 1
                        continue
                    title, artists, duration, track_id, thumbnail_url = song
                    artists_json = json.dumps(artists)
                    rows.append((title, artists_json, duration, track_id, thumbnail_url))

                if skipped:
                    logger.debug(
                        "%s bulk insert into %s: skipped %d track(s) with no ID",
                        platform, playlist_name, skipped,
                    )

                rows_before = len(rows)
                cursor.executemany(
                    "INSERT OR IGNORE INTO songs (title, artists, duration, track_id, thumbnail_url) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()

                inserted = cursor.rowcount
                logger.info(
                    "Bulk insert (%s) into %s: %d/%d new songs",
                    platform, playlist_name, inserted, rows_before,
                )
                return inserted

        except sqlite3.Error as e:
            # Roll back the implicit transaction: a statement that fails
            # mid-execution leaves it OPEN on the thread-cached connection,
            # which would silently swallow later adds (add_song_by_info's
            # own_tx guard would then skip its commit and the row would be
            # lost when the thread exits).  conn is None only when the
            # connection itself failed to open - nothing to roll back.
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            logger.error("Failed bulk insert (%s) into %s: %s", platform, playlist_name, e)
            raise

    def song_exists_by_info(
        self,
        playlist_name: str,
        title: str,
        artists: List[str],
        duration: int,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> bool:
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()

                norm_title = _normalize_text(title)
                # Artists are stored as the raw list JSON, so compare against
                # the same JSON rather than a normalised variant.
                artists_json = json.dumps(artists)

                cursor.execute(
                    "SELECT id FROM songs WHERE LOWER(TRIM(title)) = ? AND artists = ? AND duration = ?",
                    (norm_title, artists_json, duration),
                )
                return cursor.fetchone() is not None

        except sqlite3.Error as e:
            logger.error("Error checking song by info in %s: %s", playlist_name, e)
            return False

    def add_song_by_info(
        self,
        playlist_name: str,
        title: str,
        artists: List[str],
        duration: int,
        track_id: str,
        thumbnail_url: Optional[str] = None,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> int:
        """
        Add a song by matching (title, artists, duration), using an atomic
        check-and-insert transaction to prevent TOCTOU races.

        Returns the new or existing song ID.

        Raises:
            sqlite3.Error: If database operation fails
        """
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()

                artists_json = json.dumps(artists)
                norm_title = _normalize_text(title)

                # Atomic transaction: check + insert to prevent TOCTOU.
                # Only open a transaction when none is active on this
                # thread-cached connection - a nested BEGIN would raise
                # "cannot start a transaction within a transaction".
                own_tx = not conn.in_transaction
                if own_tx:
                    cursor.execute("BEGIN IMMEDIATE")
                try:
                    cursor.execute(
                        "SELECT id FROM songs WHERE LOWER(TRIM(title)) = ? AND artists = ? AND duration = ?",
                        (norm_title, artists_json, duration),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        if own_tx:
                            conn.commit()
                        logger.info(
                            "Song already exists in %s (ID: %s, track_id: %s)",
                            playlist_name, existing["id"], track_id,
                        )
                        return existing["id"]

                    # INSERT OR IGNORE: the info check can miss a row that
                    # holds the same track_id with drifted metadata (artist
                    # order, title formatting, duration). A plain INSERT
                    # would then raise a UNIQUE constraint error after the
                    # platform add already succeeded, leaving the local DB
                    # without the song - reuse the existing row instead.
                    cursor.execute(
                        "INSERT OR IGNORE INTO songs (title, artists, duration, track_id, thumbnail_url) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (title, artists_json, duration, track_id, thumbnail_url),
                    )
                    if own_tx:
                        conn.commit()
                except Exception:
                    if own_tx:
                        conn.rollback()
                    raise

                if cursor.rowcount > 0:
                    song_id = cursor.lastrowid
                    if song_id is None:
                        raise RuntimeError("song_id is None after INSERT")
                    logger.info("Added song (info match) to %s (ID: %s)", playlist_name, song_id)
                    return song_id

                # Insert was ignored - the track_id already exists. Return
                # the existing row's ID (mirrors add_song's behaviour).
                cursor.execute("SELECT id FROM songs WHERE track_id = ?", (track_id,))
                row = cursor.fetchone()
                if row is not None:
                    logger.info(
                        "Song %s already exists in %s by track_id (existing ID: %s)",
                        track_id, playlist_name, row["id"],
                    )
                    return row["id"]

                # Defensive fallback - the row may have been inserted by
                # another thread between the check and the INSERT.
                cursor.execute(
                    "SELECT id FROM songs WHERE LOWER(TRIM(title)) = ? AND artists = ? AND duration = ?",
                    (norm_title, artists_json, duration),
                )
                row = cursor.fetchone()
                if row is not None:
                    return row["id"]
                raise RuntimeError(
                    "INSERT OR IGNORE reported no insert but no matching row found"
                )

        except sqlite3.Error as e:
            logger.error("Failed to add song to %s: %s", playlist_name, e)
            raise

    def add_song(
        self,
        playlist_name: str,
        title: str,
        artists: List[str],
        duration: int,
        track_id: str,
        thumbnail_url: Optional[str] = None,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> int:
        """
        Add a song to the playlist's database.

        Uses INSERT OR IGNORE so duplicates are handled silently.
        If the song already exists (by track_id UNIQUE constraint),
        its existing ID is returned instead.

        Args:
            playlist_name: Name of the playlist
            title: Song title
            artists: List of artist names
            duration: Duration in seconds
            track_id: Platform track/video ID
            thumbnail_url: URL to song thumbnail
            platform: Platform identifier for the per-platform DB

        Returns:
            Song ID (new or existing)

        Raises:
            sqlite3.Error: If database operation fails
        """
        conn = None
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()

                artists_json = json.dumps(artists)

                cursor.execute(
                    "INSERT OR IGNORE INTO songs (title, artists, duration, track_id, thumbnail_url) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (title, artists_json, duration, track_id, thumbnail_url),
                )
                conn.commit()

                if cursor.rowcount == 0:
                    # Song already exists - look up existing ID
                    cursor.execute("SELECT id FROM songs WHERE track_id = ?", (track_id,))
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError(
                            "INSERT OR IGNORE reported no insert but no existing row found"
                        )
                    song_id = row["id"]
                    logger.info(
                        "Song %s already exists in %s (existing ID: %s)",
                        track_id, playlist_name, song_id,
                    )
                    return song_id

                song_id = cursor.lastrowid
                if song_id is None:
                    raise RuntimeError("song_id is None after INSERT")
                logger.info(
                    "Added song %s to playlist %s (ID: %s)",
                    track_id, playlist_name, song_id,
                )
                return song_id

        except sqlite3.Error as e:
            # Roll back the implicit transaction - see _add_songs_bulk.
            # conn is None only when the connection failed to open.
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            logger.error("Failed to add song to %s: %s", playlist_name, e)
            raise

    def song_exists(
        self,
        playlist_name: str,
        track_id: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> bool:
        """
        Check if a song exists in the playlist's database by track ID.

        Args:
            playlist_name: Name of the playlist
            track_id: Platform track/video ID
            platform: Platform identifier for the per-platform DB

        Returns:
            True if song exists, False otherwise
        """
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM songs WHERE track_id = ?", (track_id,))
                return cursor.fetchone() is not None

        except sqlite3.Error as e:
            logger.error("Error checking if song exists in %s: %s", playlist_name, e)
            return False

    def get_song_by_track_id(
        self,
        playlist_name: str,
        track_id: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> Optional[Dict]:
        """
        Get song data by track ID.

        Args:
            playlist_name: Name of the playlist
            track_id: Platform track/video ID
            platform: Platform identifier for the per-platform DB

        Returns:
            Song data as dict or None if not found
        """
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM songs WHERE track_id = ?", (track_id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                song_dict = dict(row)
                song_dict["artists"] = json.loads(song_dict["artists"])
                return song_dict

        except sqlite3.Error as e:
            logger.error("Error getting song from %s: %s", playlist_name, e)
            return None

    def delete_song(
        self,
        playlist_name: str,
        song_id: int,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> bool:
        """
        Delete a song from the playlist's database.

        Args:
            playlist_name: Name of the playlist
            song_id: Song ID
            platform: Platform identifier for the per-platform DB

        Returns:
            True if deleted, False otherwise
        """
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM songs WHERE id = ?", (song_id,))
                conn.commit()
                logger.info("Deleted song %s from playlist %s", song_id, playlist_name)
                return cursor.rowcount > 0

        except sqlite3.Error as e:
            logger.error("Error deleting song from %s: %s", playlist_name, e)
            return False

    def get_all_songs(
        self,
        playlist_name: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> List[Dict]:
        """
        Get all songs from the playlist's database.

        Args:
            playlist_name: Name of the playlist
            platform: Platform identifier for the per-platform DB

        Returns:
            List of song dictionaries
        """
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM songs ORDER BY added_at DESC")
                rows = cursor.fetchall()
                songs = []
                for row in rows:
                    song_dict = dict(row)
                    song_dict["artists"] = json.loads(song_dict["artists"])
                    songs.append(song_dict)
                return songs

        except sqlite3.Error as e:
            logger.error("Error getting songs from %s: %s", playlist_name, e)
            return []

    def get_latest_song(
        self,
        playlist_name: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> Optional[Dict]:
        """
        Get the most recently added song from the playlist.

        Args:
            playlist_name: Name of the playlist
            platform: Platform identifier for the per-platform DB

        Returns a dict with keys (title, artists, duration, track_id,
        thumbnail_url), or None if the playlist is empty.
        """
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT title, artists, thumbnail_url, duration, track_id "
                    "FROM songs ORDER BY id DESC LIMIT 1"
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return {
                    "title": row[0],
                    "artists": json.loads(row[1]),
                    "thumbnail_url": row[2],
                    "duration": row[3],
                    "track_id": row[4],
                }

        except sqlite3.Error as e:
            logger.error("Error getting latest song from %s: %s", playlist_name, e)
            return None

    def get_latest_songs(
        self,
        playlist_name: str,
        limit: int,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> List[Dict]:
        """Get the most recently added songs from the playlist.

        Args:
            playlist_name: Name of the playlist
            limit: Maximum number of songs to return
            platform: Platform identifier for the per-platform DB

        Returns a list of up to *limit* song dicts, **newest first**
        (``ORDER BY id DESC`` - the same ordering as :meth:`get_latest_song`,
        monotonic with ``added_at`` and immune to same-second timestamp
        ties).  Each dict has keys (id, title, artists, thumbnail_url,
        duration, track_id).  Returns ``[]`` on sqlite errors.
        """
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, title, artists, thumbnail_url, duration, track_id "
                    "FROM songs ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                songs = []
                for row in cursor.fetchall():
                    songs.append(
                        {
                            "id": row[0],
                            "title": row[1],
                            "artists": json.loads(row[2]),
                            "thumbnail_url": row[3],
                            "duration": row[4],
                            "track_id": row[5],
                        }
                    )
                return songs

        except sqlite3.Error as e:
            logger.error("Error getting latest songs from %s: %s", playlist_name, e)
            return []

    def get_song_count(
        self,
        playlist_name: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> int:
        """
        Get the total number of songs in the playlist.

        Args:
            playlist_name: Name of the playlist
            platform: Platform identifier for the per-platform DB

        Returns:
            Number of songs
        """
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM songs")
                return cursor.fetchone()[0]

        except sqlite3.Error as e:
            logger.error("Error getting song count from %s: %s", playlist_name, e)
            return 0

    def get_total_duration(
        self,
        playlist_name: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
    ) -> int:
        """Get the total duration of all songs in the playlist (in seconds).

        Args:
            playlist_name: Name of the playlist
            platform: Platform identifier for the per-platform DB
            playlist_id: Stable API identifier for the per-playlist DB

        Returns:
            Total duration in seconds (0 on error or empty playlist).
        """
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COALESCE(SUM(duration), 0) FROM songs")
                row = cursor.fetchone()
                return row[0] if row else 0

        except sqlite3.Error as e:
            logger.error("Error getting total duration from %s: %s", playlist_name, e)
            return 0

    def search_songs(
        self,
        playlist_name: str,
        query: str,
        *,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
        limit: int = 5,
    ) -> List[Dict]:
        """Search songs by title or artist (case-insensitive LIKE).

        Wildcard characters (``%``, ``_``) in *query* are escaped so they
        are treated as literals.  Returns up to *limit* matches, newest
        first.  Returns ``[]`` on error or empty query.
        """
        if not query or not query.strip():
            return []
        # Escape SQL LIKE wildcards in user input.
        safe = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{safe}%"
        try:
            with self.db_manager.get_connection(
                playlist_name, platform=platform, playlist_id=playlist_id
            ) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, title, artists, thumbnail_url, duration, track_id "
                    "FROM songs "
                    "WHERE title LIKE ? ESCAPE '\\' "
                    "OR artists LIKE ? ESCAPE '\\' "
                    "ORDER BY added_at DESC LIMIT ?",
                    (pattern, pattern, limit),
                )
                songs = []
                for row in cursor.fetchall():
                    songs.append({
                        "id": row[0],
                        "title": row[1],
                        "artists": json.loads(row[2]),
                        "thumbnail_url": row[3],
                        "duration": row[4],
                        "track_id": row[5],
                    })
                return songs

        except sqlite3.Error as e:
            logger.error("Error searching songs in %s: %s", playlist_name, e)
            return []
