"""
Pending duplicate songs + pair-song memory (``db/extra.json``).

Two kinds of state live here:

``pending``
    Queued "Similar song" songs - a hotkey add fuzzy-matched a song
    already in the playlist and was NOT added anywhere; the user resolves
    it later in the activity window.

``songs``
    Pair-keyed memory of past resolutions (the "not duplicates" marker).
    Key format ``<platform>|<playlist_id>|<track_a>|<track_b>`` with the
    track ids sorted so the key is order-stable.  Values:
    ``{"song": "added" | "not_duplicate" | "dismissed", "at": iso}``
    - "added"         dialog Add:            future adds proceed silently
    - "not_duplicate" pair-card whitelist:  future adds bypass the check
    - "dismissed"     dialog Don't add:     future adds are skipped

``errors``
    Persistent error log shown in the activity window's Errors tab
    (today flow errors vanish into a red card status that is gone on the
    next keybind).  Capped at :data:`MAX_ERRORS`, newest first on read.

The file sits under the gitignored ``db/`` and is created on demand,
mirroring :mod:`services.playlist_store`'s write discipline (module-level
lock, atomic temp-file + rename, defensive parent mkdir).  Unlike
playlist_store there is no in-memory cache - the file is tiny (<50 pending
records), so every operation re-reads it under the lock, which removes
cache-invalidation bugs by construction.
"""

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

from services import profile_store as _profile_store

extra_json = _profile_store.db_dir() / "extra.json"

# Serialise all read/write access across flow threads and the UI thread.
_lock = threading.RLock()

# Oldest-evicted once exceeded - a backlog must not grow unbounded when a
# user ignores the activity window for weeks.
MAX_PENDING = 50
MAX_ERRORS = 100

VALID_SONGS = ("added", "not_duplicate", "dismissed")

