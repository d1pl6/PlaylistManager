"""
Profile metadata and path resolution.

Single source of truth for profile data paths.  Imported at module level
by ``utils/config.py``, ``services/playlist_store.py``, etc. -- must be
cheap (one small JSON read) and free of app / tkinter imports.

The active profile name is persisted in ``cfg/profile.json`` (a tiny
``{"active": "default"}`` file).  Profile metadata (which buckets each
profile captures) lives in ``db/profiles.json``.

Switching profiles is a *restart* of the app -- all module-level path
constants are bound on first import, so mid-process re-pointing is not
feasible for v1.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_active: str = ""
_profiles_data: dict | None = None

# Where active-profile name is persisted (fast CLI read).
ACTIVE_JSON = Path(__file__).resolve().parents[2] / "cfg" / "profile.json"

# Where full profile metadata lives.
PROFILES_JSON = Path(__file__).resolve().parents[2] / "db" / "profiles.json"

# Default paths (global / shared locations).
_DEFAULT_DB_DIR = Path(__file__).resolve().parents[2] / "db"
_DEFAULT_CFG_DIR = Path(__file__).resolve().parents[2] / "cfg"

# Platformdirs auth root (outside repo).
try:
    from platformdirs import user_config_dir as _user_config_dir
    _AUTH_ROOT = Path(_user_config_dir("playlistmanager")) / "auth"
except Exception:
    _AUTH_ROOT = Path.home() / ".config" / "playlistmanager" / "auth"

# ---------------------------------------------------------------------------
# File I/O helpers (atomic, exFAT-safe)
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: object) -> None:
    """Atomically write *data* to *path* (temp + rename)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        logger.exception("Failed to write %s", path)


def _read_json(path: Path) -> dict | list | None:
    """Read JSON from *path*, returning None on any failure."""
    try:
        if path.exists() and path.stat().st_size > 0:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.warning("Failed to read %s", path)
    return None


# ---------------------------------------------------------------------------
# Active-profile persistence
# ---------------------------------------------------------------------------


def _ensure_active_file() -> None:
    """Create ``cfg/profile.json`` with the ``default`` profile if absent."""
    if ACTIVE_JSON.exists():
        return
    data = {
        "active": "default",
        "profiles": {
            "default": {
                "logins": False,
                "playlists": False,
                "settings": False,
            }
        },
    }
    _write_json(ACTIVE_JSON, data)


def _persist_active() -> None:
    """Write just the active profile name into ``cfg/profile.json``."""
    data = _read_json(ACTIVE_JSON)
    if data is None or not isinstance(data, dict):
        data = {}
    data["active"] = _active
    _write_json(ACTIVE_JSON, data)


# ---------------------------------------------------------------------------
# Profile-metadata persistence
# ---------------------------------------------------------------------------


def _ensure_profiles_file() -> None:
    """Create ``db/profiles.json`` with the ``default`` profile if absent."""
    if PROFILES_JSON.exists():
        return
    data = {
        "version": 1,
        "profiles": {
            "default": {
                "logins": False,
                "playlists": False,
                "settings": False,
            }
        },
    }
    _write_json(PROFILES_JSON, data)


def _load_profiles() -> dict:
    """Load ``db/profiles.json`` (caller holds *_lock* or is init-time)."""
    global _profiles_data
    if _profiles_data is not None:
        return _profiles_data
    _ensure_profiles_file()
    raw = _read_json(PROFILES_JSON)
    if not isinstance(raw, dict) or "profiles" not in raw:
        raw = {
            "version": 1,
            "profiles": {
                "default": {
                    "logins": False,
                    "playlists": False,
                    "settings": False,
                }
            },
        }
    _profiles_data = raw
    return _profiles_data


def _save_profiles(data: dict) -> None:
    """Persist ``db/profiles.json`` (caller holds *_lock*)."""
    global _profiles_data
    _write_json(PROFILES_JSON, data)
    _profiles_data = data


# ---------------------------------------------------------------------------
# Initialization (called once, at import time by config.py)
# ---------------------------------------------------------------------------


def initialize() -> None:
    """Read or seed the active profile.  Called once at import time.

    Creates ``cfg/profile.json`` and ``db/profiles.json`` on first run
    with a ``default`` profile (all buckets shared -- identical to
    pre-profile behaviour).
    """
    global _active
    if _active:
        return
    _ensure_active_file()
    raw = _read_json(ACTIVE_JSON)
    if isinstance(raw, dict) and "active" in raw:
        _active = str(raw["active"])
    else:
        _active = "default"


# ---------------------------------------------------------------------------
# Public API -- active profile
# ---------------------------------------------------------------------------


