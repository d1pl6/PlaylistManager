import io
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageTk, UnidentifiedImageError

logger = logging.getLogger(__name__)


class PlaylistService:
    def __init__(self, yt_client: Optional[Any]) -> None:
        self.yt = yt_client

    def get_library_playlists(self) -> List[Dict[str, Any]]:
        if self.yt is None:
            return []
        try:
            return self.yt.get_library_playlists()
        except AttributeError as e:
            logger.error(f"YouTube client missing get_library_playlists method: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to get library playlists: {e}")
            return []

    def get_playlist_id(self, name: str) -> Optional[str]:
        if self.yt is None:
            return None
        try:
            playlists = self.yt.get_library_playlists()
            for playlist in playlists:
                if playlist.get("title") == name:
                    return playlist.get("playlistId")
            return None
        except AttributeError as e:
            logger.error(f"YouTube client missing get_library_playlists method: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to get playlist ID: {e}")
            return None

    def get_playlist_details(self, playlist_id: str, limit: int = 1) -> Dict[str, Any]:
        if self.yt is None:
            return {}
        try:
            return self.yt.get_playlist(playlist_id, limit=limit)
        except AttributeError as e:
            logger.error(f"YouTube client missing get_playlist method: {e}")
            return {}
        except Exception as e:
            logger.error(f"Failed to get playlist details: {e}")
            return {}

    @staticmethod
    def get_smallest_thumbnail(thumbnails: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        """Get the smallest thumbnail by area (width × height)."""
        if not thumbnails:
            return None
        try:
            def thumbnail_area(t: Dict[str, Any]) -> int:
                width = t.get("width", 0) or 0
                height = t.get("height", 0) or 0
                return width * height
            
            smallest = min(thumbnails, key=thumbnail_area)
            return smallest.get("url")
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Failed to select smallest thumbnail: {e}")
            return None

    @staticmethod
    def fetch_thumbnail(thumb_url: Optional[str], size: Tuple[int, int] = (64, 64)) -> Optional[ImageTk.PhotoImage]:
        """Fetch and resize a thumbnail from URL. Returns PhotoImage or None on failure."""
        if not thumb_url:
            return None
        try:
            resp = requests.get(thumb_url, timeout=10)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).resize(size)
            return ImageTk.PhotoImage(img)
        except requests.RequestException as e:
            logger.error(f"Network error fetching thumbnail from {thumb_url}: {e}")
            return None
        except UnidentifiedImageError as e:
            logger.error(f"Invalid image format from {thumb_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to fetch thumbnail from {thumb_url}: {e}")
            return None
