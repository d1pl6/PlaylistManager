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

The mode decides *whether* images are kept; ``[thumbnails] max_size``
(``utils.config.THUMBNAIL_SIZE_CAPS``) decides *how large* they may be.
The cap is applied in :meth:`ThumbnailService._fit_within` on the single
fetch path every caller funnels through, so it bounds the decode working
set and the cached bytes alike, and it is part of both cache keys -
switching it takes effect on the next fetch instead of serving entries
written under the previous cap.
"""

import hashlib
import io
import json
import logging
import os
import queue
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

import platformdirs
import requests
from PIL import Image, ImageTk

from utils.config import THUMBNAIL_MODES, get_setting_value, resolve_thumbnail_size_cap
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

# Bounded daemon pool for EVERY background thumbnail/cover job.  A raw
# ``threading.Thread`` per thumbnail lets a large library pile up hundreds
# of blocked threads behind the 4-slot fetch semaphore - each with its own
# stack and TLS - so showcases, warm-up, pruning and the image viewer all
# queue onto this single fixed pool instead.  The pool is deliberately
# created lazily on the first submit: headless/``max``-mode runs never
# spawn its threads.
THUMB_POOL_SIZE = 8


class _ThumbPool:
    """Fixed-size daemon worker pool for thumbnail jobs.

    Jobs are plain zero-argument callables; worker threads are daemon so a
    quit never waits on queued or in-flight fetches.  Concurrency of actual
    network work is still bounded separately by the service's fetch
    semaphore (4), so the pool size only bounds thread count, not
    connections.
    """

    def __init__(self, max_workers: int) -> None:
        self._jobs: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._workers = [
            threading.Thread(target=self._run, daemon=True, name=f"thumb-pool-{i}")
            for i in range(max_workers)
        ]
        for w in self._workers:
            w.start()

    def _run(self) -> None:
        while True:
            fn = self._jobs.get()
            try:
                fn()
            except Exception:
                logger.debug("thumbnail pool job failed", exc_info=True)
            finally:
                self._jobs.task_done()

    def submit(self, fn: Callable[[], None]) -> None:
        """Queue *fn* for execution on the pool."""
        self._jobs.put(fn)


def _normalize_url(thumb_url: str) -> str | None:
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

    # Lazily-created shared worker pool (see _ThumbPool).  Created on the
    # first submit so thumbnails-less runs never spawn its threads.
    _pool: _ThumbPool | None = None
    _pool_lock = threading.Lock()

    @staticmethod
    def submit(fn: Callable[[], None]) -> None:
        """Run *fn* on the shared bounded thumbnail pool.

        Replaces the per-job ``threading.Thread(target=fn, daemon=True)``
        pattern - a large library would otherwise hold hundreds of blocked
        threads (stack + TLS each) behind the fetch semaphore.
        """
        with ThumbnailService._pool_lock:
            if ThumbnailService._pool is None:
                ThumbnailService._pool = _ThumbPool(THUMB_POOL_SIZE)
        ThumbnailService._pool.submit(fn)

    _cache_lock = threading.Lock()
    _cache: dict[tuple[str, tuple[int, int] | None, int], tuple[float, Image.Image]] = {}
    _CACHE_TTL_SECONDS = 600
    # Sized entries (covers / song thumbs, 40-64 px) share one budget;
    # full-res (url, None) images are multi-MB and get their own small
    # budget so a few image-viewer opens cannot crowd out every cover.
    _CACHE_MAX_ENTRIES = 256
    _CACHE_MAX_FULL = 8

    # Serializes dedupe index read-modify-write (concurrent song renders).
    _dedupe_lock = threading.Lock()

    # In-memory dedupe index + the file signature it was parsed from.
    # _save_dedupe_index refreshes both; any external change to the file
    # changes the signature and forces a re-read.  Only ever touched
    # while holding _dedupe_lock - the cached list object is shared with
    # callers, so it must not be mutated outside the lock.
    _dedupe_index_cache: list | None = None
    _dedupe_index_sig: tuple[int, int] | None = None

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

    @staticmethod
    def _cap() -> int:
        """Effective longest-edge pixel cap for this run (0 = no cap).

        Read per fetch like :meth:`_mode`, so a Settings change applies
        live.  Validation lives in ``config.resolve_thumbnail_size_cap``;
        an unreadable settings file degrades to no cap (never to a cap
        that would silently shrink the art).
        """
        try:
            return resolve_thumbnail_size_cap()
        except Exception:
            return 0

    @staticmethod
    def _fit_within(img: Image.Image, cap: int) -> Image.Image:
        """Downscale *img* so its longest edge is at most *cap* pixels.

        A no-op when *cap* is 0 (the default) or the image already fits -
        so an uncapped run produces byte-identical files to the previous
        behaviour and existing caches stay valid.  Aspect ratio is
        preserved and only the longest edge is compared, so a 16:9 cover
        is capped by its width.  Never upscales: a cap below the display
        size yields a smaller source, not a blurred enlargement.

        ``draft()`` first for JPEG sources (what every platform serves):
        it asks libjpeg for a 1/2, 1/4 or 1/8-scale decode, which is
        exactly the downscale we were going to throw away - without it
        PIL still materialises the full-resolution bitmap before
        :meth:`_cover_fit` shrinks it.  No-op for formats without DCT
        scaling (PNG, WebP), hence the cheap try/except.
        """
        if cap <= 0:
            return img
        if img.format == "JPEG":
            try:
                img.draft(None, (cap, cap))
            except Exception as e:
                logger.debug("JPEG draft() failed, decoding at full size: %s", e)
        w, h = img.size
        if w <= 0 or h <= 0 or max(w, h) <= cap:
            return img
        scale = cap / max(w, h)
        return img.resize(
            (max(1, round(w * scale)), max(1, round(h * scale))),
            Image.Resampling.LANCZOS,
        )

    # ------------------------------------------------------------------
    # In-memory cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_get(key) -> Image.Image | None:
        # The cached object is handed out directly (no defensive copy):
        # no caller mutates a fetch_* result - they all convert straight
        # to a Tk PhotoImage, and _open_image_window resizes only its own
        # img.copy().  The store-side copy in _cache_put keeps the cache
        # entry safe from whatever the caller does with its own object.
        with ThumbnailService._cache_lock:
            hit = ThumbnailService._cache.get(key)
            if hit is None:
                return None
            ts, cached = hit
            if time.monotonic() - ts < ThumbnailService._CACHE_TTL_SECONDS:
                return cached
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
            # Evict per kind: full-res entries compete only with each
            # other (_CACHE_MAX_FULL), never with the smaller covers.
            full = key[1] is None
            cap = (
                ThumbnailService._CACHE_MAX_FULL
                if full
                else ThumbnailService._CACHE_MAX_ENTRIES
            )
            same_kind = [
                k for k in ThumbnailService._cache if (k[1] is None) == full
            ]
            if len(same_kind) >= cap:
                oldest = min(
                    same_kind, key=lambda k: ThumbnailService._cache[k][0]
                )
                del ThumbnailService._cache[oldest]
            ThumbnailService._cache[key] = (now, img.copy())

    # ------------------------------------------------------------------
    # Disk cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _disk_key(url: str, size: tuple[int, int] | None) -> str:
        """Deterministic filename component for a (url, size) entry.

        The size cap is folded into the digest so entries written under a
        different cap are never served, and cache-mode pruning treats
        them as unreferenced (it builds its active set from this same
        helper).  Uncapped, the digest is byte-identical to the
        historical one, so existing caches keep working.
        """
        cap = ThumbnailService._cap()
        cap_part = f"|c{cap}" if cap else ""
        if size is None:
            return hashlib.md5(f"{url}{cap_part}".encode("utf-8")).hexdigest()
        return hashlib.md5(
            f"{url}|{size[0]}x{size[1]}{cap_part}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _mem_key(
        url: str, size: tuple[int, int] | None
    ) -> tuple[str, tuple[int, int] | None, int]:
        """In-memory cache key: (url, size, cap).

        *size* keeps its ``None``-means-full-image meaning at index 1
        (the eviction split in :meth:`_cache_put` reads ``key[1]``).
        Folding the cap in makes a Settings change apply on the next
        fetch instead of after the entry TTL - the disk key would
        already have moved on.
        """
        return (url, size, ThumbnailService._cap())

    @staticmethod
    def _disk_load(path: Path) -> Image.Image | None:
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
        """Dedupe index rows, cached until songs/index.json changes.

        Caller holds ``_dedupe_lock`` (the cached list object is shared
        with mutators such as ``_dedupe_store``).  The signature check
        keeps the cache correct across external file edits and tests
        that rewrite the file.
        """
        sig = None
        try:
            st = _DEDUPE_INDEX.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        if (
            ThumbnailService._dedupe_index_cache is not None
            and sig == ThumbnailService._dedupe_index_sig
        ):
            return ThumbnailService._dedupe_index_cache
        rows: list = []
        if sig is not None:
            try:
                with open(_DEDUPE_INDEX, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    rows = data
            except (OSError, ValueError):
                rows = []
        ThumbnailService._dedupe_index_cache = rows
        ThumbnailService._dedupe_index_sig = sig
        return rows

    @staticmethod
    def _save_dedupe_index(rows: list) -> None:
        """Persist the dedupe index and refresh the in-memory copy.

        Caller holds ``_dedupe_lock``.
        """
        try:
            _SONG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _DEDUPE_INDEX.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f)
            os.replace(tmp, _DEDUPE_INDEX)
        except OSError as e:
            logger.debug("Failed to write dedupe index: %s", e)
        ThumbnailService._dedupe_index_cache = rows
        try:
            st = _DEDUPE_INDEX.stat()
            ThumbnailService._dedupe_index_sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            ThumbnailService._dedupe_index_sig = None

    @staticmethod
    def _find_dedupe_match(song: dict) -> dict | None:
        """Return the index row matching *song*, or None.

        Matching reuses the add-path policy from ``services.duplicate_check``
        (shared artist + title ratio + duration tolerance/drift band) so the
        thumbnail identity and the duplicate checker can never disagree.
        With ``[duplicate_check] is_true`` off, matching degrades to an
        exact-title variant by using a 1.0 threshold.
        """
        from services.duplicate_check import find_similar, read_settings

        enabled, threshold, tolerance = read_settings()
        if not enabled:
            threshold = 1.0  # exact normalized title + artist gate
        with ThumbnailService._dedupe_lock:
            rows = ThumbnailService._load_dedupe_index()
            if not rows:
                return None
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
        thumb_url: str, size: tuple[int, int] | None
    ) -> Image.Image | None:
        """Semaphore-bounded GET; cover-fits to *size* (None = full image).

        The one funnel every fetch goes through, so the size cap applies
        uniformly - to covers, to song thumbs and to the image viewer's
        originals alike.
        """
        with ThumbnailService._fetch_semaphore:
            try:
                resp = _session.get(thumb_url, timeout=10)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content))
                # Cap the decoded source *before* anything else touches it:
                # PIL loads the full bitmap on first access, so this is
                # the last point where the cap can bound the working set.
                img = ThumbnailService._fit_within(img, ThumbnailService._cap())
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
    def _cover_fit(img: Image.Image, size: tuple[int, int]) -> Image.Image:
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
    def get_smallest_thumbnail(thumbnails: list[dict] | None) -> str | None:
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
    def from_data(data: dict) -> str | None:
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
        thumb_url: str | None,
        size: tuple[int, int] = (64, 64),
    ) -> Image.Image | None:
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

        key = ThumbnailService._mem_key(normalized, size)
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
        size: tuple[int, int] = SONG_THUMB_SIZE,
    ) -> Image.Image | None:
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

        key = ThumbnailService._mem_key(normalized, size)
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
    def fetch_full_image(thumb_url: str | None) -> Image.Image | None:
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

        key = ThumbnailService._mem_key(normalized, None)
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
        thumb_url: str | None, size: tuple[int, int] | None = None
    ) -> None:
        """Remove a specific cached entry (url, size) - memory and disk.

        If *size* is None, removes the full-image entry keyed by (url, None).

        The entry is the one for the cap that is in effect right now (both
        keys are cap-scoped); entries left behind by an earlier cap are
        orphans for :meth:`prune_song_cache` / :meth:`clear_disk_cache`.
        The URL is normalised first, because that is how every fetch
        caches it - popping the raw spelling missed every entry.
        """
        if not thumb_url:
            return
        normalized = _normalize_url(thumb_url)
        if normalized is None:
            return
        key = ThumbnailService._mem_key(normalized, size)
        with ThumbnailService._cache_lock:
            ThumbnailService._cache.pop(key, None)
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
        option).  Runs on the bounded thumbnail pool; failures are
        logged per song.
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

        ThumbnailService.submit(work)

    @staticmethod
    def prefetch_song_thumbs(songs: list[dict]) -> None:
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
        active_urls, size: tuple[int, int] = SONG_THUMB_SIZE
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
            ThumbnailService._dedupe_index_cache = None
            ThumbnailService._dedupe_index_sig = None
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