"""
OS-keyring mirror for platform credentials (credentials at rest).

The credential FILES under the profile-aware auth dir remain the
plugin-facing contract: platform plugins read - and some flows write -
those JSON files directly, and the ``ytmusicapi`` terminal flow writes
``browser.json`` itself outside the app.  The OS keyring holds an
encrypted MIRROR of each auth-dir credential file:

* core-originated writes mirror the payload into the keyring when a
  usable backend exists (``mirror()``);
* a missing file is restored from the keyring on demand
  (:func:`restore`) and at startup (:func:`restore_all`);
* logout / uninstall delete the keyring copy alongside the file
  (:func:`delete`) - the cleanup runs even when mirroring is turned
  off, so a previously-mirrored entry can never survive a logout;
* profile lifecycle is mirrored too: deleting a profile sweeps its
  entries (:func:`delete_profile`) and renaming one moves them
  (:func:`rename_profile`) - otherwise the account prefix would leave
  live credentials stranded in the keyring after the profile's auth
  dir is gone or moved.

Machines without a usable backend (headless Linux without a
secret-service daemon, CI) degrade to file-only storage - the same
0600-files behavior as before - and log the degradation once.  The
mirror never blocks a login and never raises into a caller.

``keyring`` itself is an OPTIONAL dependency (the ``keyring`` extra):
it is the only one of the declared packages with a heavy, platform-
conditional transitive chain (``SecretStorage`` -> ``cryptography`` +
``jeepney`` on Linux) and it is useless without a running daemon, so
it is not worth forcing on every install.  The import therefore stays
lazy (inside :func:`_backend`) and the probe classifies its absence
separately (:func:`_probe_backend` returns a reason) so the log tells
the user to install the package rather than to chase a daemon they may
already have.

The ``[security] credential_keyring`` setting (default ``on``, see
utils/config.py) is the documented toggle: ``off`` stops mirroring and
restoring without touching existing keyring entries.

Only *usable* backends are trusted.  ``keyring``'s own ``fail`` backend
and the entire ``keyrings.alt`` package (plaintext reimplementations -
strictly weaker than the 0600 files they would mirror) count as
unavailable, and a functional write/read/delete probe must succeed for
the keyring to be considered available.  Availability is sampled once
per process and cached; a desktop keyring daemon that starts after the
app will be picked up on the next launch.
"""

import logging
import os
import tempfile
import threading
from pathlib import Path, PurePosixPath

from services import profile_store as _profile_store

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "playlistmanager"
_PROBE_ACCOUNT = "credential_store_probe"

# Probe result cache (None = not sampled yet).  Guarded by _lock.
_available: bool | None = None
# Why the probe failed ("", "missing", "unusable"); only meaningful while
# _available is False, and recomputed with it.
_reason: str = ""
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Backend probing
# ---------------------------------------------------------------------------


def _backend():
    """Return the active keyring backend (may raise).

    Kept behind a function so tests can swap in a deterministic fake
    (``monkeypatch.setattr(credential_store, "_backend", lambda: fake)``)
    instead of mutating the global ``keyring`` backend.

    The import is lazy because ``keyring`` is an optional dependency -
    its absence surfaces as ``ImportError`` here, which
    :func:`_probe_backend` classifies as "not installed" rather than
    crashing the app at import time.
    """
    import keyring

    return keyring.get_keyring()


def _is_usable_backend(kr) -> bool:
    """True when *kr* is an OS-backed keyring, not a placeholder.

    Vetoes the library's own fail backend (raises on every op) and
    every ``keyrings.alt`` backend (plaintext/unencrypted
    reimplementations - mirroring credentials into them would store a
    second plaintext copy, strictly worse than the 0600 files alone).
    """
    try:
        module = type(kr).__module__
    except Exception:
        return False
    if module == "keyring.backends.fail":
        return False
    if module.startswith("keyrings.alt"):
        return False
    return True


def _probe_backend() -> tuple[bool, str]:
    """Functional write/read/delete round trip against the keyring.

    Returns ``(available, reason)``.  *reason* is ``""`` when available,
    ``"missing"`` when the optional ``keyring`` package is not installed,
    and ``"unusable"`` for every other failure (no backend, the library's
    fail backend, a ``keyrings.alt`` reimplementation, a daemon that
    refuses the write).  The two failure kinds need different advice -
    starting a secret-service daemon does nothing for a missing package.
    """
    try:
        kr = _backend()
    except ImportError:
        return False, "missing"
    except Exception:
        return False, "unusable"
    if not _is_usable_backend(kr):
        return False, "unusable"
    try:
        kr.set_password(KEYRING_SERVICE, _PROBE_ACCOUNT, "probe")
        ok = kr.get_password(KEYRING_SERVICE, _PROBE_ACCOUNT) == "probe"
    except Exception:
        return False, "unusable"
    finally:
        try:
            kr.delete_password(KEYRING_SERVICE, _PROBE_ACCOUNT)
        except Exception:
            pass
    return (True, "") if ok else (False, "unusable")


