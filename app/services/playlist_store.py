"""
Persistent storage for playlists metadata (``db/playlists.json``).

Playlists are uniquely identified by the combination ``(platform, playlist_id)``.
When *playlist_id* is unknown (legacy entries), ``(platform, name)`` is used as
a fallback so existing data is never orphaned.
"""

import json
import os
import time
import threading
import logging
from pathlib import Path

from constants import PLATFORM_YOUTUBE_MUSIC

logger = logging.getLogger(__name__)

playlists_json = Path(__file__).resolve().parents[2] / "db" / "playlists.json"

# In-memory cache so we don't re-read the file on every operation.
_playlist_cache: list[dict] | None = None
_cache_timestamp: float = 0.0
_CACHE_TTL: float = 1.0  # seconds before re-reading

# Serialise all read/write access so concurrent mutations don't lose data.
_lock = threading.Lock()


def _find_by_key(
    playlists: list[dict],
    *,
    platform: str = "",
    name: str = "",
    playlist_id: str = "",
) -> dict | None:
    """Locate a playlist dict inside *playlists*.

    Lookup priority (first match wins):

    1. ``(platform, playlist_id)`` - primary key, used when *playlist_id* is
       non-empty.
    2. ``(platform, name)`` - legacy fallback for entries stored before the
       *playlist_id* field existed.
    3. ``name`` alone - legacy catch-all (no platform filter).
    """
    if playlist_id and platform:
        for p in playlists:
            if p.get("playlist_id") == playlist_id and p.get("platform") == platform:
                return p

    if name and platform:
        for p in playlists:
            if p.get("name") == name and p.get("platform") == platform:
                return p

    if name:
        for p in playlists:
            if p.get("name") == name:
                return p

    return None


