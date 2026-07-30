import sqlite3
import json
import logging
from typing import Dict, List, Optional
from services.database import DatabaseManager

logger = logging.getLogger(__name__)


class SongManager:
    """Manages song CRUD operations for playlists."""

    def __init__(self):
        self.db_manager = DatabaseManager()

    @staticmethod
    def _parse_duration(duration_str: str) -> int:
        """Parse a duration string like '3:45' or '1:02:30' to seconds."""
        parts = duration_str.strip().split(":")
        parts = [int(p) for p in parts]
        seconds = 0
        for p in parts:
            seconds = seconds * 60 + p
        return seconds

    def add_songs_bulk(
        self,
        playlist_name: str,
        tracks: List[Dict],
        platform: str = "youtube_music",
    ) -> int:
        """
        Bulk-insert songs into the database, dispatching by platform.

        Args:
            playlist_name: Name of the playlist
            tracks: List of track dicts from the platform API
            platform: Platform identifier ("youtube_music" or "spotify")

        Returns:
            Number of songs actually inserted (skips duplicates)
        """
        extractor = _TRACK_EXTRACTORS.get(platform)
        if extractor is None:
            logger.warning("Unknown platform '%s' for bulk insert, falling back to youtube_music", platform)
            extractor = _extract_youtube_track
        return self._add_songs_bulk(playlist_name, tracks, extractor, platform)

    def _add_songs_bulk(
        self,
        playlist_name: str,
        tracks: List[Dict],
        extractor,
        platform: str,
    ) -> int:
        """
        Core bulk insert — shared by YouTube Music and Spotify.

        Args:
            playlist_name: Name of the playlist
            tracks: List of track dicts
            extractor: Callable(track) -> (title, artists, duration, track_id, thumbnail_url) | None
            platform: Platform label for log messages

        Returns:
            Number of songs actually inserted (skips duplicates)
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
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
                "INSERT OR IGNORE INTO songs (title, artists, duration, track_id, thumbnail_url) VALUES (?, ?, ?, ?, ?)",
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
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.strip().lower()

    @staticmethod
    def _normalize_artists(artists: List[str]) -> str:
        """
        Produce a canonical JSON string for artist comparison.

        Assumption: artist order and casing are not semantically meaningful,
        so we sort and lowercase consistently.  This matches the same song
        regardless of how the API returns the artist list.
        """
        normalized = sorted(a.strip().lower() for a in artists if a.strip())
        return json.dumps(normalized)

    def song_exists_by_info(
        self, playlist_name: str, title: str, artists: List[str], duration: int
    ) -> bool:
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            norm_title = self._normalize_text(title)
            norm_artists = self._normalize_artists(artists)

            cursor.execute(
                "SELECT id FROM songs WHERE LOWER(TRIM(title)) = ? AND artists = ? AND duration = ?",
                (norm_title, norm_artists, duration),
            )
            return cursor.fetchone() is not None

        except sqlite3.Error as e:
            logger.error(f"Error checking song by info in {playlist_name}: {e}")
            return False
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    def add_song_by_info(
        self,
        playlist_name: str,
        title: str,
        artists: List[str],
        duration: int,
        track_id: str,
        thumbnail_url: Optional[str] = None,
    ) -> int:
        """
        Add a song by matching (title, artists, duration), using an atomic
        check-and-insert transaction to prevent TOCTOU races.

        Returns the new or existing song ID.

        Raises:
            sqlite3.Error: If database operation fails
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            artists_json = json.dumps(artists)
            norm_title = self._normalize_text(title)
            norm_artists = self._normalize_artists(artists)

            # Atomic transaction: check + insert to prevent TOCTOU
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "SELECT id FROM songs WHERE LOWER(TRIM(title)) = ? AND artists = ? AND duration = ?",
                    (norm_title, norm_artists, duration),
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
                    "INSERT INTO songs (title, artists, duration, track_id, thumbnail_url) VALUES (?, ?, ?, ?, ?)",
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
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    def add_song(
        self,
        playlist_name: str,
        title: str,
        artists: List[str],
        duration: int,
        track_id: str,
        thumbnail_url: Optional[str] = None,
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

        Returns:
            Song ID (new or existing)

        Raises:
            sqlite3.Error: If database operation fails
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            artists_json = json.dumps(artists)

            cursor.execute(
                "INSERT OR IGNORE INTO songs (title, artists, duration, track_id, thumbnail_url) VALUES (?, ?, ?, ?, ?)",
                (title, artists_json, duration, track_id, thumbnail_url),
            )
            conn.commit()

            if cursor.rowcount == 0:
                # Song already exists — look up existing ID
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
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    def song_exists(self, playlist_name: str, track_id: str) -> bool:
        """
        Check if a song exists in the playlist's database by track ID.

        Args:
            playlist_name: Name of the playlist
            track_id: Platform track/video ID

        Returns:
            True if song exists, False otherwise
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            cursor.execute("SELECT id FROM songs WHERE track_id = ?", (track_id,))
            result = cursor.fetchone()

            return result is not None

        except sqlite3.Error as e:
            logger.error(f"Error checking if song exists in {playlist_name}: {e}")
            return False
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    def get_song_by_track_id(self, playlist_name: str, track_id: str) -> Optional[Dict]:
        """
        Get song data by track ID.

        Args:
            playlist_name: Name of the playlist
            track_id: Platform track/video ID

        Returns:
            Song data as dict or None if not found
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM songs WHERE track_id = ?", (track_id,))
            row = cursor.fetchone()

            if row is None:
                return None

            # Convert row to dict and parse artists JSON
            song_dict = dict(row)
            song_dict["artists"] = json.loads(song_dict["artists"])
            return song_dict

        except sqlite3.Error as e:
            logger.error(f"Error getting song from {playlist_name}: {e}")
            return None
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    def delete_song(self, playlist_name: str, song_id: int) -> bool:
        """
        Delete a song from the playlist's database.

        Args:
            playlist_name: Name of the playlist
            song_id: Song ID

        Returns:
            True if deleted, False otherwise
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            cursor.execute("DELETE FROM songs WHERE id = ?", (song_id,))
            conn.commit()

            logger.info(f"Deleted song {song_id} from playlist {playlist_name}")
            return cursor.rowcount > 0

        except sqlite3.Error as e:
            logger.error(f"Error deleting song from {playlist_name}: {e}")
            return False
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    def get_all_songs(self, playlist_name: str) -> List[Dict]:
        """
        Get all songs from the playlist's database.

        Args:
            playlist_name: Name of the playlist

        Returns:
            List of song dictionaries
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM songs ORDER BY added_at DESC")
            rows = cursor.fetchall()

            # Convert rows to dicts and parse artists JSON
            songs = []
            for row in rows:
                song_dict = dict(row)
                song_dict["artists"] = json.loads(song_dict["artists"])
                songs.append(song_dict)

            return songs

        except sqlite3.Error as e:
            logger.error(f"Error getting songs from {playlist_name}: {e}")
            return []
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    def get_latest_song(self, playlist_name: str) -> Optional[Dict]:
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT title, artists, thumbnail_url FROM songs ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "title": row[0],
                "artists": json.loads(row[1]),
                "thumbnail_url": row[2],
            }
        except sqlite3.Error as e:
            logger.error(f"Error getting latest song from {playlist_name}: {e}")
            return None
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    def get_song_count(self, playlist_name: str) -> int:
        """
        Get the total number of songs in the playlist.

        Args:
            playlist_name: Name of the playlist

        Returns:
            Number of songs
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM songs")
            count = cursor.fetchone()[0]

            return count

        except sqlite3.Error as e:
            logger.error(f"Error getting song count from {playlist_name}: {e}")
            return 0
        finally:
            if conn:
                DatabaseManager.close_connection(conn)


# ── Platform track extractors for bulk insert ──────────────────────────────

def _extract_youtube_track(track: dict) -> tuple | None:
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
        duration = SongManager._parse_duration(duration_raw)
    else:
        duration = int(duration_raw)

    thumbnails = track.get("thumbnails", [])
    thumbnail_url = None
    if thumbnails:
        thumbnail_url = max(
            thumbnails,
            key=lambda t: t.get("width", 0) * t.get("height", 0),
        ).get("url")

    return (title, artists, duration, track_id, thumbnail_url)


def _extract_spotify_track(track: dict) -> tuple | None:
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
    thumbnail_url = images[0]["url"] if images else None

    return (title, artists, duration, track_id, thumbnail_url)


_TRACK_EXTRACTORS: dict[str, callable] = {
    "youtube_music": _extract_youtube_track,
    "spotify": _extract_spotify_track,
}
