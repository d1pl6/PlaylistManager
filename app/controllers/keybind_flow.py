from __future__ import annotations
import logging
import re
from typing import TYPE_CHECKING, Callable, Dict, Optional
from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC
from services.song_manager import SongManager

if TYPE_CHECKING:
    from ytmusicapi import YTMusic
    from integrations.music_youtube.music_youtube_receiver import URLReceiverManager
    from services.integration import SpotifyIntegration

logger = logging.getLogger(__name__)


class KeybindFlowController:
    """
    Orchestrates the complete keybind workflow:
    1. Start Flask receiver server
    2. Wait for and receive URL
    3. Validate URL
    4. Fetch song details via ytmusicapi
    5. Check if song exists in database
    6. Add to playlist if new
    """

    def __init__(
        self,
        yt_music_api: YTMusic,
        song_manager: SongManager,
        url_receiver: URLReceiverManager,
    ):
        """
        Initialize the keybind flow controller.

        Args:
            yt_music_api: Authenticated YTMusic instance
            song_manager: SongManager instance
            url_receiver: URLReceiverManager instance
        """
        self.yt_music = yt_music_api
        self.song_manager = song_manager
        self.url_receiver = url_receiver
        self._playlist_id_cache: Dict[str, str] = {}

    def _invalidate_playlist_cache(self) -> None:
        """Clear the playlist ID cache (e.g. after re-auth)."""
        self._playlist_id_cache.clear()

    def execute_flow(
        self,
        playlist_name: str,
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
        on_success: Callable[[Dict], None],
    ) -> None:
        """
        Execute the complete keybind workflow.

        Args:
            playlist_name: Name of the playlist to add song to
            on_status: Callback for status updates
            on_error: Callback for errors
            on_success: Callback for success with result dict
        """
        try:
            # Start Flask server
            on_status("Starting")
            self._start_server()

            # Wait for URL
            on_status("Waiting")
            self.url_receiver.set_waiting(True)
            url = self._get_url_from_receiver()

            # Validate URL
            on_status("Valid")
            from integrations.music_youtube.music_youtube_receiver import URLReceiverManager as _RM
            video_id = _RM._extract_video_id(url)
            if video_id is None:
                raise ValueError("Failed to extract video ID from URL")

            # Fetch song details
            on_status("Fetch")
            song_data = self._fetch_song_details(video_id)
            logger.debug(f"Song data: {song_data}")

            # Check if song exists
            on_status("Check")
            logger.debug(f"Checking if {video_id} exists in {playlist_name}")
            exists = self.song_manager.song_exists(
                playlist_name, video_id, platform=PLATFORM_YOUTUBE_MUSIC
            )
            logger.debug(f"Song exists: {exists}")
            if exists:
                on_success(
                    {
                        "status": "exists",
                        "song": song_data,
                        "message": f"'{song_data.get('title', 'Unknown')}' already in playlist",
                    }
                )
                return

            # Add to YouTube Music playlist first (platform API)
            on_status("Sync")
            playlist_id = self._get_playlist_id(playlist_name)
            logger.debug(f"YouTube Music playlist ID: {playlist_id}")
            if playlist_id is None:
                raise RuntimeError(
                    f"Could not find YouTube Music playlist '{playlist_name}'"
                )
            self.yt_music.add_playlist_items(playlist_id, [video_id])
            logger.info(f"Added {video_id} to YouTube Music playlist {playlist_id}")

            # Add to local database (so platform failure doesn't
            # leave us with a stale local entry that requires manual cleanup)
            on_status("Add")
            logger.debug("Adding song to local database")
            song_id = self.song_manager.add_song(
                playlist_name,
                song_data["title"],
                song_data["artists"],
                song_data["duration"],
                video_id,
                song_data.get("thumbnail"),
                platform=PLATFORM_YOUTUBE_MUSIC,
            )
            logger.debug(f"Added to local DB with ID: {song_id}")

            on_success(
                {
                    "status": "added",
                    "song": song_data,
                    "song_id": song_id,
                    "message": f"Added '{song_data.get('title', 'Unknown')}'",
                }
            )

        except TimeoutError as e:
            on_error(f"Timeout: {str(e)}")
        except ValueError as e:
            on_error(f"Validation: {str(e)}")
        except Exception as e:
            logger.error(f"Keybind flow error: {e}", exc_info=True)
            on_error(f"Error: {str(e)}")
        finally:
            self._cleanup()

    def _start_server(self) -> None:
        """Start the Flask URL receiver server."""
        try:
            if not self.url_receiver.is_running():
                self.url_receiver.start()
                logger.debug("URL receiver server started")
        except Exception as e:
            logger.error(f"Failed to start URL receiver: {e}")
            raise

    def _get_url_from_receiver(self, timeout: int = 30) -> str:
        """
        Get URL from the receiver queue with timeout.

        Args:
            timeout: Timeout in seconds

        Returns:
            The received URL

        Raises:
            TimeoutError: If no URL received within timeout
        """
        url = self.url_receiver.get_received_url(timeout=timeout)
        logger.debug(f"Received URL from receiver")
        return url

    def _fetch_song_details(self, video_id: str) -> Dict:
        """
        Fetch song details using ytmusicapi.

        Artist resolution priority:
          1. get_song_related() - structured artist data from the related response
          2. videoDetails.author split on common separators (e.g. " - Topic")
          3. subtitle from related[0] contents
          4. channel name / "Unknown Artist"

        Args:
            video_id: YouTube video ID

        Returns:
            Song data dictionary with keys: title, artists, duration, thumbnail

        Raises:
            Exception: If song details cannot be fetched
        """
        try:
            logger.debug(f"Fetching song details for video ID: {video_id}")

            # Use YTMusic's get_song API to fetch details
            song_info = self.yt_music.get_song(video_id)

            if not song_info:
                raise ValueError(f"Song not found for video ID: {video_id}")

            video_details = song_info.get("videoDetails", {})
            title = video_details.get("title", "Unknown")

            artists = self._resolve_artists(video_id, song_info, video_details)

            duration = video_details.get("lengthSeconds", 0)
            if isinstance(duration, str):
                duration = int(duration)

            thumbnails = (
                video_details.get("thumbnail", {})
                .get("thumbnails", [])
            )
            thumbnail_url = None
            if thumbnails:
                thumbnail_url = max(
                    thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0)
                ).get("url")

            song_data = {
                "title": title,
                "artists": artists,
                "duration": duration,
                "thumbnail": thumbnail_url,
                "video_id": video_id,
            }

            logger.info(f"Fetched song: {title} by {', '.join(artists)}")
            return song_data

        except Exception as e:
            logger.error(f"Error fetching song details: {e}")
            raise

    def _resolve_artists(
        self, video_id: str, song_info: Dict, video_details: Dict
    ) -> list[str]:
        """Resolve artist names using multiple sources."""
        # Priority 1: structured artist data from get_song_related
        artists = self._artists_from_song_related(video_id)
        if artists:
            return artists

        # Priority 2: videoDetails.author - may include channel suffix
        author = video_details.get("author", "")
        if author:
            cleaned = _strip_channel_suffix(author)
            if cleaned:
                return [cleaned]

        # Priority 3: subtitle from related contents
        related = song_info.get("related", [])
        if related:
            subtitle = related[0].get("subtitle", "")
            if subtitle:
                parsed = [a.strip() for a in subtitle.split(",") if a.strip()]
                if parsed:
                    return parsed

        # Priority 4: channel name fallback
        channel_id = video_details.get("channelId")
        if channel_id and author:
            return [_strip_channel_suffix(author) or author]
        if author:
            return [author]

        return ["Unknown Artist"]

    def _artists_from_song_related(self, video_id: str) -> list[str]:
        """
        Attempt to extract artist names from get_song_related response.

        The response may contain sections keyed by type (e.g. 'artist',
        'song', 'video'). Look for 'artist' entries carrying a 'name' field.
        """
        try:
            related = self.yt_music.get_song_related(video_id)
            if not isinstance(related, dict):
                return []

            artists = []
            # The response may have an 'artist' key with artist cards
            for entry in related.get("artist", []):
                if isinstance(entry, dict):
                    name = entry.get("name") or entry.get("title")
                    if name:
                        artists.append(name)
            return artists
        except Exception:
            logger.debug("get_song_related artist extraction failed", exc_info=True)
            return []


    def _get_playlist_id(self, playlist_name: str) -> Optional[str]:
        """
        Get YouTube Music playlist ID by name, with caching.

        The cache avoids a network call on every keybind press since
        playlist IDs rarely change within a session. The cache is
        invalidated on re-auth via _invalidate_playlist_cache().

        Args:
            playlist_name: Name of the playlist

        Returns:
            Playlist ID or None if not found
        """
        cached = self._playlist_id_cache.get(playlist_name)
        if cached is not None:
            return cached

        try:
            playlists = self.yt_music.get_library_playlists()
            for playlist in playlists:
                if playlist.get("title") == playlist_name:
                    pid = playlist.get("playlistId")
                    if pid:
                        self._playlist_id_cache[playlist_name] = pid
                    return pid
            return None
        except Exception as e:
            logger.error(f"Failed to get playlist ID for '{playlist_name}': {e}")
            return None

    def _cleanup(self) -> None:
        """Clean up resources."""
        try:
            if self.url_receiver.is_running():
                self.url_receiver.stop()
                logger.debug("URL receiver stopped")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")


