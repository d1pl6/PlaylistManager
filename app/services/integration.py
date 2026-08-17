"""
Integration registry and platform-specific integration wrappers.

Concrete integration classes receive their auth manager as a constructor
argument rather than importing it at module level, so this file has zero
import-time side effects and does not pull in optional dependencies.
"""

import logging
from typing import Dict, List, Optional

from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC

logger = logging.getLogger(__name__)


class BaseIntegration:
    id: str = ""
    display_name: str = ""

    def is_authenticated(self) -> bool:
        raise NotImplementedError

    def authenticate(self) -> bool:
        raise NotImplementedError

    def refresh_auth(self) -> bool:
        raise NotImplementedError

    def get_library_playlists(self) -> list:
        raise NotImplementedError

    def get_playlist_details(self, playlist_id: str, limit: int = 1) -> dict:
        raise NotImplementedError

    def get_playlist_id(self, name: str) -> Optional[str]:
        """Look up a playlist's platform ID by name.

        Returns *None* when the playlist cannot be found or the
        platform does not support name-based lookups.
        """
        return None

    def get_playlist_tracks(self, playlist_id: str) -> list:
        """Fetch all tracks for a playlist.

        Returns an empty list when the playlist doesn't exist or the
        platform returns an error.
        """
        return []

    def remove_track(self, playlist_id: str, track_id: str) -> bool:
        """Remove one track from a platform playlist.

        Returns True only when the platform confirmed the removal.
        The default returns False - implementations must override.
        """
        return False


class IntegrationRegistry:
    def __init__(self):
        self._integrations: Dict[str, BaseIntegration] = {}

    def register(self, integration: BaseIntegration):
        self._integrations[integration.id] = integration

    def get_all(self) -> Dict[str, BaseIntegration]:
        return dict(self._integrations)

    def get_active(self) -> Dict[str, BaseIntegration]:
        return {
            k: v for k, v in self._integrations.items() if v.is_authenticated()
        }

    def get(self, integration_id: str) -> Optional[BaseIntegration]:
        return self._integrations.get(integration_id)


class YouTubeMusicIntegration(BaseIntegration):
    id = PLATFORM_YOUTUBE_MUSIC
    display_name = "YouTube Music"

    def __init__(self, auth_manager=None):
        self._auth = auth_manager
        self.yt_client = None

    def authenticate(self) -> bool:
        if self._auth is None:
            return False
        if self._auth.setup_auth():
            self.yt_client = self._auth.get_yt_music()
            return True
        return False

    def is_authenticated(self) -> bool:
        return self.yt_client is not None

    def refresh_auth(self) -> bool:
        self.yt_client = None
        return self.authenticate()

    def get_library_playlists(self) -> list:
        if not self.yt_client:
            return []
        try:
            # Filter to playlists the user can actually add songs to.
            # ytmusicapi's parse marks each item with "owned" (the library
            # browse can surface followed/saved playlists the user is not a
            # collaborator on - adding to them fails on the platform side).
            # Items without the flag are kept defensively so a parser change
            # can never hide an owned playlist.
            return [
                p
                for p in self.yt_client.get_library_playlists()
                if p.get("owned") is not False
            ]
        except Exception as e:
            logger.error(f"YouTube Music: failed to get library playlists: {e}")
            return []

    def get_playlist_details(self, playlist_id: str, limit: int = 1) -> dict:
        if not self.yt_client:
            return {}
        try:
            return self.yt_client.get_playlist(playlist_id, limit=limit)
        except Exception as e:
            logger.error(f"YouTube Music: failed to get playlist details: {e}")
            return {}

    def get_playlist_id(self, name: str) -> Optional[str]:
        if not self.yt_client:
            return None
        try:
            playlists = self.yt_client.get_library_playlists()
            for playlist in playlists:
                if playlist.get("owned") is not False and playlist.get("title") == name:
                    return playlist.get("playlistId")
            return None
        except Exception as e:
            logger.error(f"YouTube Music: failed to get playlist ID: {e}")
            return None

    def get_playlist_tracks(self, playlist_id: str) -> list:
        if not self.yt_client:
            return []
        try:
            result = self.yt_client.get_playlist(playlist_id, limit=None)
            return result.get("tracks", [])
        except Exception as e:
            logger.error(f"YouTube Music: failed to get playlist tracks: {e}")
            return []

    def remove_track(self, playlist_id: str, track_id: str) -> bool:
        if not self.yt_client:
            return False
        try:
            # ytmusicapi's remove_playlist_items requires BOTH videoId and
            # setVideoId per item - setVideoId is the playlist-scoped id the
            # edit endpoint needs (only present when the playlist is
            # editable).  Fetch the playlist, locate the track and pass its
            # full item through; a track that is gone or lacks setVideoId
            # (non-owned playlist) is reported, not raised.
            playlist = self.yt_client.get_playlist(playlist_id, limit=None)
            tracks = playlist.get("tracks", [])
            target = next(
                (
                    t
                    for t in tracks
                    if t.get("videoId") == track_id and t.get("setVideoId")
                ),
                None,
            )
            if target is None:
                logger.warning(
                    "YouTube Music: track %s is not removable from playlist %s "
                    "(not in the playlist, or the playlist is not editable)",
                    track_id, playlist_id,
                )
                return False
            self.yt_client.remove_playlist_items(playlist_id, [target])
            logger.info(
                "YouTube Music: removed track %s from playlist %s",
                track_id, playlist_id,
            )
            return True
        except Exception as e:
            logger.error(
                f"YouTube Music: failed to remove track {track_id}: {e}"
            )
            return False


