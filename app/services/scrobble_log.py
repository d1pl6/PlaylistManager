"""
Auto-scrobble ledger (``db/scrobbles.json``).

Records, for every add-flow that auto-scrobbled a song, the exact Unix
timestamp the backends accepted AND which ones accepted it (Last.fm,
ListenBrainz, ... - every platform with ``[<platform>] scrobbles = true``
receives the same listen).  The showcase "remove song" action then deletes
THAT scrobble on EACH of those services (Last.fm's
``library.removeScrobble`` with the timestamp, ListenBrainz's
``/1/delete-listen``) instead of the track's most recent one - deleting a
guess would silently erase a legitimate earlier scrobble the user actually
listened to, and asking a service that never got the listen would not
delete anything while leaving the real one behind.

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
from collections.abc import Iterable
from datetime import datetime

logger = logging.getLogger(__name__)

from services import profile_store as _profile_store
from utils.atomic_json import atomic_json_write

# Bound at import time so paths resolve exactly once per process; the
# active profile can only change via restart, so re-evaluating here
# would be both wasted work and wrong mid-process.
scrobbles_json = _profile_store.db_dir() / "scrobbles.json"

# Serialise all read/write access across flow threads and the UI thread.
_lock = threading.RLock()

# Oldest-evicted once exceeded - a ledger that is never cleaned (rows
# removed via reloads, playlists uninstalled) must not grow forever.
MAX_ENTRIES = 4096

# v2 added the per-record ``backend`` field (which service backend accepted
# the scrobble); v3 turned it into ``backends``, a LIST, because every
# backend switched on with ``[<platform>] scrobbles = true`` now receives the
# same listen and all of them have to be told apart on the way back out.
#
# Older records are read as a single-element list, so a v1 record (no
# backend field at all) becomes ``[]`` - "unknown", which the remove path
# reports rather than guessing a service.
_SCHEMA_VERSION = 3


def _backends_of(record) -> list:
    """The service ids a stored record says accepted the scrobble.

    Tolerates every schema version: v3 ``backends`` (list), v2 ``backend``
    (one id), v1 (neither -> unknown).
    """
    if not isinstance(record, dict):
        return []
    stored = record.get("backends")
    if isinstance(stored, str):
        stored = [stored]
    if not isinstance(stored, list):
        stored = []
    ids = [str(b).strip() for b in stored if str(b or "").strip()]
    if not ids:
        legacy = record.get("backend")
        if isinstance(legacy, str) and legacy.strip():
            ids = [legacy.strip()]
    return ids


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

    Delegates to :func:`utils.atomic_json.atomic_json_write`.  Failures
    are logged and swallowed - this is bookkeeping, never worth crashing
    the add flow over (see module docstring).
    """
    try:
        atomic_json_write(scrobbles_json, data)
    except Exception as e:
        logger.error("Failed to write %s: %s", scrobbles_json, e)


def _entry_count(scrobbles: dict) -> int:
    """Total song records across all platforms/playlists (nested dicts).

    Tolerates parseable-but-misshapen ledgers (a platform mapped to a
    non-dict, a playlist mapped to a non-dict): malformed levels are
    skipped instead of raising TypeError out of ``record_scrobble`` -
    this is bookkeeping, never worth crashing the add flow over.
    """
    total = 0
    for playlists in scrobbles.values():
        if not isinstance(playlists, dict):
            continue
        for songs in playlists.values():
            if isinstance(songs, dict):
                total += len(songs)
    return total