def _strip_channel_suffix(name: str) -> str:
    """Remove common YouTube Music channel suffixes from an artist name.

    Handles patterns like "Taylor Swift - Topic", "Artist Name - Topic",
    "Various Artists - Topic" etc.
    """

    # Strip " - Topic", " - Topic", "– Topic" and similar variants
    cleaned = re.sub(r"\s*[-–-]\s*Topic\s*$", "", name, flags=re.IGNORECASE).strip()
    return cleaned


class SpotifyFlowController:
    def __init__(
        self,
        spotify_integration: SpotifyIntegration,
        song_manager: SongManager,
    ):
        self.spotify_integration = spotify_integration
        self.song_manager = song_manager
        self._playlist_id_cache: Dict[str, str] = {}

    def _invalidate_playlist_cache(self) -> None:
        """Clear the playlist ID cache (e.g. after re-auth)."""
        self._playlist_id_cache.clear()

    def _get_playlist_id(self, playlist_name: str) -> Optional[str]:
        """Get Spotify playlist ID by name, with caching."""
        cached = self._playlist_id_cache.get(playlist_name)
        if cached is not None:
            return cached

        try:
            pid = self.spotify_integration.get_playlist_id_by_name(playlist_name)
            if pid:
                self._playlist_id_cache[playlist_name] = pid
            return pid
        except Exception as e:
            logger.error(f"Failed to get Spotify playlist ID for '{playlist_name}': {e}")
            return None

    def execute_flow(
        self,
        playlist_name: str,
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
        on_success: Callable[[Dict], None],
    ) -> None:
        try:
            on_status("Fetch")
            playing = self.spotify_integration.get_currently_playing()
            if not playing:
                on_error("Nothing playing")
                return

            title = playing["title"]
            artists = playing["artists"]
            duration = playing["duration_ms"] // 1000
            track_id = playing["track_id"]
            thumbnail = playing.get("thumbnail")

            song_data = {
                "title": title,
                "artists": artists,
                "duration": duration,
                "track_id": track_id,
                "thumbnail": thumbnail,
            }

            on_status("Check")
            if self.song_manager.song_exists_by_info(
                playlist_name, title, artists, duration, platform=PLATFORM_SPOTIFY
            ):
                on_success({
                    "status": "exists",
                    "song": song_data,
                    "message": f"'{title}' already in playlist",
                })
                return

            # Add to Spotify playlist first (platform API)
            on_status("Sync")
            playlist_id = self._get_playlist_id(playlist_name)
            if playlist_id is None:
                raise RuntimeError(
                    f"Could not find Spotify playlist '{playlist_name}'"
                )
            ok = self.spotify_integration.add_tracks_to_playlist(
                playlist_id, [track_id]
            )
            if not ok:
                raise RuntimeError(f"Spotify rejected adding '{title}'")
            logger.info(f"Added {track_id} to Spotify playlist {playlist_id}")

            # Add to local database (platform failure won't
            # leave a stale local entry behind)
            on_status("Add")
            song_id = self.song_manager.add_song_by_info(
                playlist_name,
                title,
                artists,
                duration,
                track_id,
                thumbnail,
                platform=PLATFORM_SPOTIFY,
            )

            on_success({
                "status": "added",
                "song": song_data,
                "song_id": song_id,
                "message": f"Added '{title}'",
            })

        except Exception as e:
            logger.error(f"Spotify flow error: {e}", exc_info=True)
            on_error(f"Error: {str(e)}")
