"""
Consolidated thumbnail service.

Replaces the three duplicate thumbnail implementations:
  - BaseIntegration.fetch_thumbnail / get_smallest_thumbnail
  - PlaylistService.fetch_thumbnail / get_smallest_thumbnail
  - MainWindow._set_playlist_cover (inline threading + download)

Only this module should import ``requests`` and ``PIL`` for thumbnail
work, keeping the integration and UI layers free of those dependencies.

Threading rule: **tkinter is not thread-safe.**  :meth:`fetch_image`
downloads and resizes a plain PIL image and is safe to call from any
thread; the :class:`PIL.Image.Image` must then be handed to the main
thread (e.g. via ``root.after(0, ...)``) where :meth:`to_photoimage`
creates the Tk object.
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
    def from_data(data: dict) -> Optional[str]:
        """Extract a thumbnail URL from a playlist/song response dict.

        Accepts either a ``thumbnails`` list of ``{"url": ...}`` dicts
        (the smallest by area wins) or a bare ``thumbnail`` URL string.
        """
        if not isinstance(data, dict):
            return None
        thumbnails = data.get("thumbnails") or data.get("thumbnail")
        if isinstance(thumbnails, list):
            return ThumbnailService.get_smallest_thumbnail(thumbnails)
        if isinstance(thumbnails, str):
            return thumbnails
        return None

    @staticmethod
    def fetch_image(
        thumb_url: Optional[str],
        size: Tuple[int, int] = (64, 64),
    ) -> Optional[Image.Image]:
        """Download and resize a thumbnail into a plain PIL image.

        Thread-safe - no Tk objects are created here.  Returns *None*
        on any failure so callers don't need to catch exceptions.

        Handles HTTP to HTTPS upgrade, network errors, and invalid image
        data.
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
            return Image.open(io.BytesIO(resp.content)).resize(size)
        except requests.RequestException as e:
            logger.error("Network error fetching thumbnail from %s: %s", thumb_url, e)
            return None
        except Exception as e:
            logger.error("Failed to fetch thumbnail from %s: %s", thumb_url, e)
            return None

    @staticmethod
    def to_photoimage(img: Image.Image) -> ImageTk.PhotoImage:
        """Wrap a PIL image in a Tk ``PhotoImage``.

        Tkinter is not thread-safe - call this **only from the main
        (tkinter) thread**, after handing the image over from a worker
        thread via ``root.after(0, ...)``.
        """
        return ImageTk.PhotoImage(img)
