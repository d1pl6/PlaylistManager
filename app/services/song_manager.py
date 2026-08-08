import sqlite3
import json
import logging
import threading
from typing import Dict, List, Optional, Callable
from constants import PLATFORM_YOUTUBE_MUSIC, PLATFORM_SPOTIFY
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
        # "M:SS" - a seconds value >= 60 means the string is malformed
        # (e.g. "3:75" instead of "4:15"); the result is still computed.
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
    or None if the track has no id.
    """
    track_id = track.get("id")
    if not track_id:
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


# Maps platform name → extractor callable.  Extractor signature:
#   (track: dict) -> (title, artists, duration, track_id, thumbnail_url) | None
_TRACK_EXTRACTORS: dict[str, Callable[[dict], Optional[tuple]]] = {
    PLATFORM_YOUTUBE_MUSIC: _extract_youtube_track,
    PLATFORM_SPOTIFY: _extract_spotify_track,
}

# ------------------------------------------------------------------
# Thumbnail picking
# ------------------------------------------------------------------

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
    ) -> int:
        """
        Bulk-insert songs into the database, dispatching by platform.

        Args:
            playlist_name: Name of the playlist
            tracks: List of track dicts from the platform API
            platform: Platform identifier (PLATFORM_YOUTUBE_MUSIC or PLATFORM_SPOTIFY)

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
        return self._add_songs_bulk(playlist_name, tracks, extractor, platform)

    def _add_songs_bulk(
        self,
        playlist_name: str,
        tracks: List[Dict],
        extractor: Callable[[dict], Optional[tuple]],
        platform: str,
    ) -> int:
        """
        Core bulk insert - shared by YouTube Music and Spotify.

        Args:
            playlist_name: Name of the playlist
            tracks: List of track dicts
            extractor: Callable(track) -> (title, artists, duration, track_id, thumbnail_url) | None
            platform: Platform label for log messages

        Returns:
            Number of songs actually inserted (skips duplicates)
        """
        try:
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
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
            logger.error("Failed bulk insert (%s) into %s: %s", platform, playlist_name, e)
            raise

    def song_exists_by_info(
        self,
        playlist_name: str,
        title: str,
        artists: List[str],
        duration: int,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
    ) -> bool:
        try:
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
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
            logger.error(f"Error checking song by info in {playlist_name}: {e}")
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
    ) -> int:
        """
        Add a song by matching (title, artists, duration), using an atomic
        check-and-insert transaction to prevent TOCTOU races.

        Returns the new or existing song ID.

        Raises:
            sqlite3.Error: If database operation fails
        """
        try:
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
                cursor = conn.cursor()

                artists_json = json.dumps(artists)
                norm_title = _normalize_text(title)

                # Atomic transaction: check + insert to prevent TOCTOU
                cursor.execute("BEGIN IMMEDIATE")
                try:
                    cursor.execute(
                        "SELECT id FROM songs WHERE LOWER(TRIM(title)) = ? AND artists = ? AND duration = ?",
                        (norm_title, artists_json, duration),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        conn.commit()
                        logger.info(
                            "Song already exists in %s (ID: %s, track_id: %s)",
                            playlist_name, existing["id"], track_id,
                        )
                        return existing["id"]

                    cursor.execute(
                        "INSERT INTO songs (title, artists, duration, track_id, thumbnail_url) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (title, artists_json, duration, track_id, thumbnail_url),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

                song_id = cursor.lastrowid
                if song_id is None:
                    raise RuntimeError("song_id is None after INSERT")
                logger.info("Added song (info match) to %s (ID: %s)", playlist_name, song_id)
                return song_id

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
        try:
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
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
            logger.error("Failed to add song to %s: %s", playlist_name, e)
            raise

    def song_exists(
        self,
        playlist_name: str,
        track_id: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
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
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM songs WHERE track_id = ?", (track_id,))
                return cursor.fetchone() is not None

        except sqlite3.Error as e:
            logger.error(f"Error checking if song exists in {playlist_name}: {e}")
            return False

    def get_song_by_track_id(
        self,
        playlist_name: str,
        track_id: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
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
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM songs WHERE track_id = ?", (track_id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                song_dict = dict(row)
                song_dict["artists"] = json.loads(song_dict["artists"])
                return song_dict

        except sqlite3.Error as e:
            logger.error(f"Error getting song from {playlist_name}: {e}")
            return None

    def delete_song(
        self,
        playlist_name: str,
        song_id: int,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
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
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM songs WHERE id = ?", (song_id,))
                conn.commit()
                logger.info(f"Deleted song {song_id} from playlist {playlist_name}")
                return cursor.rowcount > 0

        except sqlite3.Error as e:
            logger.error(f"Error deleting song from {playlist_name}: {e}")
            return False

    def get_all_songs(
        self,
        playlist_name: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
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
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
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
            logger.error(f"Error getting songs from {playlist_name}: {e}")
            return []

    def get_latest_song(
        self,
        playlist_name: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
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
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
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
            logger.error(f"Error getting latest song from {playlist_name}: {e}")
            return None

    def get_song_count(
        self,
        playlist_name: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
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
            with self.db_manager.get_connection(playlist_name, platform=platform) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM songs")
                return cursor.fetchone()[0]

        except sqlite3.Error as e:
            logger.error(f"Error getting song count from {playlist_name}: {e}")
            return 0