# Log text per probe failure reason (see _probe_backend).
_PROBE_HINTS = {
    "missing": (
        "The optional 'keyring' package is not installed - credentials stay "
        "file-only (0600 auth dir files); install it with "
        'pip install "PlaylistManager[keyring]" (distro package: '
        "python-keyring / python3-keyring), or set "
        "[security] credential_keyring=off to silence this check"
    ),
    "unusable": (
        "No usable OS keyring backend - credentials stay file-only (0600 "
        "auth dir files); install/start a secret-service daemon "
        "(gnome-keyring, kwallet) or set "
        "[security] credential_keyring=off to silence this check"
    ),
}


def keyring_available() -> bool:
    """Whether a usable OS keyring backend is present (cached probe).

    Sampled once per process: a desktop daemon that appears after the
    first probe is picked up on the next launch.
    """
    global _available, _reason
    if _available is None:
        with _lock:
            if _available is None:
                _available, _reason = _probe_backend()
                if not _available:
                    logger.warning(
                        "%s", _PROBE_HINTS.get(_reason, _PROBE_HINTS["unusable"])
                    )
    return _available


def mirroring_enabled() -> bool:
    """True when the ``[security] credential_keyring`` toggle is on."""
    from utils.config import get_setting_value

    return (
        get_setting_value("security", "credential_keyring", "on").strip().lower()
        != "off"
    )


# ---------------------------------------------------------------------------
# Paths and account naming
# ---------------------------------------------------------------------------


def auth_file(name: str) -> Path:
    """Full path of *name* inside the profile-aware auth dir.

    *name* must be a bare filename - same traversal rejection as
    ``PluginInfo.auth_path``: slashes or ``..`` segments are refused.
    """
    if not name or PurePosixPath(name).name != name:
        raise ValueError(f"credential name {name!r} must be a bare filename")
    return _profile_store.auth_dir() / name


def _account(name: str, profile: str | None = None) -> str:
    """OS-keyring account name: isolates profiles from each other.

    *profile* defaults to the active profile.  Pass it explicitly when
    operating on a profile that is NOT active - the profile delete /
    rename sweeps must never touch the active profile's entries (or
    resolve their own name into it).
    """
    if profile is None:
        profile = _profile_store.active_profile()
    return f"{profile}:{name}"


# ---------------------------------------------------------------------------
# Keyring primitives (never raise)
# ---------------------------------------------------------------------------


def _keyring_get(name: str, profile: str | None = None) -> str | None:
    try:
        return _backend().get_password(KEYRING_SERVICE, _account(name, profile))
    except Exception:
        return None


def _keyring_set(name: str, payload: str, profile: str | None = None) -> bool:
    try:
        _backend().set_password(KEYRING_SERVICE, _account(name, profile), payload)
        return True
    except Exception:
        logger.debug("OS keyring write failed for %s", name)
        return False


def _keyring_delete(name: str, profile: str | None = None) -> bool:
    try:
        _backend().delete_password(KEYRING_SERVICE, _account(name, profile))
        return True
    except Exception:
        return False  # missing entry and broken backend both land here


def _known_credential_names() -> list[str]:
    """Basenames of every auth-dir credential file the app declares.

    ``keyring`` exposes no portable entry-enumeration API, so a
    profile-wide sweep (delete / rename) can only name the files the app
    itself knows about.  Same source of truth as :func:`restore_all`:
    ``auth_setup.PLATFORM_CREDENTIAL_FILES``, which also carries the
    hardcoded-map fallback used when a manifest cannot be read.
    """
    try:
        from services.auth_setup import PLATFORM_CREDENTIAL_FILES
    except Exception:
        return []
    names: list[str] = []
    for paths in PLATFORM_CREDENTIAL_FILES.values():
        for p in paths:
            if p.name not in names:
                names.append(p.name)
    return names


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def mirror(name: str, payload: str | None = None) -> bool:
    """Copy a credential file's payload into the OS keyring.

    *name* is the bare filename inside the auth dir; *payload* overrides
    reading the file back (only used when the caller holds the exact
    bytes the plugin just wrote - the default read-back keeps the mirror
    in lockstep with what is actually on disk).

    No-op when mirroring is off or no usable backend exists; a payload
    identical to the stored one is skipped (polls re-observe the same
    terminal-flow file without spamming the daemon).  Returns True when
    the keyring now holds *payload*.
    """
    if not mirroring_enabled() or not keyring_available():
        return False
    if payload is None:
        try:
            payload = auth_file(name).read_text(encoding="utf-8")
        except OSError:
            return False
    if _keyring_get(name) == payload:
        return True
    return _keyring_set(name, payload)


