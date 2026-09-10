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

Thumbnail modes (``[thumbnails] mode`` in settings.ini, see
``utils.config.THUMBNAIL_MODES``):

- ``off``: live fetch every run, in-memory cache only (the historical
  behaviour; also the default).
- ``download``: every fetched image is persisted under
  ``CACHE_ROOT/`` and served from disk on later runs (complete offline
  library after an import; see :meth:`warm_song_cache`).
- ``dedupe``: song thumbs are keyed by song identity (title + artists +
  duration) through the duplicate-check matcher, so the same song in
  several playlists or platforms downloads exactly one image.
- ``cache``: covers plus only the song thumbs currently visible in the
  showcase are kept; :meth:`prune_song_cache` deletes the rest after
  each showcase refresh.
- ``max``: no thumbnails at all - every fetch returns None and nothing
  is written.  ``--data-saver`` forces this for the run.
"""

import hashlib
import io
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import platformdirs
import requests
from PIL import Image, ImageTk

from utils.config import THUMBNAIL_MODES, get_setting_value
from utils.logging_config import network_log

logger = logging.getLogger(__name__)

# Set by the App when launched with --data-saver: forces the effective
# mode to "max" for the whole run, overriding the persisted setting.
DATA_SAVER = False

# On-disk thumbnail cache root (per-OS via platformdirs; NOT profile-aware
# - a thumbnail is the same bytes whichever profile fetched it).  Tests
# redirect these module attributes to a temp tree.
CACHE_ROOT = Path(platformdirs.user_cache_dir("playlistmanager"))
_PLAYLIST_DIR = CACHE_ROOT / "playlists"
_SONG_DIR = CACHE_ROOT / "songs"
_FULL_DIR = CACHE_ROOT / "full"
_DEDUPE_INDEX = _SONG_DIR / "index.json"

# The size the showcase renders song thumbs at (keep in sync with
# ShowcaseManager._fetch_song_thumb / prune calls).
SONG_THUMB_SIZE = (40, 40)

# Reused across fetches: keeps TCP/TLS connections alive between dialog
# opens instead of a fresh handshake per thumbnail.  requests.Session is
# thread-safe for concurrent get() calls (bounded by _fetch_semaphore).
_session = requests.Session()


def _normalize_url(thumb_url: str) -> Optional[str]:
    """Upgrade http->https and reject anything that isn't https."""
    url = thumb_url.strip()
    if url.lower().startswith("http://"):
        url = "https://" + url[7:]
    if not url.lower().startswith("https://"):
        logger.warning("Rejected non-HTTPS thumbnail URL: %s", url)
        return None
    return url


def _normalize_text(value: str) -> str:
    """Normalise a title/artist field for identity comparison."""
    return str(value or "").strip().casefold()