class PlaylistStore:
    @staticmethod
    def load_playlists():
        """Return a copy of the playlist list, reading from disk if stale.

        The copy (instead of the cached list itself) makes iteration safe:
        writers only mutate their own copy under ``_lock`` and then publish
        it via :meth:`_write`, so a concurrent write can never resize the
        list a reader is iterating (which would otherwise raise
        ``RuntimeError: list changed size during iteration``).
        """
        global _playlist_cache, _cache_timestamp

        now = time.monotonic()
        if _playlist_cache is None or (now - _cache_timestamp) >= _CACHE_TTL:
            _playlist_cache = []
            _cache_timestamp = now
            if os.path.exists(playlists_json) and os.path.getsize(playlists_json) > 0:
                try:
                    with open(playlists_json, "r", encoding="utf-8") as f:
                        _playlist_cache = json.load(f)
                        _cache_timestamp = now
                except Exception as e:
                    logger.error(f"Failed to read playlists.json: {e}")
        return list(_playlist_cache)

    @staticmethod
    def get_existing_names(platform: str = ""):
        """Return a set of playlist names, optionally filtered by platform."""
        with _lock:
            playlists = PlaylistStore.load_playlists()
        if platform:
            return {
                p.get("name")
                for p in playlists
                if p.get("platform", PLATFORM_YOUTUBE_MUSIC) == platform
            }
        return {p.get("name") for p in playlists}

    @staticmethod
    def get_existing_ids_by_platform(platform: str) -> set[str]:
        """Return the set of *playlist_id* values already stored for *platform*.

        Useful for filtering API results before showing the playlist dialog
        so already-added playlists are hidden even if their name changed.
        """
        with _lock:
            playlists = PlaylistStore.load_playlists()
        return {
            p.get("playlist_id")
            for p in playlists
            if p.get("platform") == platform and p.get("playlist_id")
        }

    @staticmethod
    def find_playlist(
        name: str,
        platform: str = "",
        playlist_id: str = "",
    ):
        """Find a playlist entry by key.

        Args:
            name: Playlist name.
            platform: Platform identifier.
            playlist_id: Stable API identifier.  When provided the lookup
                favours ``(platform, playlist_id)`` over ``(platform, name)``.

        Returns:
            The playlist dict, or ``None`` if not found.
        """
        with _lock:
            playlists = PlaylistStore.load_playlists()
        return _find_by_key(playlists, platform=platform, name=name, playlist_id=playlist_id)

    @staticmethod
    def add_playlist(
        name: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
        thumbnail_url: str = "",
    ):
        """Add or update a playlist.

        The unique key is ``(platform, playlist_id)``.  If an entry with the
        same key already exists the existing record is updated **in-place**
        (preserving ``hotkey``) instead of appending a duplicate.

        If *playlist_id* is empty (legacy path) the fallback key
        ``(platform, name)`` is used for dedup.
        """
        with _lock:
            playlists = PlaylistStore.load_playlists()
            existing = _find_by_key(
                playlists, platform=platform, name=name, playlist_id=playlist_id,
            )
            if existing is not None:
                existing["name"] = name
                existing["playlist_id"] = playlist_id
                if thumbnail_url:
                    existing["thumbnail_url"] = thumbnail_url
                # hotkey is intentionally preserved - do not overwrite.
                logger.info(
                    "Updated playlist '%s' (platform=%s, id=%s)",
                    name, platform, playlist_id or "<legacy>",
                )
            else:
                playlists.append(
                    {
                        "name": name,
                        "platform": platform,
                        "hotkey": "",
                        "playlist_id": playlist_id,
                        "thumbnail_url": thumbnail_url,
                    }
                )
                logger.info(
                    "Added playlist '%s' (platform=%s, id=%s)",
                    name, platform, playlist_id or "<none>",
                )
            PlaylistStore._write(playlists)

    @staticmethod
    def update_thumbnail(name: str, platform: str, thumbnail_url: str):
        """Update the thumbnail URL for a single playlist.

        Matches by ``(platform, playlist_id)`` when available, falling back
        to ``(platform, name)`` for legacy entries.
        """
        with _lock:
            playlists = PlaylistStore.load_playlists()
            # We don't have playlist_id here, so match by (platform, name).
            target = _find_by_key(playlists, platform=platform, name=name)
            if target is not None:
                target["thumbnail_url"] = thumbnail_url
                PlaylistStore._write(playlists)

    @staticmethod
    def update_keybind(name: str, platform: str, hotkey: str):
        """Update the hotkey binding for a single playlist."""
        with _lock:
            playlists = PlaylistStore.load_playlists()
            target = _find_by_key(playlists, platform=platform, name=name)
            if target is not None:
                target["hotkey"] = hotkey
                PlaylistStore._write(playlists)

    @staticmethod
    def delete_playlist(name: str, platform: str, playlist_id: str = ""):
        """Remove a playlist entry.

        Args:
            name: Playlist name (used for fallback lookup).
            platform: Platform identifier (required).
            playlist_id: Stable API identifier (preferred lookup key).
        """
        with _lock:
            playlists = PlaylistStore.load_playlists()
            target = _find_by_key(
                playlists, platform=platform, name=name, playlist_id=playlist_id,
            )
            if target is not None:
                playlists.remove(target)
                logger.info(
                    "Deleted playlist '%s' (platform=%s, id=%s)",
                    name, platform, playlist_id or "<legacy>",
                )
                PlaylistStore._write(playlists)
            else:
                logger.warning(
                    "No playlist found to delete: name='%s', platform=%s, id=%s",
                    name, platform, playlist_id or "<none>",
                )

    @staticmethod
    def delete_playlists_for_platform(platform: str) -> int:
        """Remove every registry entry for *platform*.

        Returns the number of entries removed.  Entries without a platform
        field are treated as :data:`PLATFORM_YOUTUBE_MUSIC` (the legacy
        default, matching :meth:`get_existing_names`).
        """
        with _lock:
            playlists = PlaylistStore.load_playlists()
            kept = [
                p
                for p in playlists
                if p.get("platform", PLATFORM_YOUTUBE_MUSIC) != platform
            ]
            removed = len(playlists) - len(kept)
            if removed:
                logger.info(
                    "Removed %d playlist(s) for platform %s", removed, platform
                )
                PlaylistStore._write(kept)
            return removed

    @staticmethod
    def migrate_schema(
        lookup_playlist_id: "Callable[[str, str], str] | None" = None,
    ):
        """Backfill missing *playlist_id* values for legacy entries.

        For each entry where ``playlist_id`` is empty, attempt to fill it
        by calling *lookup_playlist_id(name, platform)*.  Provide a callback
        that queries the appropriate integration API.

        If no callback is supplied (or the callback returns an empty string),
        the entry is left untouched - the fallback logic in ``_find_by_key``
        will continue to work using ``(platform, name)``.
        """
        if lookup_playlist_id is None:
            return

        with _lock:
            playlists = PlaylistStore.load_playlists()
            changed = False
            for p in playlists:
                if p.get("playlist_id"):
                    continue
                platform = p.get("platform", "")
                name = p.get("name", "")
                if not platform or not name:
                    continue
                try:
                    pid = lookup_playlist_id(name, platform)
                except Exception:
                    logger.exception(
                        "Migration lookup failed for '%s' (%s)", name, platform
                    )
                    continue
                if pid:
                    p["playlist_id"] = pid
                    changed = True
                    logger.info(
                        "Migrated playlist '%s' (%s): playlist_id=%s", name, platform, pid
                    )
            if changed:
                PlaylistStore._write(playlists)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write(playlists):
        """Write the playlist list to disk atomically (temp-file + rename).

        Also updates the in-memory cache so subsequent reads skip the file.
        """
        global _playlist_cache, _cache_timestamp
        try:
            # Write to a temporary file, then atomically replace the real one.
            # This prevents partial/corrupt writes on crash.
            temp = playlists_json.with_suffix(".json.tmp")
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(playlists, f, ensure_ascii=False, indent=2)
            temp.replace(playlists_json)

            # Update cache so next read skips the file.
            _playlist_cache = playlists
            _cache_timestamp = time.monotonic()
        except Exception as e:
            logger.error(f"Failed to write playlists.json: {e}")
