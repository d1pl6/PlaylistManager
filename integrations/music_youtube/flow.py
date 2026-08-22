"""
YouTube Music add-song flow (keybind workflow).

Moved from app/controllers/keybind_flow.py in 0.3.0 - this is the
plugin's implementation of the "extension" flow type: the browser
extension posts the current tab URL to a local receiver server, and
this flow turns it into an added song.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Callable, Dict, Optional

from services.integration import BaseFlowController
from services.playlist_store import PlaylistStore, playlist_still_registered
from services.song_manager import SongManager
from utils.logging_config import trace_log

from .music_youtube_receiver import extract_video_id

if TYPE_CHECKING:
    from .integration import YouTubeMusicIntegration

logger = logging.getLogger(__name__)

# Mirrors plugin.json "id" - flows key local-DB writes by it.
PLATFORM = "youtube_music"


class YouTubeMusicFlow(BaseFlowController):
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
        youtube_integration: YouTubeMusicIntegration,
        song_manager: SongManager,
        url_receiver: URLReceiverManager,
    ):
        """
        Initialize the flow controller.

        Args:
            youtube_integration: Authenticated YouTubeMusicIntegration (its
                ``yt_client`` is captured for this flow's lifetime; re-auth
                replaces the whole flow object).
            song_manager: SongManager instance
            url_receiver: URLReceiverManager instance
        """
        self.yt_music = youtube_integration.yt_client
        self.song_manager = song_manager
        self.url_receiver = url_receiver
        # keyed by (playlist_name, known_id) so two playlists that share a
        # name (different ids) cannot poison each other's cache entry
        self._playlist_id_cache: Dict[tuple, str] = {}

    def execute_flow(
        self,
        playlist_name: str,
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
        on_success: Callable[[Dict], None],
        url: Optional[str] = None,
        song_data: Optional[Dict] = None,
        playlist_id: Optional[str] = None,
    ) -> None:
        """
        Execute the complete keybind workflow.

        Args:
            playlist_name: Name of the playlist to add song to
            on_status: Callback for status updates
            on_error: Callback for errors
            on_success: Callback for success with result dict
            url: Pre-captured song URL. When given, the receiver server start
                and URL wait are skipped (used by the CLI batch mode, which
                captures one URL and reuses it for several playlists).
            song_data: Pre-fetched song details. When given, the ytmusicapi
                fetch is skipped (the CLI fetches once and shares the result).
            playlist_id: Known playlist ID from the store. When given, the
                by-name library scan is skipped (the ID is cached instead).
                Without it (legacy entries) the library is scanned once per
                playlist name.

        Note: the platform-first invariant is preserved in every mode - the song
        is added to the platform API before the local database, and a platform
        failure never leaves a "successful" local entry behind.
        """
        try:
            if url is None:
                # Start Flask server
                on_status("Starting")
                self._start_server()

                # Wait for URL
                on_status("Waiting")
                self.url_receiver.set_waiting(True)
                url = self._get_url_from_receiver()

            # Validate URL
            on_status("Validating")
            video_id = extract_video_id(url)
            if video_id is None:
                raise ValueError("Failed to extract video ID from URL")

            # Fetch song details (skipped when the caller already fetched them)
            if song_data is None:
                on_status("Fetch")
                song_data = self.fetch_song_details(video_id)
            trace_log(logger, "Song data: %s", song_data)

            # Local-DB key: the id the CALLER knew ("" for legacy entries
            # registered before playlist ids existed).  The platform id
            # resolved below can differ from it; the local DB must stay
            # keyed by the caller's id or reads and writes would split
            # across two files.
            local_playlist_id = playlist_id or ""

            # Check if song exists.  The store-liveness check guards the
            # local DB access: a playlist frame closed while this flow was
            # waiting/validating must not resurrect its deleted database.
            on_status("Check")
            if not playlist_still_registered(
                playlist_name, PLATFORM, local_playlist_id
            ):
                raise RuntimeError(
                    f"Playlist '{playlist_name}' was removed while the flow "
                    "was running"
                )
            logger.debug("Checking if %s exists in %s", video_id, playlist_name)
            exists = self.song_manager.song_exists(
                playlist_name,
                video_id,
                platform=PLATFORM,
                playlist_id=local_playlist_id,
            )
            logger.debug("Song exists: %s", exists)
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
            playlist_id = self._get_playlist_id(playlist_name, playlist_id)
            logger.debug("YouTube Music playlist ID: %s", playlist_id)
            if playlist_id is None:
                raise RuntimeError(
                    f"Could not find YouTube Music playlist '{playlist_name}'"
                )
            result = self.yt_music.add_playlist_items(playlist_id, [video_id])
            # ytmusicapi returns a plain status string in some versions and
            # {"status": ...} in others; a failed add (duplicate with
            # duplicates=False, unknown video) comes back as a non-SUCCEEDED
            # status instead of raising.  Surfacing it keeps the platform-first
            # invariant: the song must not be written to the local DB when the
            # platform rejected it.
            status = result.get("status") if isinstance(result, dict) else result
            if not status or "SUCCEEDED" not in str(status):
                raise RuntimeError(
                    f"YouTube Music rejected adding video '{video_id}' "
                    f"(status: {status!r})"
                )
            logger.info("Added %s to YouTube Music playlist %s", video_id, playlist_id)

            # Add to local database.  The platform add above succeeded
            # (or we raised), so we never write locally when the platform
            # rejected the song - the platform-first invariant.
            on_status("Add")
            if not playlist_still_registered(
                playlist_name, PLATFORM, local_playlist_id
            ):
                # The playlist was deleted while the platform add was in
                # flight.  The platform copy is authoritative and done;
                # write nothing locally and report success so the caller
                # does not show an error for an add that happened.
                logger.warning(
                    "Playlist '%s' removed mid-flow - platform add done, "
                    "local record skipped",
                    playlist_name,
                )
                on_success(
                    {
                        "status": "added",
                        "song": song_data,
                        "song_id": None,
                        "message": (
                            f"Added '{song_data.get('title', 'Unknown')}' "
                            "(playlist was removed)"
                        ),
                    }
                )
                return
            logger.debug("Adding song to local database")
            song_id = self.song_manager.add_song(
                playlist_name,
                song_data["title"],
                song_data["artists"],
                song_data["duration"],
                video_id,
                song_data.get("thumbnail"),
                platform=PLATFORM,
                playlist_id=local_playlist_id,
            )
            logger.debug("Added to local DB with ID: %s", song_id)

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
            logger.error("Keybind flow error: %s", e, exc_info=True)
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
            logger.error("Failed to start URL receiver: %s", e)
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
        logger.debug("Received URL from receiver")
        return url

    def capture(
        self, timeout: int = 30
    ) -> tuple[Optional[str], Optional[Dict], str]:
        """Capture the current song once - CLI batch mode helper.

        Starts the receiver, waits for the extension's URL, validates it
        and fetches song details.  Returns ``(url, song_data, error)``;
        on failure *url* and *song_data* are ``None`` and *error*
        describes what went wrong ("" on success).  The result feeds
        ``execute_flow(url=..., song_data=...)`` so one captured song can
        be added to several playlists.
        """
        url = None
        try:
            self._start_server()
            self.url_receiver.set_waiting(True)
            print(
                "Waiting for YouTube Music URL... "
                "(play the song in the browser with the extension installed)",
                flush=True,
            )
            url = self._get_url_from_receiver(timeout=timeout)
        except TimeoutError:
            return None, None, "Timeout: no URL received from the browser extension"
        except Exception as e:
            logger.error(
                "Failed to capture the YouTube Music URL: %s", e, exc_info=True
            )
            return None, None, f"failed to start the URL receiver: {e}"
        finally:
            try:
                self.url_receiver.set_waiting(False)
            except Exception:
                pass
            self._cleanup()

        video_id = extract_video_id(url)
        if video_id is None:
            logger.error("Could not extract a video ID from '%s'", url)
            return None, None, "could not extract a video ID from the received URL"
        try:
            song_data = self.fetch_song_details(video_id)
        except Exception as e:
            logger.error("Failed to fetch song details: %s", e, exc_info=True)
            return None, None, f"failed to fetch song details: {e}"
        return url, song_data, ""

    def fetch_song_details(self, video_id: str) -> Dict:
        """
        Fetch song details using ytmusicapi.

        Public so the CLI can fetch once and pass the result to several
        playlists via ``execute_flow(..., song_data=...)``.

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
            logger.debug("Fetching song details for video ID: %s", video_id)

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

            logger.info("Fetched song: %s by %s", title, ", ".join(artists))
            return song_data

        except Exception as e:
            logger.error("Error fetching song details: %s", e)
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

    def _get_playlist_id(self, playlist_name: str, known_id: Optional[str] = None) -> Optional[str]:
        """
        Get YouTube Music playlist ID by name, with caching.

        The cache avoids a network call on every keybind press since
        playlist IDs rarely change within a session.  Re-auth replaces the
        whole flow object (and with it the cache), so no explicit
        invalidation is needed.

        Args:
            playlist_name: Name of the playlist
            known_id: Playlist ID already known by the caller (from the
                store, where it was persisted at add-playlist time).  When
                given, no platform round trip happens - the value is cached
                for the session.

        Returns:
            Playlist ID or None if not found
        """
        cache_key = (playlist_name, known_id or "")
        cached = self._playlist_id_cache.get(cache_key)
        if cached is not None:
            return cached

        if known_id:
            self._playlist_id_cache[cache_key] = known_id
            return known_id

        try:
            # limit=None scans the whole library: the patched
            # get_library_playlists defaults to the first page (~25),
            # which would silently fail for any playlist beyond it.
            playlists = self.yt_music.get_library_playlists(limit=None)
            for playlist in playlists:
                if playlist.get("title") == playlist_name:
                    pid = playlist.get("playlistId")
                    if pid:
                        self._playlist_id_cache[cache_key] = pid
                    return pid
            return None
        except Exception as e:
            logger.error("Failed to get playlist ID for '%s': %s", playlist_name, e)
            return None

    def _cleanup(self) -> None:
        """Clean up resources."""
        try:
            if self.url_receiver.is_running():
                self.url_receiver.stop()
                logger.debug("URL receiver stopped")
        except Exception as e:
            logger.error("Error during cleanup: %s", e)


def _strip_channel_suffix(name: str) -> str:
    """Remove common YouTube Music channel suffixes from an artist name.

    Handles patterns like "Taylor Swift - Topic", "Artist Name - Topic",
    "Various Artists - Topic" etc.
    """

    # Strip " - Topic", "- Topic" and similar variants
    cleaned = re.sub(r"\s*[-–-]\s*Topic\s*$", "", name, flags=re.IGNORECASE).strip()
    return cleaned