class ThumbnailService:
    """Fetch, resize, and create PhotoImage objects from thumbnail URLs."""

    # Bounds app-wide thumbnail concurrency: the playlist picker spawns one
    # worker per entry, and a large library would otherwise open dozens of
    # parallel connections on every dialog open.
    _fetch_semaphore = threading.BoundedSemaphore(4)

    _cache_lock = threading.Lock()
    _cache: Dict[Tuple[str, Optional[Tuple[int, int]]], Tuple[float, Image.Image]] = {}
    _CACHE_TTL_SECONDS = 600
    _CACHE_MAX_ENTRIES = 256

    # Serializes dedupe index read-modify-write (concurrent song renders).
    _dedupe_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------

    @staticmethod
    def _mode() -> str:
        """Effective thumbnail mode for this run.

        ``--data-saver`` (``DATA_SAVER``) always wins with ``max``; the
        persisted ``[thumbnails] mode`` setting is authoritative otherwise.
        Unknown values fall back to ``off``.
        """
        if DATA_SAVER:
            return "max"
        try:
            mode = get_setting_value("thumbnails", "mode", "off")
        except Exception:
            return "off"
        return mode if mode in THUMBNAIL_MODES else "off"

    # ------------------------------------------------------------------
    # In-memory cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_get(key) -> Optional[Image.Image]:
        with ThumbnailService._cache_lock:
            hit = ThumbnailService._cache.get(key)
            if hit is None:
                return None
            ts, cached = hit
            if time.monotonic() - ts < ThumbnailService._CACHE_TTL_SECONDS:
                return cached.copy()
            del ThumbnailService._cache[key]
            return None

    @staticmethod
    def _cache_put(key, img: Image.Image) -> None:
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

    # ------------------------------------------------------------------
    # Disk cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _disk_key(url: str, size: Optional[Tuple[int, int]]) -> str:
        """Deterministic filename component for a (url, size) entry."""
        if size is None:
            return hashlib.md5(url.encode("utf-8")).hexdigest()
        return hashlib.md5(
            f"{url}|{size[0]}x{size[1]}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _disk_load(path: Path) -> Optional[Image.Image]:
        """Load a cached image; a corrupt file is treated as a miss."""
        try:
            with Image.open(path) as img:
                return img.copy()
        except Exception as e:
            logger.debug("Disk cache miss/corrupt %s: %s", path, e)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    @staticmethod
    def _disk_store(path: Path, img: Image.Image) -> None:
        """Write an image atomically (temp + rename), best effort."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            img.save(tmp, format="PNG")
            os.replace(tmp, path)
        except OSError as e:
            logger.debug("Failed to store disk cache %s: %s", path, e)

    # ------------------------------------------------------------------
    # Dedupe index (song identity -> image, in "dedupe" mode)
    # ------------------------------------------------------------------

    @staticmethod
    def _song_identity(song: dict) -> str:
        """Canonical identity used for dedupe filenames (not matching)."""
        title = _normalize_text(song.get("title"))
        artists = sorted(
            _normalize_text(a) for a in (song.get("artists") or [])
        )
        duration = int(song.get("duration") or 0)
        return f"{title}|{','.join(artists)}|{duration}"

    @staticmethod
    def _song_filename(song: dict) -> str:
        return hashlib.md5(
            ThumbnailService._song_identity(song).encode("utf-8")
        ).hexdigest() + ".png"

    @staticmethod
    def _load_dedupe_index() -> list:
        try:
            with open(_DEDUPE_INDEX, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    @staticmethod
    def _save_dedupe_index(rows: list) -> None:
        try:
            _SONG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _DEDUPE_INDEX.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f)
            os.replace(tmp, _DEDUPE_INDEX)
        except OSError as e:
            logger.debug("Failed to write dedupe index: %s", e)

    @staticmethod
    def _find_dedupe_match(song: dict) -> Optional[dict]:
        """Return the index row matching *song*, or None.

        Matching reuses the add-path policy from ``services.duplicate_check``
        (shared artist + title ratio + duration tolerance/drift band) so the
        thumbnail identity and the duplicate checker can never disagree.
        With ``[duplicate_check] is_true`` off, matching degrades to an
        exact-title variant by using a 1.0 threshold.
        """
        from services.duplicate_check import find_similar, read_settings

        rows = ThumbnailService._load_dedupe_index()
        if not rows:
            return None
        enabled, threshold, tolerance = read_settings()
        if not enabled:
            threshold = 1.0  # exact normalized title + artist gate
        match = find_similar(
            rows,
            song.get("title"),
            song.get("artists"),
            song.get("duration"),
            threshold=threshold,
            duration_tolerance=tolerance,
        )
        return match

    # ------------------------------------------------------------------
    # Shared HTTP fetch
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch(
        thumb_url: str, size: Optional[Tuple[int, int]]
    ) -> Optional[Image.Image]:
        """Semaphore-bounded GET; cover-fits to *size* (None = full image)."""
        with ThumbnailService._fetch_semaphore:
            try:
                resp = _session.get(thumb_url, timeout=10)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content))
                if size is None:
                    img = img.convert("RGBA")
                else:
                    img = ThumbnailService._cover_fit(img, size)
            except requests.RequestException as e:
                network_log(
                    logger, "Network error fetching thumbnail from %s: %s",
                    thumb_url, e,
                )
                return None
            except Exception as e:
                network_log(
                    logger, "Failed to fetch thumbnail from %s: %s",
                    thumb_url, e,
                )
                return None
        return img

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        """Download and cover-fit a playlist/generic thumbnail.

        Thread-safe - no Tk objects are created here.  Returns *None*
        on any failure so callers don't need to catch exceptions.

        Handles HTTP to HTTPS upgrade, network errors, and invalid image
        data.  The image is scaled to *size* preserving aspect ratio and
        center-cropped (see :meth:`_cover_fit`).  In ``off`` mode the
        in-memory cache is used as before; ``download``/``dedupe``/
        ``cache`` also persist to disk; ``max`` returns None immediately.
        """
        if not thumb_url:
            return None
        normalized = _normalize_url(thumb_url)
        if normalized is None:
            return None
        mode = ThumbnailService._mode()
        if mode == "max":
            return None

        key = (normalized, size)
        cached = ThumbnailService._cache_get(key)
        if cached is not None:
            return cached

        disk_path = _PLAYLIST_DIR / (
            ThumbnailService._disk_key(normalized, size) + ".png"
        )
        if mode != "off":
            img = ThumbnailService._disk_load(disk_path)
            if img is not None:
                return img

        img = ThumbnailService._fetch(normalized, size)
        if img is None:
            return None
        if mode != "off":
            ThumbnailService._disk_store(disk_path, img)
        ThumbnailService._cache_put(key, img)
        return img

    @staticmethod
    def fetch_song_image(
        song: dict,
        size: Tuple[int, int] = SONG_THUMB_SIZE,
    ) -> Optional[Image.Image]:
        """Download a song thumbnail honouring the thumbnail mode.

        ``song`` uses the SongManager row shape (``title``, ``artists``,
        ``duration``, ``thumbnail_url``).  In ``dedupe`` mode the disk
        cache is keyed by song identity via the duplicate-check matcher,
        so the same song in several playlists or profiles reuses one
        image even when the URLs differ.
        """
        if not isinstance(song, dict):
            return None
        raw_url = song.get("thumbnail_url") or song.get("thumbnail") or ""
        if not raw_url:
            return None
        normalized = _normalize_url(raw_url)
        if normalized is None:
            return None
        mode = ThumbnailService._mode()
        if mode == "max":
            return None

        key = (normalized, size)
        cached = ThumbnailService._cache_get(key)
        if cached is not None:
            return cached

        if mode != "off":
            if mode == "dedupe":
                img = None
                match = ThumbnailService._find_dedupe_match(song)
                if match is not None and match.get("file"):
                    img = ThumbnailService._disk_load(_SONG_DIR / match["file"])
            else:
                img = ThumbnailService._disk_load(
                    _SONG_DIR / (ThumbnailService._disk_key(normalized, size) + ".png")
                )
            if img is not None:
                ThumbnailService._cache_put(key, img)
                return img

        img = ThumbnailService._fetch(normalized, size)
        if img is None:
            return None
        if mode != "off":
            if mode == "dedupe":
                ThumbnailService._dedupe_store(song, img)
            else:
                ThumbnailService._disk_store(
                    _SONG_DIR / (ThumbnailService._disk_key(normalized, size) + ".png"),
                    img,
                )
        ThumbnailService._cache_put(key, img)
        return img

    @staticmethod
    def _dedupe_store(song: dict, img: Image.Image) -> None:
        """Persist a song image under its identity key + index row."""
        fname = ThumbnailService._song_filename(song)
        ThumbnailService._disk_store(_SONG_DIR / fname, img)
        with ThumbnailService._dedupe_lock:
            rows = ThumbnailService._load_dedupe_index()
            # A concurrent render may already have stored this song - the
            # deterministic filename turns the race into a benign no-op.
            if any(r.get("file") == fname for r in rows):
                return
            rows.append(
                {
                    "title": song.get("title", ""),
                    "artists": list(song.get("artists") or []),
                    "duration": int(song.get("duration") or 0),
                    "url": song.get("thumbnail_url") or song.get("thumbnail") or "",
                    "file": fname,
                }
            )
            ThumbnailService._save_dedupe_index(rows)

    @staticmethod
    def fetch_full_image(thumb_url: Optional[str]) -> Optional[Image.Image]:
        """Download the original image without resizing.

        Returns a PIL Image or None on failure. Thread-safe; do not call
        Tk methods from worker threads.
        """
        if not thumb_url:
            return None
        normalized = _normalize_url(thumb_url)
        if normalized is None:
            return None
        if ThumbnailService._mode() == "max":
            return None

        key = (normalized, None)
        cached = ThumbnailService._cache_get(key)
        if cached is not None:
            return cached

        disk_path = _FULL_DIR / (ThumbnailService._disk_key(normalized, None) + ".png")
        if ThumbnailService._mode() != "off":
            img = ThumbnailService._disk_load(disk_path)
            if img is not None:
                return img

        img = ThumbnailService._fetch(normalized, None)
        if img is None:
            return None
        if ThumbnailService._mode() != "off":
            ThumbnailService._disk_store(disk_path, img)
        ThumbnailService._cache_put(key, img)
        return img

    @staticmethod
    def clear_cache_for(
        thumb_url: Optional[str], size: Optional[Tuple[int, int]] = None
    ) -> None:
        """Remove a specific cached entry (url, size) - memory and disk.

        If *size* is None, removes the full-image entry keyed by (url, None).
        """
        if not thumb_url:
            return
        key = (thumb_url, size)
        with ThumbnailService._cache_lock:
            ThumbnailService._cache.pop(key, None)
        normalized = _normalize_url(thumb_url)
        if normalized is None:
            return
        try:
            if size is None:
                candidates = (_FULL_DIR,)
            else:
                # Covers live under playlists/, song thumbs under songs/ -
                # a size-ful clear must check both.
                candidates = (_PLAYLIST_DIR, _SONG_DIR)
            for subdir in candidates:
                (subdir / (ThumbnailService._disk_key(normalized, size) + ".png")).unlink(
                    missing_ok=True
                )
        except OSError as e:
            logger.debug("Failed to clear disk cache for %s: %s", thumb_url, e)

    # ------------------------------------------------------------------
    # Data-saver support (download prefetch, cache-mode prune, clear)
    # ------------------------------------------------------------------

    @staticmethod
    def warm_song_cache(
        playlist_name: str, platform: str, playlist_id: str = ""
    ) -> None:
        """Prefetch every song thumb of a playlist to disk.

        Only acts in ``download`` mode (the complete-offline-library
        option).  Runs in a daemon thread; failures are logged per song.
        """
        if ThumbnailService._mode() != "download":
            return

        def work() -> None:
            try:
                from services.song_manager import SongManager

                songs = SongManager().get_all_songs(
                    playlist_name, platform=platform, playlist_id=playlist_id
                )
                ThumbnailService.prefetch_song_thumbs(songs)
            except Exception as e:
                logger.debug(
                    "Song thumbnail warm-up failed for '%s': %s", playlist_name, e
                )

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def prefetch_song_thumbs(songs: List[dict]) -> None:
        """Download every song thumb to disk (``download`` mode only)."""
        if ThumbnailService._mode() != "download":
            return
        for song in songs:
            try:
                ThumbnailService.fetch_song_image(song, SONG_THUMB_SIZE)
            except Exception as e:
                logger.debug(
                    "Prefetch failed for %r: %s", song.get("title"), e
                )

    @staticmethod
    def prune_song_cache(
        active_urls, size: Tuple[int, int] = SONG_THUMB_SIZE
    ) -> None:
        """Delete cached song thumbs not among *active_urls*.

        Only acts in ``cache`` mode (the bounded, visible-only option);
        other modes leave the disk cache untouched.  Best-effort, safe
        from any thread.  Dedupe index rows referencing pruned files are
        dropped too.
        """
        if ThumbnailService._mode() != "cache":
            return
        try:
            if not _SONG_DIR.exists():
                return
            active = {
                ThumbnailService._disk_key(u, size) + ".png"
                for u in active_urls
                if u
            }
            for p in _SONG_DIR.glob("*.png"):
                if p.name not in active:
                    p.unlink(missing_ok=True)
        except OSError as e:
            logger.debug("Thumbnail cache prune failed: %s", e)
            return
        with ThumbnailService._dedupe_lock:
            rows = ThumbnailService._load_dedupe_index()
            kept = [r for r in rows if (_SONG_DIR / r.get("file", "")).exists()]
            if len(kept) != len(rows):
                ThumbnailService._save_dedupe_index(kept)

    @staticmethod
    def clear_disk_cache() -> None:
        """Wipe the on-disk thumbnail cache entirely."""
        with ThumbnailService._dedupe_lock:
            for d in (_PLAYLIST_DIR, _SONG_DIR, _FULL_DIR):
                try:
                    if d.exists():
                        shutil.rmtree(d, ignore_errors=True)
                except OSError as e:
                    logger.debug("Failed to clear disk cache %s: %s", d, e)

    @staticmethod
    def disk_cache_size() -> int:
        """Total bytes used by the on-disk thumbnail cache."""
        total = 0
        for d in (_PLAYLIST_DIR, _SONG_DIR, _FULL_DIR):
            try:
                total += sum(
                    p.stat().st_size for p in d.glob("**/*") if p.is_file()
                )
            except OSError:
                continue
        return total

    @staticmethod
    def to_photoimage(img: Image.Image) -> ImageTk.PhotoImage:
        """Wrap a PIL image in a Tk ``PhotoImage``.

        Tkinter is not thread-safe - call this **only from the main
        (tkinter) thread**, after handing the image over from a worker
        thread via ``root.after(0, ...)``.
        """
        return ImageTk.PhotoImage(img)