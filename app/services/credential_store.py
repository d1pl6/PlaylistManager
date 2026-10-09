"""
OS-keyring mirror for platform credentials (credentials at rest).

The OS keyring holds an encrypted MIRROR of each auth-dir credential file:

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

Platform-conditional transitive chain (``SecretStorage`` ->
``cryptography`` + ``jeepney`` on Linux)
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

_available: bool | None = None
_reason: str = ""
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Backend probing
# ---------------------------------------------------------------------------


def _backend():
    """
    Return the active keyring backend (may raise).
    """
    import keyring

    return keyring.get_keyring()


def _is_usable_backend(kr) -> bool:
    """
    True when *kr* is an OS-backed keyring, not a placeholder.
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
    """
    Functional write/read/delete round trip against the keyring.
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


# Log text per probe failure reason.
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
    """
    Whether a usable OS keyring backend is present (cached probe).

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
    from utils.config import get_setting_value

    return (
        get_setting_value("security", "credential_keyring", "on").strip().lower()
        != "off"
    )


# ---------------------------------------------------------------------------
# Paths and account naming
# ---------------------------------------------------------------------------


def auth_file(name: str) -> Path:
    """
    Full path of *name* inside the profile-aware auth dir.

    *name* must be a bare filename - same traversal rejection as
    ``PluginInfo.auth_path``: slashes or ``..`` segments are refused.
    """
    if not name or PurePosixPath(name).name != name:
        raise ValueError(f"credential name {name!r} must be a bare filename")
    return _profile_store.auth_dir() / name


def _account(name: str, profile: str | None = None) -> str:
    """
    OS-keyring account name: isolates profiles from each other.
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
    """
    Basenames of every auth-dir credential file the app declares.
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
    """
    Copy a credential file's payload into the OS keyring.
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
    """
    Re-materialize a MISSING credential file from the OS keyring.
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
        fd, tmp = tempfile.mkstemp(dir=str(auth_dir), prefix=name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
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
    """
    Restore every missing auth-dir credential file from the keyring.
    """
    for name in _known_credential_names():
        try:
            restore(name)
        except Exception:
            logger.debug("restore_all failed for %s", name, exc_info=True)


def delete(name: str) -> None:
    """
    Remove the keyring mirror for *name* (never raises).
    """
    if not keyring_available():
        return
    _keyring_delete(name)


def delete_profile(profile: str) -> None:
    """
    Clear every mirrored credential entry owned by *profile*.
    """
    if not keyring_available():
        return
    for name in _known_credential_names():
        try:
            _keyring_delete(name, profile=profile)
        except Exception:
            logger.debug("Keyring sweep failed for %s of %s", name, profile)


def rename_profile(old: str, new: str) -> None:
    """
    Move *old*'s mirrored entries to the *new* profile prefix.
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