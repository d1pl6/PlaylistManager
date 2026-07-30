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
        if platform == "spotify":
            return self.add_songs_bulk_spotify(playlist_name, tracks)
        return self._add_songs_bulk_youtube(playlist_name, tracks)

    def _add_songs_bulk_youtube(
        self,
        playlist_name: str,
        tracks: List[Dict],
    ) -> int:
        """
        Bulk-insert songs from ytmusicapi playlist tracks into the database.

        Args:
            playlist_name: Name of the playlist
            tracks: List of track dicts from ytmusicapi get_playlist()

        Returns:
            Number of songs actually inserted (skips duplicates)
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            rows = []
            for track in tracks:
                track_id = track.get("videoId")
                if not track_id:
                    continue

                title = track.get("title", "Unknown")
                artists = [
                    a.get("name", "Unknown") for a in track.get("artists", [])
                ]
                if not artists:
                    artists = ["Unknown Artist"]

                duration_raw = track.get("duration_seconds", 0) or track.get("duration", "0")
                if isinstance(duration_raw, str):
                    duration = self._parse_duration(duration_raw)
                else:
                    duration = int(duration_raw)

                thumbnails = track.get("thumbnails", [])
                thumbnail_url = None
                if thumbnails:
                    thumbnail_url = max(
                        thumbnails,
                        key=lambda t: t.get("width", 0) * t.get("height", 0),
                    ).get("url")

                artists_json = json.dumps(artists)
                rows.append((title, artists_json, duration, track_id, thumbnail_url))

            rows_before = len(rows)
            cursor.executemany(
                """
                INSERT OR IGNORE INTO songs (title, artists, duration, track_id, thumbnail_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()

            inserted = cursor.rowcount
            logger.info(
                f"Bulk insert into {playlist_name}: {inserted}/{rows_before} new songs"
            )
            return inserted

        except sqlite3.Error as e:
            logger.error(f"Failed bulk insert into {playlist_name}: {e}")
            raise
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.strip().lower()

    @staticmethod
    def _normalize_artists(artists: List[str]) -> str:
        normalized = sorted(a.strip().lower() for a in artists if a.strip())
        return json.dumps(normalized)

    def add_songs_bulk_spotify(
        self,
        playlist_name: str,
        tracks: List[Dict],
    ) -> int:
        """Bulk-insert Spotify tracks. Deduplication is handled by UNIQUE constraint on track_id."""
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            rows = []
            for track in tracks:
                track_id = track.get("id")
                if not track_id:
                    continue

                title = track.get("name", "Unknown")
                artists = [a.get("name", "Unknown") for a in track.get("artists", [])]
                if not artists:
                    artists = ["Unknown Artist"]

                duration_ms = track.get("duration_ms", 0)
                duration = duration_ms // 1000 if duration_ms else 0

                images = track.get("album", {}).get("images", [])
                thumbnail_url = images[0]["url"] if images else None

                artists_json = json.dumps(artists)
                rows.append((title, artists_json, duration, track_id, thumbnail_url))

            rows_before = len(rows)
            cursor.executemany(
                """
                INSERT OR IGNORE INTO songs (title, artists, duration, track_id, thumbnail_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()

            inserted = cursor.rowcount
            logger.info(
                f"Bulk insert (Spotify) into {playlist_name}: {inserted}/{rows_before} new songs"
            )
            return inserted

        except sqlite3.Error as e:
            logger.error(f"Failed bulk insert (Spotify) into {playlist_name}: {e}")
            raise
        finally:
            if conn:
                DatabaseManager.close_connection(conn)

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
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            artists_json = json.dumps(artists)

            cursor.execute(
                "SELECT id FROM songs WHERE LOWER(TRIM(title)) = ? AND artists = ? AND duration = ?",
                (self._normalize_text(title), self._normalize_artists(artists), duration),
            )
            if cursor.fetchone():
                raise sqlite3.IntegrityError("Song already exists by info match")

            cursor.execute(
                "INSERT INTO songs (title, artists, duration, track_id, thumbnail_url) VALUES (?, ?, ?, ?, ?)",
                (title, artists_json, duration, track_id, thumbnail_url),
            )
            conn.commit()
            song_id = cursor.lastrowid
            if song_id is None:
                raise RuntimeError("song_id is None after INSERT")
            logger.info(f"Added song (info match) to {playlist_name} (ID: {song_id})")
            return song_id

        except sqlite3.IntegrityError:
            logger.warning(f"Song already exists in {playlist_name}")
            raise
        except sqlite3.Error as e:
            logger.error(f"Failed to add song to {playlist_name}: {e}")
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

        Args:
            playlist_name: Name of the playlist
            title: Song title
            artists: List of artist names
            duration: Duration in seconds
            track_id: Platform track/video ID
            thumbnail_url: URL to song thumbnail

        Returns:
            Song ID if successful

        Raises:
            sqlite3.Error: If database operation fails
        """
        conn = None
        try:
            conn = self.db_manager.get_db_connection(playlist_name)
            cursor = conn.cursor()

            # Convert artists list to JSON string
            artists_json = json.dumps(artists)

            cursor.execute(
                """
                INSERT INTO songs (title, artists, duration, track_id, thumbnail_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (title, artists_json, duration, track_id, thumbnail_url),
            )

            conn.commit()
            song_id = cursor.lastrowid
            if song_id is None:
                raise RuntimeError("song_id is None after INSERT")
            logger.info(
                f"Added song {track_id} to playlist {playlist_name} (ID: {song_id})"
            )

            return song_id

        except sqlite3.IntegrityError:
            logger.warning(
                f"Song {track_id} already exists in playlist {playlist_name}"
            )
            raise
        except sqlite3.Error as e:
            logger.error(f"Failed to add song to {playlist_name}: {e}")
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