def active_profile() -> str:
    """Return the active profile name (``"default"`` until the user
    creates another)."""
    if not _active:
        initialize()
    return _active


def set_active(name: str) -> None:
    """Switch the active profile (persists to disk).

    The change takes effect on the *next* app launch (restart required).
    """
    with _lock:
        profiles = _load_profiles()
        if name not in profiles.get("profiles", {}):
            raise ValueError(f"Profile {name!r} does not exist")
        global _active
        _active = name
        _persist_active()


# ---------------------------------------------------------------------------
# Public API -- profile list / CRUD
# ---------------------------------------------------------------------------

# Valid profile names: alphanumeric + underscore + hyphen, no leading dot,
# no path-illegal characters.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def list_profiles() -> list[str]:
    """Return sorted profile names."""
    with _lock:
        profiles = _load_profiles()
        return sorted(profiles.get("profiles", {}).keys())


def create(
    name: str,
    logins: bool = True,
    playlists: bool = True,
    settings: bool = True,
) -> None:
    """Create a new profile.

    Validates *name* (non-empty, no illegal characters, unique),
    persists the new profile in ``db/profiles.json``, and copies current
    shared data into the profile slot for any captured bucket ("copy, never move").

    The new profile is **not** made active here: activation (``set_active``)
    is the caller's job, and callers defer it until the user has actually
    committed to restarting the app.  If a just-created profile were made
    active before a confirmed restart, the user who declines the restart
    would be stranded unable to delete it (delete refuses the active
    profile) even though the process still runs on the old profile's paths.
    """
    if not name or not name.strip():
        raise ValueError("Profile name cannot be empty")
    if len(name) > 64:
        raise ValueError("Profile name too long (max 64 characters)")
    if not _NAME_RE.match(name):
        raise ValueError(
            "Profile name must start with a letter or digit "
            "and contain only letters, digits, underscores, and hyphens"
        )
    if name.startswith("."):
        raise ValueError("Profile name must not start with a dot")

    with _lock:
        profiles = _load_profiles()
        if name in profiles.get("profiles", {}):
            raise ValueError(f"Profile {name!r} already exists")
        profiles.setdefault("profiles", {})[name] = {
            "logins": bool(logins),
            "playlists": bool(playlists),
            "settings": bool(settings),
        }
        profiles["version"] = profiles.get("version", 1)
        _save_profiles(profiles)

    # Copy current shared data into the profile slot for captured buckets
    # ("copy, never move") so the new profile starts from
    # today's state instead of empty.  Copies, never moves: the shared data
    # stays intact until the user flips a bucket on this profile.
    _copy_bucket_data(name, playlists=playlists, settings=settings, logins=logins)


def _copy_dir_contents(src: Path, dst: Path) -> None:
    """Copy every file in *src* into *dst* (create *dst* if needed).

    The ``profiles`` subdirectory (the profile slots themselves) is
    skipped so a copy can never nest slots inside slots.
    """
    if not src.is_dir():
        return
    import shutil

    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name == "profiles":
            continue
        target = dst / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif item.is_file():
                shutil.copy2(item, target)
        except OSError:
            logger.warning("Failed to copy %s into profile slot", item)


def _copy_bucket_data(name: str, *, playlists: bool, settings: bool, logins: bool) -> None:
    """Copy current shared data into *name*'s owned slots (best effort).

    Only copies files that already exist in the shared/global location;
    a fresh install with nothing to copy leaves the profile slot empty,
    which is correct.
    """
    if playlists:
        _copy_dir_contents(_DEFAULT_DB_DIR, _DEFAULT_DB_DIR / "profiles" / name)
    if settings:
        _copy_dir_contents(_DEFAULT_CFG_DIR, _DEFAULT_CFG_DIR / "profiles" / name)
    if logins and _AUTH_ROOT.is_dir():
        _copy_dir_contents(_AUTH_ROOT, _AUTH_ROOT / name)


def delete(name: str) -> None:
    """Delete a profile.

    The ``default`` profile and the currently active profile cannot be
    deleted.  Owned bucket data (``db/profiles/<name>/``) is removed;
    shared bucket data is left untouched.
    """
    if name == "default":
        raise ValueError("Cannot delete the default profile")
    with _lock:
        profiles = _load_profiles()
        progs = profiles.get("profiles", {})
        if name not in progs:
            raise ValueError(f"Profile {name!r} does not exist")
        if name == active_profile():
            raise ValueError("Cannot delete the active profile -- switch first")
        progs.pop(name)
        _save_profiles(profiles)
    # Remove owned playlists directory.
    import shutil

    owned = _DEFAULT_DB_DIR / "profiles" / name
    if owned.is_dir():
        shutil.rmtree(owned, ignore_errors=True)
    # Remove profile-specific cfg if it exists.
    owned_cfg = _DEFAULT_CFG_DIR / "profiles" / name
    if owned_cfg.is_dir():
        shutil.rmtree(owned_cfg, ignore_errors=True)


