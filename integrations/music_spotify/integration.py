"""
Spotify integration.

The platform identifier is the plain string ``"spotify"``, matching this
plugin's plugin.json ``id`` - plugin.json is the single declaration; the
class attribute mirrors it for IntegrationRegistry keying.
"""

import logging
from typing import Dict, List, Optional

from services.integration import BaseIntegration

logger = logging.getLogger(__name__)


class SpotifyIntegration(BaseIntegration):
    id = "spotify"
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
            if owner_id is None:
                logger.warning(
                    "Could not determine Spotify user id; non-owned "
                    "playlists will appear in the picker"
                )
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
