import io
import logging
from typing import Dict, List, Optional

import requests
from PIL import Image, ImageTk

from integrations.music_youtube.music_youtube import youtube_auth
from integrations.music_spotify.music_spotify import spotify_auth

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

    @staticmethod
    def get_smallest_thumbnail(thumbnails: Optional[List[Dict]]) -> Optional[str]:
        if not thumbnails:
            return None
        return min(
            thumbnails, key=lambda t: t.get("width") or t.get("height", 0)
        ).get("url")

    @staticmethod
    def fetch_thumbnail(thumb_url: Optional[str], size=(40, 40)) -> Optional[object]:
        if not thumb_url:
            return None
        if thumb_url.lower().startswith("http://"):
            thumb_url = "https://" + thumb_url[7:]
        if not thumb_url.lower().startswith("https://"):
            logger.warning(f"Rejected non-HTTPS thumbnail URL: {thumb_url}")
            return None
        try:
            resp = requests.get(thumb_url, timeout=10)
            img = Image.open(io.BytesIO(resp.content)).resize(size)
            return ImageTk.PhotoImage(img)
        except Exception as e:
            logger.error(f"Failed to fetch thumbnail: {e}")
            return None


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
    id = "youtube_music"
    display_name = "YouTube Music"

    def __init__(self):
        self._auth = youtube_auth
        self.yt_client = None

    def authenticate(self) -> bool:
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
            return self.yt_client.get_library_playlists()
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

    def get_song(self, video_id: str) -> dict:
        if not self.yt_client:
            return {}
        return self.yt_client.get_song(video_id)

    def get_playlist_id(self, name: str) -> Optional[str]:
        if not self.yt_client:
            return None
        try:
            playlists = self.yt_client.get_library_playlists()
            for playlist in playlists:
                if playlist.get("title") == name:
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


class SpotifyIntegration(BaseIntegration):
    id = "spotify"
    display_name = "Spotify"

    def __init__(self):
        self._auth = spotify_auth
        self.spotify_api = None

    def authenticate(self) -> bool:
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
            return [
                {
                    "title": p.get("name", "Unknown"),
                    "playlistId": p.get("id", ""),
                    "thumbnail": p.get("images", [{}])[0].get("url") if p.get("images") else None,
                    "trackCount": p.get("tracks", {}).get("total", 0),
                }
                for p in raw
            ]
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
