import io
import logging

import requests
from PIL import Image, ImageTk

logger = logging.getLogger(__name__)


class PlaylistService:
    def __init__(self, yt_client):
        self.yt = yt_client

    def get_library_playlists(self):
        if self.yt is None:
            return []
        try:
            return self.yt.get_library_playlists()
        except Exception as e:
            logger.error(f"Failed to get library playlists: {e}")
            return []

    def get_playlist_id(self, name: str):
        if self.yt is None:
            return None
        try:
            playlists = self.yt.get_library_playlists()
            for playlist in playlists:
                if playlist.get("title") == name:
                    return playlist.get("playlistId")
            return None
        except Exception as e:
            logger.error(f"Failed to get playlist ID: {e}")
            return None

    def get_playlist_details(self, playlist_id, limit=1):
        if self.yt is None:
            return {}
        try:
            return self.yt.get_playlist(playlist_id, limit=limit)
        except Exception as e:
            logger.error(f"Failed to get playlist details: {e}")
            return {}

    @staticmethod
    def get_smallest_thumbnail(thumbnails):
        if not thumbnails:
            return None
        return min(
            thumbnails, key=lambda t: t.get("width") or t.get("height", 0)
        ).get("url")

    @staticmethod
    def fetch_thumbnail(thumb_url, size=(64, 64)):
        if not thumb_url:
            return None
        try:
            resp = requests.get(thumb_url, timeout=10)
            img = Image.open(io.BytesIO(resp.content)).resize(size)
            return ImageTk.PhotoImage(img)
        except Exception as e:
            logger.error(f"Failed to fetch thumbnail: {e}")
            return None
