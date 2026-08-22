"""
Spotify add-song flow (keybind workflow).

Moved from app/controllers/keybind_flow.py in 0.3.0 - this is the
plugin's implementation of the "api" flow type: the currently-playing
track is read straight from the Spotify API, no URL receiver involved.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Dict, Optional

from services.integration import BaseFlowController
from services.playlist_store import playlist_still_registered
from services.song_manager import SongManager

if TYPE_CHECKING:
    from .integration import SpotifyIntegration

logger = logging.getLogger(__name__)

# Mirrors plugin.json "id" - flows key local-DB writes by it.
PLATFORM = "spotify"


class SpotifyFlow(BaseFlowController):
    def __init__(
        self,
        spotify_integration: SpotifyIntegration,
        song_manager: SongManager,
    ):
        self.spotify_integration = spotify_integration
        self.song_manager = song_manager
        # keyed by (playlist_name, known_id) so two playlists that share a
        # name (different ids) cannot poison each other's cache entry
        self._playlist_id_cache: Dict[tuple, str] = {}

    def _get_playlist_id(self, playlist_name: str, known_id: Optional[str] = None) -> Optional[str]:
        """Get Spotify playlist ID by name, with caching."""
        cache_key = (playlist_name, known_id or "")
        cached = self._playlist_id_cache.get(cache_key)
        if cached is not None:
            return cached

        if known_id:
            self._playlist_id_cache[cache_key] = known_id
            return known_id

        try:
            pid = self.spotify_integration.get_playlist_id_by_name(playlist_name)
            if pid:
                self._playlist_id_cache[cache_key] = pid
            return pid
        except Exception as e:
            logger.error("Failed to get Spotify playlist ID for '%s': %s", playlist_name, e)
            return None

    def capture(
        self, timeout: int = 30
    ) -> tuple[Optional[str], Optional[Dict], str]:
        """Fetch the currently-playing track once - CLI batch mode helper.

        *timeout* is accepted for signature uniformity with extension-type
        flows (Spotify reads the platform directly - no receiver wait).
        Returns ``(url, song_data, error)``; *url* is always ``None`` (an
        api-type flow has no song URL) and *song_data* feeds
        ``execute_flow(song_data=...)`` for one shared capture across all
        target playlists.
        """
        try:
            playing = self.spotify_integration.get_currently_playing()
        except Exception as e:
            logger.error(
                "Failed to fetch Spotify currently-playing: %s", e, exc_info=True
            )
            return None, None, str(e)
        if not playing:
            return None, None, "Nothing playing on Spotify"
        song_data = {
            "title": playing["title"],
            "artists": playing["artists"],
            "duration": playing["duration_ms"] // 1000,
            "track_id": playing["track_id"],
            "thumbnail": playing.get("thumbnail"),
        }
        return None, song_data, ""

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
        """Execute the add-to-playlist flow for the currently playing track.

        When ``song_data`` is provided (CLI batch mode), the pre-fetched
        dict is reused across all target playlists so the same song is
        added to every playlist.  Without it the flow reads the
        currently-playing track from the Spotify API.  ``url`` is unused
        (accepted for caller uniformity).  ``playlist_id`` skips the
        by-name playlist lookup when the store already knows it.
        """
        try:
            on_status("Fetch")
            if song_data is None:
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
            else:
                title = song_data["title"]
                artists = song_data["artists"]
                duration = song_data["duration"]
                track_id = song_data["track_id"]
                thumbnail = song_data.get("thumbnail")

            on_status("Check")
            # Store-liveness first (see YouTubeMusicFlow): a closed
            # frame must not resurrect its deleted database.
            if not playlist_still_registered(
                playlist_name, PLATFORM, playlist_id
            ):
                raise RuntimeError(
                    f"Playlist '{playlist_name}' was removed while the flow "
                    "was running"
                )
            # Exact track_id check - NOT the info-match heuristic.  A
            # (title, artists, duration) match can be a different track with
            # identical metadata ("Intro" by the same artist), which would be
            # reported as already-present and never added to the playlist.
            # The track_id is stable for Spotify, so matching it is exact;
            # add_song_by_info keeps the info-match as its INSERT-side
            # fallback for metadata drift.
            if self.song_manager.song_exists(
                playlist_name,
                track_id,
                platform=PLATFORM,
                playlist_id=playlist_id or "",
            ):
                on_success({
                    "status": "exists",
                    "song": song_data,
                    "message": f"'{title}' already in playlist",
                })
                return

            # Add to Spotify playlist first (platform API)
            on_status("Sync")
            local_playlist_id = playlist_id or ""  # see YouTubeMusicFlow note
            playlist_id = self._get_playlist_id(playlist_name, playlist_id)
            if playlist_id is None:
                raise RuntimeError(
                    f"Could not find Spotify playlist '{playlist_name}'"
                )
            ok = self.spotify_integration.add_tracks_to_playlist(
                playlist_id, [track_id]
            )
            if not ok:
                raise RuntimeError(f"Spotify rejected adding '{title}'")
            logger.info("Added %s to Spotify playlist %s", track_id, playlist_id)

            # Add to local database (platform failure won't
            # leave a stale local entry behind)
            on_status("Add")
            if not playlist_still_registered(
                playlist_name, PLATFORM, local_playlist_id
            ):
                # Playlist deleted while the platform add was in flight -
                # the platform copy is done and authoritative; write
                # nothing locally (a fresh database would just orphan).
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
                        "message": f"Added '{title}' (playlist was removed)",
                    }
                )
                return
            song_id = self.song_manager.add_song_by_info(
                playlist_name,
                title,
                artists,
                duration,
                track_id,
                thumbnail,
                platform=PLATFORM,
                playlist_id=local_playlist_id,
            )

            on_success({
                "status": "added",
                "song": song_data,
                "song_id": song_id,
                "message": f"Added '{title}'",
            })

        except Exception as e:
            logger.error("Spotify flow error: %s", e, exc_info=True)
            on_error(f"Error: {str(e)}")
