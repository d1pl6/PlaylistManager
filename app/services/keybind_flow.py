import logging
from typing import Callable, Dict, Optional
from ytmusicapi import YTMusic
from services.song_manager import SongManager
from integrations.music_youtube.music_youtube_receiver import URLReceiverManager
from integrations.music_spotify.music_spotify import SpotifyAPI

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
            # Step 1: Start Flask server
            on_status("Starting")
            self._start_server()

            # Step 2: Wait for URL
            on_status("Waiting")
            url = self._get_url_from_receiver()

            # Step 3: Validate URL
            on_status("Valid")
            video_id = URLReceiverManager._extract_video_id(url)
            if not video_id:
                raise ValueError("Failed to extract video ID from URL")

            # Step 4: Fetch song details
            on_status("Fetch")
            song_data = self._fetch_song_details(video_id)
            logger.debug(f"Song data: {song_data}")

            # Step 5: Check if song exists
            on_status("Check")
            logger.debug(f"Checking if {video_id} exists in {playlist_name}")
            exists = self.song_manager.song_exists(playlist_name, video_id)
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

            # Step 6: Add to local database
            on_status("Add")
            logger.debug("Adding song to local database")
            song_id = self.song_manager.add_song(
                playlist_name,
                song_data["title"],
                song_data["artists"],
                song_data["duration"],
                video_id,
                song_data.get("thumbnail"),
            )
            logger.debug(f"Added to local DB with ID: {song_id}")

            # Step 7: Add to YouTube Music playlist
            on_status("Sync")
            playlist_id = self._get_playlist_id(playlist_name)
            logger.debug(f"YouTube Music playlist ID: {playlist_id}")
            if playlist_id:
                self.yt_music.add_playlist_items(playlist_id, [video_id])
                logger.info(f"Added {video_id} to YouTube Music playlist {playlist_id}")
            else:
                logger.warning(f"Could not find YouTube Music playlist '{playlist_name}'")

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
        try:
            url = self.url_receiver.get_received_url(timeout=timeout)
            logger.debug(f"Received URL from receiver")
            return url
        except TimeoutError:
            raise

    def _fetch_song_details(self, video_id: str) -> Dict:
        """
        Fetch song details using ytmusicapi.

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

            # Extract song details
            video_details = song_info.get("videoDetails", {})
            title = video_details.get("title", "Unknown")

            # Get artists from the song details
            artists = []

            # Try author from videoDetails (most reliable)
            author = video_details.get("author")
            if author:
                artists = [author]

            # Try subtitle from related contents if available
            if not artists:
                related = song_info.get("related", [])
                if related and len(related) > 0:
                    subtitle = related[0].get("subtitle", "")
                    if subtitle:
                        artists = [a.strip() for a in subtitle.split(",") if a.strip()]

            # Fallback to channel name if nothing else worked
            if not artists:
                channel_id = video_details.get("channelId")
                if channel_id:
                    artists = [video_details.get("author", "Unknown Artist")]
                else:
                    artists = ["Unknown Artist"]

            # Get duration
            duration = song_info.get("videoDetails", {}).get("lengthSeconds", 0)
            if isinstance(duration, str):
                duration = int(duration)

            # Get thumbnail
            thumbnails = (
                song_info.get("videoDetails", {})
                .get("thumbnail", {})
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

    def _get_playlist_id(self, playlist_name: str) -> Optional[str]:
        """
        Get YouTube Music playlist ID by name.

        Args:
            playlist_name: Name of the playlist

        Returns:
            Playlist ID or None if not found
        """
        try:
            playlists = self.yt_music.get_library_playlists()
            for playlist in playlists:
                if playlist.get("title") == playlist_name:
                    return playlist.get("playlistId")
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


class SpotifyFlowController:
    def __init__(
        self,
        spotify_api: SpotifyAPI,
        song_manager: SongManager,
    ):
        self.spotify_api = spotify_api
        self.song_manager = song_manager

    def execute_flow(
        self,
        playlist_name: str,
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
        on_success: Callable[[Dict], None],
    ) -> None:
        try:
            on_status("Fetch")
            playing = self.spotify_api.get_currently_playing()
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
            if self.song_manager.song_exists_by_info(playlist_name, title, artists, duration):
                on_success({
                    "status": "exists",
                    "song": song_data,
                    "message": f"'{title}' already in playlist",
                })
                return

            on_status("Add")
            song_id = self.song_manager.add_song_by_info(
                playlist_name, title, artists, duration, track_id, thumbnail
            )

            on_status("Sync")
            playlist_id = self.spotify_api.get_playlist_id_by_name(playlist_name)
            if playlist_id:
                self.spotify_api.add_tracks_to_playlist(playlist_id, [track_id])
                logger.info(f"Added {track_id} to Spotify playlist {playlist_id}")
            else:
                logger.warning(f"Could not find Spotify playlist '{playlist_name}'")

            on_success({
                "status": "added",
                "song": song_data,
                "song_id": song_id,
                "message": f"Added '{title}'",
            })

        except Exception as e:
            logger.error(f"Spotify flow error: {e}", exc_info=True)
            on_error(f"Error: {str(e)}")
