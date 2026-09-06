"""
Auto-scrobble ledger (``db/scrobbles.json``).

Records, for every add-flow that auto-scrobbled a song, the exact Unix
timestamp Last.fm accepted.  The showcase "remove song" action then
deletes THAT scrobble (``library.removeScrobble`` with the timestamp)
instead of the track's most recent one - deleting a guess would silently
erase a legitimate earlier scrobble the user actually listened to.

The file sits under the gitignored ``db/`` and is created on demand,
mirroring :mod:`services.duplicate_queue`'s discipline: module-level
lock, atomic temp-file + rename, defensive parent mkdir, re-read under
the lock - no in-memory cache to go stale.

Records are keyed by ``(platform, playlist_id, song_id)``: song_id is the
local per-playlist DB row id, so the same track added to two playlists
records two separate entries that can only be unscrobbled by removing the
matching row.  The row's exact timestamp lives here so a later row
re-import (reload swaps row ids) or a pruned entry simply means "we no
longer know which scrobble this add created" - and the remove path then
leaves the song's scrobble history alone instead of guessing.

Entries are best-effort bookkeeping, capped FIFO so an ignored ledger
cannot grow without bound.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from services import profile_store as _profile_store

scrobbles_json = _profile_store.db_dir() / "scrobbles.json"

# Serialise all read/write access across flow threads and the UI thread.
_lock = threading.RLock()

# Oldest-evicted once exceeded - a ledger that is never cleaned (rows
# removed via reloads, playlists uninstalled) must not grow forever.
MAX_ENTRIES = 4096

_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    """Read the ledger file (caller holds *_lock*).

    A missing or corrupt file self-heals to an empty store - this is
    bookkeeping, never worth crashing the app over.
    """
    data = {
        "version": _SCHEMA_VERSION,
        "scrobbles": {},
    }
    try:
        if scrobbles_json.exists() and scrobbles_json.stat().st_size > 0:
            with open(scrobbles_json, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data["version"] = loaded.get("version", _SCHEMA_VERSION)
                if isinstance(loaded.get("scrobbles"), dict):
                    data["scrobbles"] = loaded["scrobbles"]
    except Exception as e:
        logger.error(
            "Failed to read %s (%s) - starting from an empty ledger",
            scrobbles_json, e,
        )
    return data


def _write(data: dict) -> None:
    """Atomically persist the ledger (caller holds *_lock*).

    exFAT-safe: temp file in the same directory + rename, like
    duplicate_queue._write.  The db/ directory does not exist on a fresh
    clone and is created defensively here.
    """
    try:
        scrobbles_json.parent.mkdir(parents=True, exist_ok=True)
        temp = scrobbles_json.with_suffix(".json.tmp")
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp.replace(scrobbles_json)
    except Exception as e:
        logger.error("Failed to write %s: %s", scrobbles_json, e)


def _entry_count(scrobbles: dict) -> int:
    """Total song records across all platforms/playlists (nested dicts)."""
    return sum(
        len(songs)
        for playlists in scrobbles.values()
        for songs in playlists.values()
    )


def _prune(data: dict) -> None:
    """Drop oldest records FIFO (insertion order = JSON object order)."""
    scrobbles = data.get("scrobbles", {})
    total = _entry_count(scrobbles)
    while total > MAX_ENTRIES:
        dropped = False
        for platform in list(scrobbles):
            playlists = scrobbles[platform]
            for playlist_id in list(playlists):
                songs = playlists[playlist_id]
                oldest = next(iter(songs))
                del songs[oldest]
                total -= 1
                dropped = True
                if not songs:
                    del playlists[playlist_id]
                if total <= MAX_ENTRIES:
                    break
            if not playlists:
                del scrobbles[platform]
            if total <= MAX_ENTRIES:
                break
        if not dropped:
            break  # defensive: nothing left to drop


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_scrobble(platform: str, playlist_id: str, song_id, timestamp: int) -> None:
    """Remember that *song_id* (a row in a local playlist DB) was
    auto-scrobbled and accepted at *timestamp* (epoch seconds).

    Without a *song_id* (the row never landed - e.g. the playlist was
    removed mid-flow) there is nothing to pin a future unscrobble on, so
    nothing is recorded.
    """
    if not song_id:
        return
    with _lock:
        data = _load()
        scrobbles = data.setdefault("scrobbles", {})
        playlists = scrobbles.setdefault(str(platform), {})
        songs = playlists.setdefault(str(playlist_id), {})
        songs[str(song_id)] = {
            "timestamp": int(timestamp),
            "at": _now_iso(),
        }
        _prune(data)
        _write(data)


def lookup_scrobble(platform: str, playlist_id: str, song_id) -> Optional[int]:
    """Return the recorded accepted scrobble timestamp for a local DB row,
    or ``None`` when no auto-scrobble was recorded for that row (the
    add's auto-scrobble was off, the row was re-imported by a reload
    swapping its song_id, or the entry was pruned)."""
    if not song_id:
        return None
    with _lock:
        data = _load()
        songs = (
            data.get("scrobbles", {})
            .get(str(platform), {})
            .get(str(playlist_id), {})
        )
        rec = songs.get(str(song_id)) if isinstance(songs, dict) else None
        if not isinstance(rec, dict):
            return None
        try:
            return int(rec.get("timestamp"))
        except (TypeError, ValueError):
            return None


def clear_scrobble(platform: str, playlist_id: str, song_id) -> None:
    """Forget a recorded scrobble - call after the platform confirmed the
    delete, so a failed delete keeps its record for a later retry."""
    if not song_id:
        return
    with _lock:
        data = _load()
        playlists = data.get("scrobbles", {}).get(str(platform))
        if not isinstance(playlists, dict):
            return
        songs = playlists.get(str(playlist_id))
        if not isinstance(songs, dict):
            return
        if songs.pop(str(song_id), None) is None:
            return  # nothing changed - do not rewrite the file
        if not songs:
            del playlists[str(playlist_id)]
        if not playlists:
            del data["scrobbles"][str(platform)]
        _write(data)


def remove_playlist_entries(platform: str, playlist_id: str) -> None:
    """Drop every record for a playlist (playlist deletion / uninstall)."""
    with _lock:
        data = _load()
        playlists = data.get("scrobbles", {}).get(str(platform))
        if not isinstance(playlists, dict) or str(playlist_id) not in playlists:
            return
        del playlists[str(playlist_id)]
        if not playlists:
            del data["scrobbles"][str(platform)]
        _write(data)


def remove_platform_entries(platform: str) -> None:
    """Drop every record for a platform (per-platform uninstall)."""
    with _lock:
        data = _load()
        scrobbles = data.get("scrobbles", {})
        if str(platform) not in scrobbles:
            return
        del scrobbles[str(platform)]
        _write(data)