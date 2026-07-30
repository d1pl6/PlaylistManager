"""
Consolidated thumbnail service.

Replaces the three duplicate thumbnail implementations:
  - BaseIntegration.fetch_thumbnail / get_smallest_thumbnail
  - PlaylistService.fetch_thumbnail / get_smallest_thumbnail
  - MainWindow._set_playlist_cover (inline threading + download)

Only this module should import ``requests`` and ``PIL`` for thumbnail
work, keeping the integration and UI layers free of those dependencies.
"""

import io
import logging
from typing import Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageTk

logger = logging.getLogger(__name__)


class ThumbnailService:
    """Fetch, resize, and create PhotoImage objects from thumbnail URLs."""

    @staticmethod
    def get_smallest_thumbnail(thumbnails: Optional[List[Dict]]) -> Optional[str]:
        """Return the URL of the smallest thumbnail by area (w × h).

        Returns None when *thumbnails* is empty or None.
        """
        if not thumbnails:
            return None
        try:
            def _area(t: Dict) -> int:
                return (t.get("width", 0) or 0) * (t.get("height", 0) or 0)
            smallest = min(thumbnails, key=_area)
            return smallest.get("url")
        except (ValueError, KeyError, TypeError) as e:
            logger.error("Failed to select smallest thumbnail: %s", e)
            return None

    @staticmethod
    def fetch_thumbnail(
        thumb_url: Optional[str],
        size: Tuple[int, int] = (64, 64),
    ) -> Optional[ImageTk.PhotoImage]:
        """Download, resize and return a PhotoImage.

        Handles HTTP to HTTPS upgrade, network errors, and invalid image
        data.  Returns *None* on any failure so callers don't need to
        catch exceptions.
        """
        if not thumb_url:
            return None

        if thumb_url.lower().startswith("http://"):
            thumb_url = "https://" + thumb_url[7:]
        if not thumb_url.lower().startswith("https://"):
            logger.warning("Rejected non-HTTPS thumbnail URL: %s", thumb_url)
            return None

        try:
            resp = requests.get(thumb_url, timeout=10)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).resize(size)
            return ImageTk.PhotoImage(img)
        except requests.RequestException as e:
            logger.error("Network error fetching thumbnail from %s: %s", thumb_url, e)
            return None
        except Exception as e:
            logger.error("Failed to fetch thumbnail from %s: %s", thumb_url, e)
            return None