_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    """Read the store file (caller holds *_lock*).

    A missing or corrupt file self-heals to an empty store - this is
    song memory, never worth crashing the app over.
    """
    data = {
        "version": _SCHEMA_VERSION,
        "pending": [],
        "songs": {},
        "errors": [],
    }
    try:
        if extra_json.exists() and extra_json.stat().st_size > 0:
            with open(extra_json, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data["version"] = loaded.get("version", _SCHEMA_VERSION)
                if isinstance(loaded.get("pending"), list):
                    data["pending"] = loaded["pending"]
                if isinstance(loaded.get("songs"), dict):
                    data["songs"] = loaded["songs"]
                if isinstance(loaded.get("errors"), list):
                    data["errors"] = loaded["errors"]
    except Exception as e:
        logger.error("Failed to read %s (%s) - starting from an empty store", extra_json, e)
    return data


def _write(data: dict) -> None:
    """Atomically persist the store (caller holds *_lock*).

    exFAT-safe: temp file in the same directory + rename, like
    playlist_store._write.  The db/ directory does not exist on a fresh
    clone and is created defensively here.
    """
    try:
        extra_json.parent.mkdir(parents=True, exist_ok=True)
        temp = extra_json.with_suffix(".json.tmp")
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp.replace(extra_json)
    except Exception as e:
        logger.error("Failed to write %s: %s", extra_json, e)


def make_pair_key(platform: str, playlist_id: str, track_a: str, track_b: str) -> str:
    """Order-stable pair key for the *songs* map.

    Track ids are sorted so both directions of a match collapse onto one
    key.  Legacy playlists carry ``playlist_id=""`` and stay name-scoped
    via their records - here the empty id is part of the key exactly like
    the flows use it for local DB addressing.
    """
    a, b = sorted((str(track_a or ""), str(track_b or "")))
    return f"{platform}|{playlist_id}|{a}|{b}"


# ----------------------------------------------------------------------
# Pending records
# ----------------------------------------------------------------------

def list_pending(prune_unregistered: bool = True) -> List[dict]:
    """Return pending song records (newest first).

    Recency is the file's append order - ``add_pending`` re-appends a
    refreshed record at the end - not ``created_at``, whose
    second-resolution timestamps cannot order records created in the
    same second.

    Records whose playlist is no longer registered are pruned on read
    (same liveness rule as the flow's ``playlist_still_registered``
    guard) unless *prune_unregistered* is False.
    """
    with _lock:
        data = _load()
        pending = list(data.get("pending", []))
        if prune_unregistered and pending:
            kept = [r for r in pending if _playlist_alive(r)]
            if len(kept) != len(pending):
                data["pending"] = kept
                _write(data)
            pending = kept
        return list(reversed(pending))


def _playlist_alive(record: dict) -> bool:
    """Store-liveness check for one pending record."""
    try:
        # Local import: keeps module import side-effect free for headless
        # harnesses that patch the store path.
        from services.playlist_store import playlist_still_registered

        return playlist_still_registered(
            record.get("playlist_name", ""),
            record.get("platform", ""),
            record.get("playlist_id") or None,
        )
    except Exception:
        return True  # err permissive - same policy as the flow guard


def add_pending(record: dict) -> Optional[str]:
    """Queue one near-duplicate song; returns its id.

    Re-enqueueing the same ``(playlist_id, track_id)`` replaces the old
    record instead of stacking duplicates of the same prompt.
    """
    record = dict(record)
    record.setdefault("id", uuid.uuid4().hex)
    record.setdefault("kind", "pending")
    record.setdefault("created_at", _now_iso())
    with _lock:
        data = _load()
        pending = [
            r
            for r in data.get("pending", [])
            if not (
                r.get("playlist_id") == record.get("playlist_id")
                and r.get("track_id") == record.get("track_id")
            )
        ]
        # Append = most recent; list_pending reverses for display and the
        # cap evicts from the front (oldest appended first).
        pending.append(record)
        if len(pending) > MAX_PENDING:
            pending = pending[-MAX_PENDING:]
            logger.info("extra.json pending backlog capped at %d", MAX_PENDING)
        data["pending"] = pending
        _write(data)
    return record["id"]


def remove_pending(record_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data.get("pending", []))
        data["pending"] = [
            r for r in data.get("pending", []) if r.get("id") != record_id
        ]
        removed = len(data["pending"]) < before
        if removed:
            _write(data)
        return removed


def find_pending(playlist_id: str, track_id: str) -> Optional[dict]:
    """Locate one queued record by its natural key (no pruning on read)."""
    with _lock:
        for r in _load().get("pending", []):
            if (
                r.get("playlist_id") == playlist_id
                and r.get("track_id") == track_id
            ):
                return dict(r)
    return None


# ----------------------------------------------------------------------
# song memory
# ----------------------------------------------------------------------

def get_song(pair_key: str) -> Optional[dict]:
    with _lock:
        stored = _load().get("songs", {}).get(pair_key)
        return dict(stored) if stored else None


def set_song(pair_key: str, song: str) -> None:
    if song not in VALID_SONGS:
        raise ValueError(f"Unknown duplicate-check song {song!r}")
    with _lock:
        data = _load()
        data.setdefault("songs", {})[pair_key] = {
            "song": song,
            "at": _now_iso(),
        }
        _write(data)


def delete_song(pair_key: str) -> bool:
    """Undo path for the marked-pairs manager."""
    with _lock:
        data = _load()
        songs = data.get("songs", {})
        if pair_key not in songs:
            return False
        del songs[pair_key]
        _write(data)
        return True


def list_songs() -> dict:
    """Copy of the whole songs map (marked-pairs manager source)."""
    with _lock:
        return dict(_load().get("songs", {}))


# ----------------------------------------------------------------------
# Error log
# ----------------------------------------------------------------------

def record_error(playlist_name: str, platform: str, message: str) -> dict:
    """Append one entry to the persistent error log (activity window)."""
    record = {
        "id": uuid.uuid4().hex,
        "kind": "error",
        "created_at": _now_iso(),
        "playlist_name": playlist_name or "",
        "platform": platform or "",
        "message": str(message),
    }
    with _lock:
        data = _load()
        errors = data.get("errors", [])
        errors.append(record)
        if len(errors) > MAX_ERRORS:
            errors = errors[-MAX_ERRORS:]
        data["errors"] = errors
        _write(data)
    return record


def list_errors() -> List[dict]:
    """Error log entries, newest first."""
    with _lock:
        errors = [
            dict(e) for e in reversed(_load().get("errors", []))
        ]
    return errors


def clear_errors() -> int:
    """Drop the whole error log (Errors-tab Clear button); returns count."""
    with _lock:
        data = _load()
        n = len(data.get("errors", []))
        if n:
            data["errors"] = []
            _write(data)
        return n


def purge_platform(platform: str) -> Tuple[int, int, int]:
    """Drop every record referencing *platform* (uninstall cleanup).

    Removes pending duplicate records, pair-memory entries (keys start
    with ``<platform>|`` - see :func:`make_pair_key`) and error-log
    entries for the platform.  Returns the removed counts as
    ``(pending, songs, errors)``.
    """
    with _lock:
        data = _load()
        pending = list(data.get("pending", []))
        songs = dict(data.get("songs", {}))
        errors = list(data.get("errors", []))

        kept_pending = [r for r in pending if r.get("platform") != platform]
        kept_songs = {
            k: v for k, v in songs.items() if not k.startswith(platform + "|")
        }
        kept_errors = [e for e in errors if e.get("platform") != platform]

        if (
            len(kept_pending) != len(pending)
            or len(kept_songs) != len(songs)
            or len(kept_errors) != len(errors)
        ):
            data["pending"] = kept_pending
            data["songs"] = kept_songs
            data["errors"] = kept_errors
            _write(data)
        return (
            len(pending) - len(kept_pending),
            len(songs) - len(kept_songs),
            len(errors) - len(kept_errors),
        )


# ----------------------------------------------------------------------
# Change stamp (cheap badge polling)
# ----------------------------------------------------------------------

def stamp() -> float:
    """File mtime as a change signal for badge polling.

    Reading a stat is far cheaper than parsing the JSON every poll tick,
    and cross-process writes (the CLI shares this file) are picked up
    for free.  0.0 when the file does not exist yet.
    """
    try:
        return extra_json.stat().st_mtime
    except OSError:
        return 0.0


def activity_count() -> int:
    """Pending songs + logged errors - the Activity badge number."""
    with _lock:
        data = _load()
        return len(data.get("pending", [])) + len(data.get("errors", []))