def _prune(data: dict) -> None:
    """Drop oldest records FIFO (insertion order = JSON object order).

    Misshapen levels (see :func:`_entry_count`) are dropped outright so
    the ledger self-heals back to a regular shape.
    """
    scrobbles = data.get("scrobbles", {})
    if not isinstance(scrobbles, dict):
        return
    total = _entry_count(scrobbles)
    while total > MAX_ENTRIES:
        dropped = False
        for platform in list(scrobbles):
            playlists = scrobbles[platform]
            if not isinstance(playlists, dict):
                del scrobbles[platform]
                continue
            for playlist_id in list(playlists):
                songs = playlists[playlist_id]
                if not isinstance(songs, dict):
                    del playlists[playlist_id]
                    continue
                if not songs:
                    # An empty songs dict contributes nothing to the
                    # count but would make next(iter(songs)) raise -
                    # self-heal it away (see _entry_count).
                    del playlists[playlist_id]
                    continue
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


def record_scrobble(
    platform: str,
    playlist_id: str,
    song_id,
    timestamp: int,
    backends: Iterable[str] = (),
) -> None:
    """Remember that *song_id* (a row in a local playlist DB) was
    auto-scrobbled and accepted at *timestamp* (epoch seconds).

    *backends* are the ids of the service backends that accepted it
    (``lastfm``, ``listenbrainz``, ... - one per platform enabled with
    ``[<platform>] scrobbles = true``).  It is what makes the later delete
    land on the services the scrobble actually went to: a user can switch
    backends off between the add and the remove, and resolving the target at
    delete time would then address the wrong service (or, worse, delete a
    listen this app never made).  A single id is accepted for convenience;
    it is stored as a one-element list.

    Without a *song_id* (the row never landed - e.g. the playlist was
    removed mid-flow) there is nothing to pin a future unscrobble on, so
    nothing is recorded.
    """
    if not song_id:
        return
    with _lock:
        data = _load()
        # Self-heal misshapen levels instead of letting setdefault chain
        # through a non-dict platform/playlist entry (see _entry_count).
        scrobbles = data.setdefault("scrobbles", {})
        playlists = scrobbles.get(str(platform))
        if not isinstance(playlists, dict):
            playlists = scrobbles[str(platform)] = {}
        songs = playlists.get(str(playlist_id))
        if not isinstance(songs, dict):
            songs = playlists[str(playlist_id)] = {}
        if isinstance(backends, str):
            backends = [backends]
        accepted = [str(b).strip() for b in (backends or ()) if str(b or "").strip()]
        songs[str(song_id)] = {
            "timestamp": int(timestamp),
            "at": _now_iso(),
            "backends": accepted,
        }
        _prune(data)
        _write(data)


def lookup_scrobble_record(platform: str, playlist_id: str, song_id) -> dict | None:
    """The whole ledger record for a local DB row, or ``None``.

    ``{"timestamp": int, "at": str, "backends": [str, ...]}``.  ``backends``
    is ``[]`` for records written before it was tracked (schema v1) and for
    an integration that reports no id - the remove path treats that as
    "unknown service" and leaves the scrobble alone.
    """
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
            timestamp = int(rec.get("timestamp"))
        except (TypeError, ValueError):
            return None
        return {
            "timestamp": timestamp,
            "at": str(rec.get("at") or ""),
            "backends": _backends_of(rec),
        }


def lookup_scrobble(platform: str, playlist_id: str, song_id) -> int | None:
    """Return the recorded accepted scrobble timestamp for a local DB row,
    or ``None`` when no auto-scrobble was recorded for that row (the
    add's auto-scrobble was off, the row was re-imported by a reload
    swapping its song_id, or the entry was pruned)."""
    record = lookup_scrobble_record(platform, playlist_id, song_id)
    return record["timestamp"] if record else None


def clear_scrobble(platform: str, playlist_id: str, song_id) -> None:
    """Forget a recorded scrobble, called once the local row it is keyed by
    is going away - whether or not every service confirmed its delete.

    The record is deliberately dropped even when a delete failed: it is keyed
    by the local DB row, and that row is removed in the same operation, so
    nothing could ever retry against it again and it would only occupy the
    FIFO cap.  A service that could not be reached is reported by the caller
    (see ``ui.showcase_manager._delete_scrobbles``), which is where the user
    learns the listen is still there."""
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