class SpotifyIntegration(BaseIntegration):
    id = PLATFORM_SPOTIFY
    display_name = "Spotify"

    def __init__(self, auth_manager=None):
        self._auth = auth_manager
        self.spotify_api = None

    def authenticate(self) -> bool:
        if self._auth is None:
            return False
        if self._auth.setup_auth():
            self.spotify_api = self._auth.get_api()
            return True
        return False

    def is_authenticated(self) -> bool:
        return self.spotify_api is not None

    def refresh_auth(self) -> bool:
        self.spotify_api = None
        return self.authenticate()

    def get_library_playlists(self) -> list:
        if not self.spotify_api:
            return []
        try:
            raw = self.spotify_api.get_playlists()
            # /me/playlists includes playlists the user merely *follows* -
            # those reject add-song calls ("not owner or collaborator") and
            # only clutter the picker.  Keep owned + collaborative entries.
            owner_id = self.spotify_api.get_user_id()
            out = []
            for p in raw:
                if (
                    owner_id
                    and p.get("owner", {}).get("id") != owner_id
                    and not p.get("collaborative")
                ):
                    continue
                out.append(
                    {
                        "title": p.get("name", "Unknown"),
                        "playlistId": p.get("id", ""),
                        "thumbnail": p.get("images", [{}])[0].get("url")
                        if p.get("images")
                        else None,
                        "trackCount": p.get("tracks", {}).get("total", 0),
                        "followerCount": p.get("followers", {}).get("total", 0),
                    }
                )
            return out
        except Exception as e:
            logger.error(f"Spotify: failed to get library playlists: {e}")
            return []

    def get_playlist_details(self, playlist_id: str, limit: int = 1) -> dict:
        if not self.spotify_api:
            return {}
        try:
            data = self.spotify_api.get_playlist(playlist_id)
            if not data:
                return {}
            return {
                "thumbnails": data.get("images", []),
                "title": data.get("name", ""),
                "trackCount": data.get("tracks", {}).get("total", 0),
                "owner_id": (data.get("owner") or {}).get("id", ""),
                "collaborative": bool(data.get("collaborative")),
                "followerCount": (data.get("followers") or {}).get("total", 0),
            }
        except Exception as e:
            logger.error(f"Spotify: failed to get playlist details: {e}")
            return {}

    def get_playlist_tracks(self, playlist_id: str) -> list:
        if not self.spotify_api:
            return []
        try:
            return self.spotify_api.get_playlist_tracks(playlist_id)
        except Exception as e:
            logger.error(f"Spotify: failed to get playlist tracks: {e}")
            return []

    def get_currently_playing(self) -> Optional[Dict]:
        if not self.spotify_api:
            return None
        return self.spotify_api.get_currently_playing()

    def get_playlist_id(self, name: str) -> Optional[str]:
        """Look up a Spotify playlist ID by name."""
        if not self.spotify_api:
            return None
        return self.spotify_api.get_playlist_id_by_name(name)

    def get_playlist_id_by_name(self, name: str) -> Optional[str]:
        """Alias for :meth:`get_playlist_id` - retained for backward compat."""
        return self.get_playlist_id(name)

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: List[str]) -> bool:
        if not self.spotify_api:
            return False
        return self.spotify_api.add_tracks_to_playlist(playlist_id, track_ids)

    def remove_track(self, playlist_id: str, track_id: str) -> bool:
        if not self.spotify_api:
            return False
        return self.spotify_api.remove_track_from_playlist(playlist_id, track_id)
