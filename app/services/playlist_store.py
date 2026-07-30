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


class PlaylistStore:
    @staticmethod
    def load_playlists():
        """Return the cached playlist list, or read it from disk."""
        global _playlist_cache, _cache_timestamp

        now = time.monotonic()
        if _playlist_cache is not None and (now - _cache_timestamp) < _CACHE_TTL:
            return _playlist_cache

        if os.path.exists(playlists_json) and os.path.getsize(playlists_json) > 0:
            try:
                with open(playlists_json, "r", encoding="utf-8") as f:
                    _playlist_cache = json.load(f)
                    _cache_timestamp = now
                    return _playlist_cache
            except Exception as e:
                logger.error(f"Failed to read playlists.json: {e}")
        _playlist_cache = []
        _cache_timestamp = now
        return _playlist_cache

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
    def find_playlist(name: str, platform: str = ""):
        """Find a playlist by name, with an optional platform filter.

        If *platform* is provided, only entries matching *both* name and
        platform are returned.  If *platform* is empty, the first entry
        with a matching name is returned (order follows the JSON file).

        Because playlists on different platforms may share the same name,
        callers that know the platform should *always* pass it so they
        don't accidentally pick up a playlist from a different service.
        """
        with _lock:
            playlists = PlaylistStore.load_playlists()
        for p in playlists:
            if p.get("name") == name:
                stored_platform = p.get("platform")
                if not platform or stored_platform == platform:
                    return p
        return None

    @staticmethod
    def add_playlist(
        name: str,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
        thumbnail_url: str = "",
    ):
        with _lock:
            playlists = PlaylistStore.load_playlists()
            playlists.append(
                {
                    "name": name,
                    "platform": platform,
                    "hotkey": "",
                    "playlist_id": playlist_id,
                    "thumbnail_url": thumbnail_url,
                }
            )
            PlaylistStore._write(playlists)

    @staticmethod
    def update_thumbnail(name: str, platform: str, thumbnail_url: str):
        with _lock:
            playlists = PlaylistStore.load_playlists()
            for p in playlists:
                if p.get("name") == name and p.get("platform") == platform:
                    p["thumbnail_url"] = thumbnail_url
                    break
            PlaylistStore._write(playlists)

    @staticmethod
    def update_keybind(name: str, platform: str, hotkey: str):
        with _lock:
            playlists = PlaylistStore.load_playlists()
            for p in playlists:
                if p.get("name") == name and p.get("platform") == platform:
                    p["hotkey"] = hotkey
                    break
            PlaylistStore._write(playlists)

    @staticmethod
    def delete_playlist(name: str, platform: str = ""):
        with _lock:
            playlists = PlaylistStore.load_playlists()
            if platform:
                playlists = [
                    p
                    for p in playlists
                    if not (
                        p.get("name") == name
                        and p.get("platform", PLATFORM_YOUTUBE_MUSIC) == platform
                    )
                ]
            else:
                playlists = [p for p in playlists if p.get("name") != name]
            PlaylistStore._write(playlists)

    @staticmethod
    def _write(playlists):
        """Write the playlist list to disk atomically (temp-file + rename).

        Also updates the in-memory cache so subsequent reads skip the file.
        """
        global _playlist_cache, _cache_timestamp
        try:
            # Write to a temporary file, then atomically replace the real one.
            # This prevents partial/corrupt writes on crash (issue #2).
            temp = playlists_json.with_suffix(".json.tmp")
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(playlists, f, ensure_ascii=False, indent=2)
            temp.replace(playlists_json)

            # Update cache so next read skips the file.
            _playlist_cache = playlists
            _cache_timestamp = time.monotonic()
        except Exception as e:
            logger.error(f"Failed to write playlists.json: {e}")