def rename(old: str, new: str) -> None:
    """Rename a profile (updates metadata and moves owned data)."""
    if old == "default":
        raise ValueError("Cannot rename the default profile")
    if not new or not _NAME_RE.match(new):
        raise ValueError(f"Invalid new profile name {new!r}")
    with _lock:
        profiles = _load_profiles()
        progs = profiles.get("profiles", {})
        if old not in progs:
            raise ValueError(f"Profile {old!r} does not exist")
        if new in progs:
            raise ValueError(f"Profile {new!r} already exists")
        progs[new] = progs.pop(old)
        _save_profiles(profiles)
        # Move owned data on disk.
        for base in (_DEFAULT_DB_DIR / "profiles", _DEFAULT_CFG_DIR / "profiles"):
            src = base / old
            if src.is_dir():
                dst = base / new
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
        # If this was the active profile, update the pointer.
        global _active
        if old == active_profile():
            _active = new
            _persist_active()


def set_bucket(name: str, bucket: str, on: bool) -> None:
    """Turn a bucket on or off for a profile.

    Turning *on* copies current shared data into the profile slot (so
    the profile starts from today's state).  Turning *off* stops new
    writes going to the profile slot (the data is left on disk but
    hidden - destroying data on a checkbox flip is unacceptable).
    """
    if bucket not in ("logins", "playlists", "settings"):
        raise ValueError(f"Unknown bucket {bucket!r}")
    with _lock:
        profiles = _load_profiles()
        progs = profiles.get("profiles", {})
        if name not in progs:
            raise ValueError(f"Profile {name!r} does not exist")
        was_on = progs[name].get(bucket, False)
        progs[name][bucket] = bool(on)
        _save_profiles(progs)
    if on and not was_on:
        # First time this profile captures the bucket: snapshot current
        # shared data into the profile slot.
        if bucket == "playlists":
            _copy_dir_contents(_DEFAULT_DB_DIR, _DEFAULT_DB_DIR / "profiles" / name)
        elif bucket == "settings":
            _copy_dir_contents(_DEFAULT_CFG_DIR, _DEFAULT_CFG_DIR / "profiles" / name)
        elif bucket == "logins" and _AUTH_ROOT.is_dir():
            _copy_dir_contents(_AUTH_ROOT, _AUTH_ROOT / name)


def get_bucket(name: str, bucket: str) -> bool:
    """Return whether *bucket* is captured by profile *name*."""
    with _lock:
        profiles = _load_profiles()
        return profiles.get("profiles", {}).get(name, {}).get(bucket, False)


# ---------------------------------------------------------------------------
# Path resolution -- the single source of truth
# ---------------------------------------------------------------------------


def db_dir() -> Path:
    """``db/`` root for the active profile's Playlists bucket.

    Returns ``db/profiles/<name>/`` when the active profile captures the
    Playlists bucket, otherwise the shared ``db/`` directory.
    """
    active = active_profile()
    if get_bucket(active, "playlists"):
        return _DEFAULT_DB_DIR / "profiles" / active
    return _DEFAULT_DB_DIR


def cfg_dir() -> Path:
    """``cfg/`` root for the active profile's Settings bucket.

    Returns ``cfg/profiles/<name>/`` when the active profile captures
    the Settings bucket, otherwise the shared ``cfg/`` directory.
    """
    active = active_profile()
    if get_bucket(active, "settings"):
        return _DEFAULT_CFG_DIR / "profiles" / active
    return _DEFAULT_CFG_DIR


def auth_dir() -> Path:
    """Platformdirs auth root for the active profile's Logins bucket.

    Returns ``<platformdirs>/auth/<name>/`` when the active profile
    captures the Logins bucket, otherwise the shared
    ``<platformdirs>/auth/`` directory.
    """
    active = active_profile()
    if get_bucket(active, "logins"):
        return _AUTH_ROOT / active
    return _AUTH_ROOT


def global_db_dir() -> Path:
    """Shared ``db/`` directory (regardless of profile settings)."""
    return _DEFAULT_DB_DIR


def global_cfg_dir() -> Path:
    """Shared ``cfg/`` directory (regardless of profile settings)."""
    return _DEFAULT_CFG_DIR


def global_auth_dir() -> Path:
    """Shared auth directory (regardless of profile settings)."""
    return _AUTH_ROOT
