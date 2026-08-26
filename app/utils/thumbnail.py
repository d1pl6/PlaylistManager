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
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageTk

logger = logging.getLogger(__name__)


# Reused across fetches: keeps TCP/TLS connections alive between dialog
# opens instead of a fresh handshake per thumbnail.  requests.Session is
# thread-safe for concurrent get() calls (bounded by _fetch_semaphore).
_session = requests.Session()


class ThumbnailService:
    """Fetch, resize, and create PhotoImage objects from thumbnail URLs."""

    # Bounds app-wide thumbnail concurrency: the playlist picker spawns one
    # worker per entry, and a large library would otherwise open dozens of
    # parallel connections on every dialog open.
    _fetch_semaphore = threading.BoundedSemaphore(4)

    _cache_lock = threading.Lock()
    _cache: Dict[Tuple[str, Tuple[int, int]], Tuple[float, Image.Image]] = {}
    _CACHE_TTL_SECONDS = 600
    _CACHE_MAX_ENTRIES = 256

    @staticmethod
    def _cover_fit(img: Image.Image, size: Tuple[int, int]) -> Image.Image:
        """Scale *img* to cover *size* preserving aspect, then center-crop.

        A plain ``resize(size)`` stretches non-square sources - e.g. 16:9
        YouTube playlist covers - into the square display box.  Cover-fit
        leaves square sources untouched and crops the overflow from
        landscape/portrait ones instead.
        """
        target_w, target_h = size
        w, h = img.size
        if target_w <= 0 or target_h <= 0 or w <= 0 or h <= 0:
            return img
        scale = max(target_w / w, target_h / h)
        new_w = max(round(w * scale), target_w)
        new_h = max(round(h * scale), target_h)
        if (new_w, new_h) != (w, h):
            img = img.resize((new_w, new_h), Image.Resampling.BICUBIC)
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        return img.crop((left, top, left + target_w, top + target_h))

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
        """Download and cover-fit a thumbnail into a plain PIL image.

        Thread-safe - no Tk objects are created here.  Returns *None*
        on any failure so callers don't need to catch exceptions.

        Handles HTTP to HTTPS upgrade, network errors, and invalid image
        data.  The image is scaled to *size* preserving aspect ratio and
        center-cropped (see :meth:`_cover_fit`).
        """
        if not thumb_url:
            return None

        if thumb_url.lower().startswith("http://"):
            thumb_url = "https://" + thumb_url[7:]
        if not thumb_url.lower().startswith("https://"):
            logger.warning("Rejected non-HTTPS thumbnail URL: %s", thumb_url)
            return None

        key = (thumb_url, size)
        with ThumbnailService._cache_lock:
            hit = ThumbnailService._cache.get(key)
            if hit is not None:
                ts, cached = hit
                if time.monotonic() - ts < ThumbnailService._CACHE_TTL_SECONDS:
                    return cached.copy()
                del ThumbnailService._cache[key]

        with ThumbnailService._fetch_semaphore:
            try:
                resp = _session.get(thumb_url, timeout=10)
                resp.raise_for_status()
                img = ThumbnailService._cover_fit(
                    Image.open(io.BytesIO(resp.content)), size
                )
            except requests.RequestException as e:
                logger.error("Network error fetching thumbnail from %s: %s", thumb_url, e)
                return None
            except Exception as e:
                logger.error("Failed to fetch thumbnail from %s: %s", thumb_url, e)
                return None

        with ThumbnailService._cache_lock:
            now = time.monotonic()
            # Drop expired entries first so a long session of distinct
            # thumbnails can't accumulate stale images that are never
            # accessed again (they were only evicted on a future hit).
            expired = [
                k
                for k, (ts, _) in ThumbnailService._cache.items()
                if now - ts >= ThumbnailService._CACHE_TTL_SECONDS
            ]
            for k in expired:
                del ThumbnailService._cache[k]
            if len(ThumbnailService._cache) >= ThumbnailService._CACHE_MAX_ENTRIES:
                oldest = min(
                    ThumbnailService._cache,
                    key=lambda k: ThumbnailService._cache[k][0],
                )
                del ThumbnailService._cache[oldest]
            ThumbnailService._cache[key] = (now, img.copy())
        return img

    @staticmethod
    def fetch_full_image(thumb_url: Optional[str]) -> Optional[Image.Image]:
        """Download the original image without resizing.

        Returns a PIL Image or None on failure. Thread-safe; do not call
        Tk methods from worker threads.
        """
        if not thumb_url:
            return None

        if thumb_url.lower().startswith("http://"):
            thumb_url = "https://" + thumb_url[7:]
        if not thumb_url.lower().startswith("https://"):
            logger.warning("Rejected non-HTTPS thumbnail URL: %s", thumb_url)
            return None

        key = (thumb_url, None)
        with ThumbnailService._cache_lock:
            hit = ThumbnailService._cache.get(key)
            if hit is not None:
                ts, cached = hit
                if time.monotonic() - ts < ThumbnailService._CACHE_TTL_SECONDS:
                    return cached.copy()
                del ThumbnailService._cache[key]

        with ThumbnailService._fetch_semaphore:
            try:
                resp = _session.get(thumb_url, timeout=10)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
            except requests.RequestException as e:
                logger.error("Network error fetching full thumbnail from %s: %s", thumb_url, e)
                return None
            except Exception as e:
                logger.error("Failed to fetch full thumbnail from %s: %s", thumb_url, e)
                return None

        with ThumbnailService._cache_lock:
            now = time.monotonic()
            expired = [
                k
                for k, (ts, _) in ThumbnailService._cache.items()
                if now - ts >= ThumbnailService._CACHE_TTL_SECONDS
            ]
            for k in expired:
                del ThumbnailService._cache[k]
            if len(ThumbnailService._cache) >= ThumbnailService._CACHE_MAX_ENTRIES:
                oldest = min(
                    ThumbnailService._cache,
                    key=lambda k: ThumbnailService._cache[k][0],
                )
                del ThumbnailService._cache[oldest]
            ThumbnailService._cache[key] = (now, img.copy())
        return img

    @staticmethod
    def clear_cache_for(thumb_url: Optional[str], size: Optional[Tuple[int, int]] = None) -> None:
        """Remove a specific cached entry (url, size).

        If *size* is None, removes the full-image entry keyed by (url, None).
        """
        if not thumb_url:
            return
        key = (thumb_url, size)
        with ThumbnailService._cache_lock:
            ThumbnailService._cache.pop(key, None)

    @staticmethod
    def to_photoimage(img: Image.Image) -> ImageTk.PhotoImage:
        """Wrap a PIL image in a Tk ``PhotoImage``.

        Tkinter is not thread-safe - call this **only from the main
        (tkinter) thread**, after handing the image over from a worker
        thread via ``root.after(0, ...)``.
        """
        return ImageTk.PhotoImage(img)