def restore(name: str) -> bool:
    """Re-materialize a MISSING credential file from the OS keyring.

    Only writes when the file does not exist - an on-disk file always
    wins (plugins may have refreshed it since the last mirror, and the
    keyring copy could be stale).  Atomic 0600 write, auth dir ensured
    0700.  Returns True when the file was written.
    """
    if not mirroring_enabled() or not keyring_available():
        return False
    path = auth_file(name)
    if path.exists():
        return False
    payload = _keyring_get(name)
    if payload is None:
        return False
    try:
        auth_dir = _profile_store.auth_dir()
        auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            auth_dir.chmod(0o700)
        except OSError:
            pass
        # mkstemp, NOT a predictable "<name>.tmp" opened with O_CREAT:
        # O_CREAT applies 0600 only when it CREATES the file, so a stale
        # loose tmp (crash remnant, another tool) would be written
        # through with its old mode - and os.replace carries that mode
        # onto the final credential file.  mkstemp always creates a
        # fresh 0600 file (umask can only clear bits) and cannot collide
        # with or clobber anything already in the auth dir.
        fd, tmp = tempfile.mkstemp(dir=str(auth_dir), prefix=name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)  # tmp keeps its 0600 mode through rename
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.info("Restored credential file %s from the OS keyring", name)
        return True
    except OSError as e:
        logger.warning("Could not restore %s from the OS keyring: %s", name, e)
        return False


def restore_all() -> None:
    """Restore every missing auth-dir credential file from the keyring.

    Best-effort startup hook: called before integrations build their
    auth managers (GUI) and before headless CLI flows touch platforms.
    Missing files are re-materialized; everything else is a no-op.
    Never raises.

    Each name comes from :func:`_known_credential_names` but the FILE is
    resolved through :func:`auth_file` (the profile-aware resolver).
    That split matters: ``auth_setup.PLATFORM_CREDENTIAL_FILES`` is bound
    at import time and therefore points at the auth dir of whatever
    profile was active then, so filtering on ``p.parent == auth_dir()``
    would silently skip every file once the active profile captures the
    Logins bucket into its own ``auth/<profile>/`` slot.
    """
    for name in _known_credential_names():
        try:
            restore(name)
        except Exception:
            logger.debug("restore_all failed for %s", name, exc_info=True)


def delete(name: str) -> None:
    """Remove the keyring mirror for *name* (never raises).

    Runs regardless of the ``credential_keyring`` toggle: logout /
    uninstall must clear a mirror that a previously-enabled session may
    have written.
    """
    if not keyring_available():
        return
    _keyring_delete(name)


def delete_profile(profile: str) -> None:
    """Clear every mirrored credential entry owned by *profile*.

    Deleting a profile purges its auth dir, but the keyring copy lives
    in the platform credential store, not on disk - without this sweep
    the dead profile's live credentials (Spotify refresh tokens, Deezer
    ARL cookies, YT Music cookies) survive in the OS keyring forever.
    Entry names come from :func:`_known_credential_names`: ``keyring``
    cannot enumerate entries, so only app-declared credential files can
    be swept.

    Never raises; runs regardless of the ``credential_keyring`` toggle,
    exactly like :func:`delete`.
    """
    if not keyring_available():
        return
    for name in _known_credential_names():
        try:
            _keyring_delete(name, profile=profile)
        except Exception:
            logger.debug("Keyring sweep failed for %s of %s", name, profile)


def rename_profile(old: str, new: str) -> None:
    """Move *old*'s mirrored entries to the *new* profile prefix.

    Accounts are ``"<profile>:<file>"``, so a renamed profile resolves a
    different prefix: a mirror left under the old name is unreachable
    (the renamed profile can never restore from it) while still holding
    a live credential.  Copy-then-delete per entry - a failed write
    leaves the old entry in place rather than losing the only copy.

    Never raises.  A no-op when *old* == *new*.
    """
    if old == new or not keyring_available():
        return
    for name in _known_credential_names():
        try:
            payload = _keyring_get(name, profile=old)
            if payload is None:
                continue
            if _keyring_set(name, payload, profile=new):
                _keyring_delete(name, profile=old)
        except Exception:
            logger.debug("Keyring rename failed for %s: %s -> %s", name, old, new)