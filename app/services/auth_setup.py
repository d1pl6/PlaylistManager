"""
Authentication credential management.

Extracted from ``app/ui/login_ui.py`` (Issue #2).  Handles the
credential-file lifecycle for YouTube Music and Spotify so the UI
layer only needs to call a function and handle the result.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from platformdirs import user_config_dir

from utils.logging_config import user_log
from utils.platform import get_terminal_command, open_directory

logger = logging.getLogger(__name__)

AUTH_DIR = Path(user_config_dir("playlistmanager")) / "auth"
SPOTIFY_FILE = AUTH_DIR / "spotify.json"
BROWSER_FILE = AUTH_DIR / "browser.json"

# ---------------------------------------------------------------------------
# YouTube Music
# ---------------------------------------------------------------------------


def setup_ytmusic_auth() -> Dict[str, Any]:
    """Prepare the environment for ``ytmusicapi browser`` auth.

    Returns a dict with keys ``ok`` (bool) and ``manual`` (bool).
    When ``ok`` is ``False`` and ``manual`` is ``True`` the caller
    should show a manual-step message.
    """
    AUTH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    open_directory(AUTH_DIR)

    try:
        terminal_cmd = get_terminal_command(AUTH_DIR)
        subprocess.Popen(
            terminal_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "manual": False}
    except (FileNotFoundError, OSError) as e:
        logger.error("Failed to open terminal: %s", e)
        return {
            "ok": False,
            "manual": True,
            "auth_dir": str(AUTH_DIR),
            "error": str(e),
        }

# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------


def save_spotify_credentials(
    client_id: str, client_secret: str, refresh_token: str
) -> None:
    """Write Spotify credentials to disk with secure permissions.

    Delegates to :func:`integrations.spotify.spotify
    .save_spotify_credentials_file` so the token-refresh path (which
    lives on :class:`SpotifyAPI`) and the login UI write through the
    same single writer.

    Raises ``OSError`` on write failure.
    """
    from integrations.spotify.spotify import (
        save_spotify_credentials_file,
    )

    save_spotify_credentials_file(client_id, client_secret, refresh_token)


def load_spotify_credentials() -> Dict[str, str]:
    """Load existing Spotify credentials from disk.

    Returns an empty dict when no file exists or on parse failure.
    """
    if not SPOTIFY_FILE.exists():
        return {}
    try:
        return json.loads(SPOTIFY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load Spotify credentials: %s", e)
        return {}


def delete_spotify_credentials() -> bool:
    """Remove the Spotify credential file.

    Returns ``True`` if the file was deleted, ``False`` if there was
    nothing to delete.
    """
    if SPOTIFY_FILE.exists():
        try:
            SPOTIFY_FILE.unlink()
            user_log(logger, "Deleted Spotify credentials")
            return True
        except OSError as e:
            logger.error("Failed to delete Spotify credentials: %s", e)
            raise
    return False


def verify_spotify_credentials(
    client_id: str, client_secret: str, refresh_token: str
) -> Dict[str, Any]:
    """Test if Spotify credentials are valid by calling ``/v1/me``.

    Delegates to :meth:`SpotifyAuthManager.verify_credentials` so the
    credential-verification knowledge lives on the auth manager, not in
    this service.

    Returns a dict with keys ``ok`` (bool) and either ``display_name``
    or ``error``.  On success the dict also carries ``refresh_token``:
    the *final* refresh token after the verification round trip (when
    the app opts into Spotify's token rotation, the entered token is
    invalidated during the check and replaced).
    """
    try:
        from integrations.spotify.spotify import SpotifyAuthManager

        me, api = SpotifyAuthManager.verify_credentials(
            client_id, client_secret, refresh_token
        )
        if me and me.get("display_name"):
            return {
                "ok": True,
                "display_name": me["display_name"],
                "refresh_token": api.refresh_token,
            }
        return {"ok": False, "error": "Authentication failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def save_and_verify_spotify_credentials(
    client_id: str, client_secret: str, refresh_token: str
) -> Dict[str, Any]:
    """Verify Spotify credentials and persist them if they are valid.

    Verification runs FIRST: only a successful ``/v1/me`` round trip
    writes the credential file, so a typo in the login form can no
    longer destroy previously working auth (the old save-then-verify
    ordering overwrote the file before the credential check could
    fail).

    The persisted token is the *final* one reported by the verification
    (``result["refresh_token"]``) - NOT the raw input.  Spotify's
    optional refresh-token rotation invalidates the entered token during
    the check, and persisting the dead token would break auth at the
    next launch.  A failed verify never writes anything.

    Returns the same dict as :func:`verify_spotify_credentials`.
    """
    result = verify_spotify_credentials(client_id, client_secret, refresh_token)
    if result.get("ok"):
        save_spotify_credentials(
            client_id,
            client_secret,
            result.get("refresh_token") or refresh_token,
        )
    return result


# ---------------------------------------------------------------------------
# Last.fm
# ---------------------------------------------------------------------------

def load_lastfm_credentials() -> Dict[str, str]:
    """Load existing Last.fm credentials from disk.

    Delegates to :func:`integrations.lastfm.lastfm.load_lastfm_credentials`
    so the plugin owns the read contract (single-writer lives in the
    plugin; the app keeps only the static delete path here).  Returns an
    empty dict when the plugin is missing or the file does not exist.
    """
    try:
        from integrations.lastfm.lastfm import load_lastfm_credentials as load_impl
        return load_impl()
    except ImportError:
        logger.warning("Last.fm plugin not found - cannot load credentials")
        return {}


def validate_lastfm_credentials(api_key: str, api_secret: str) -> Dict[str, Any]:
    """Validate Last.fm api_key/secret WITHOUT opening a browser or waiting
    for authorization.

    Delegates to :func:`integrations.lastfm.lastfm.validate_api_credentials`
    (a signed ``auth.getToken`` round trip - a bad key or secret fails
    fast).  Used by the login dialog's Test button; Save runs the full
    web-auth flow (:func:`verify_lastfm_credentials`).

    Returns ``{"ok": True}`` or ``{"ok": False, "error": ...}``.
    """
    try:
        from integrations.lastfm.lastfm import validate_api_credentials as validate_impl
        return validate_impl(api_key, api_secret)
    except ImportError:
        return {
            "ok": False,
            "error": "Last.fm plugin not found",
        }
    except Exception as e:
        logger.error("Last.fm credential validation failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


def delete_lastfm_credentials() -> bool:
    """Remove the Last.fm credential file.

    Returns ``True`` when the file was deleted, ``False`` when it
    did not exist.
    """
    lastfm_file = AUTH_DIR / "lastfm.json"
    if lastfm_file.exists():
        try:
            lastfm_file.unlink()
            user_log(logger, "Deleted Last.fm credentials")
            return True
        except Exception as e:
            logger.error("Failed to delete Last.fm credentials: %s", e)
            return False
    return False


def verify_lastfm_credentials(api_key: str, api_secret: str) -> Dict[str, Any]:
    """Test if Last.fm credentials are valid by calling auth.getSession.

    Delegates to :func:`integrations.lastfm.lastfm.verify_lastfm_credentials`
    so the Last.fm client logic and session-key fetch stays in the plugin.

    Returns a dict with keys:
        - ``ok`` (bool): True if verification succeeded.
        - ``username`` (str): Last.fm username (when ok is True).
        - ``display_name`` (str): Display name for the user (when ok is True).
        - ``error`` (str): Error message (when ok is False).
    """
    try:
        from integrations.lastfm.lastfm import verify_lastfm_credentials as verify_impl
        return verify_impl(api_key, api_secret)
    except ImportError:
        return {
            "ok": False,
            "error": "Last.fm plugin not found",
        }
    except Exception as e:
        logger.error("Last.fm verification failed: %s", e, exc_info=True)
        return {
            "ok": False,
            "error": str(e),
        }


def save_and_verify_lastfm_credentials(api_key: str, api_secret: str) -> Dict[str, Any]:
    """Verify Last.fm credentials and persist them if they are valid.

    Verification runs FIRST: only a successful verification round trip
    persists the credentials (verify-first pattern, repo rule #69).

    Delegates the actual save to :func:`integrations.lastfm.lastfm
    .save_lastfm_credentials_file` so the plugin owns the write-through
    contract.

    Returns the same dict as :func:`verify_lastfm_credentials`.
    """
    result = verify_lastfm_credentials(api_key, api_secret)
    if result.get("ok"):
        try:
            from integrations.lastfm.lastfm import save_lastfm_credentials_file
            save_lastfm_credentials_file(
                api_key,
                api_secret,
                result.get("session_key", ""),
                result.get("username", ""),
            )
        except ImportError:
            logger.error("Last.fm plugin not found; credentials not saved")
            return {
                "ok": False,
                "error": "Last.fm plugin not found",
            }
        except Exception as e:
            logger.error("Failed to save Last.fm credentials: %s", e, exc_info=True)
            return {
                "ok": False,
                "error": f"Failed to save: {e}",
            }
    return result


# ---------------------------------------------------------------------------
# Multi-platform credential deletion (CLI --logout)
# ---------------------------------------------------------------------------

# platform id -> credential file(s) deleted on logout, used as the
# fallback when the manifest cannot be read (plugin dir missing or a
# broken plugin.json).  The primary source is the plugin manifest: the
# auth-dir file declared via ``auth_file`` plus any ``auth_file_fallbacks``
# (legacy repo-root copies), resolved by PluginInfo.auth_paths.
PLATFORM_CREDENTIAL_FILES = {
    "youtube_music": [BROWSER_FILE],
    "spotify": [SPOTIFY_FILE],
    "lastfm": [AUTH_DIR / "lastfm.json"],
}


def _credential_paths(plugin_registry, platform: str) -> List[Path]:
    """Every file that may hold *platform*'s credentials.

    Manifest-declared paths (``PluginInfo.auth_paths``: auth-dir file +
    declared repo-root fallbacks) first, then the hardcoded map entries
    for the known platforms as defense in depth.  Deleting all of them
    is what makes logout / uninstall complete - a surviving fallback
    browser.json would otherwise re-authenticate the platform on the
    next login.
    """
    paths: List[Path] = list(PLATFORM_CREDENTIAL_FILES.get(platform, []))
    if plugin_registry is not None:
        plugin = plugin_registry.get(platform)
        if plugin is not None:
            for manifest_path in plugin.auth_paths:
                if manifest_path not in paths:
                    paths.append(manifest_path)
    return paths


def delete_platform_credentials(
    platform: str, plugin_registry=None
) -> Tuple[List[Path], List[Path]]:
    """Delete every credential file listed for *platform*.

    Returns ``(deleted, missing)`` - the files that were removed and the
    ones that did not exist.  Raises ``OSError`` on the first failed
    unlink so callers can report the error.

    *plugin_registry*, when given, supplies the manifest-declared paths
    (auth-dir file + ``auth_file_fallbacks``); without it the hardcoded
    :data:`PLATFORM_CREDENTIAL_FILES` map applies.
    """
    deleted: List[Path] = []
    missing: List[Path] = []
    for path in _credential_paths(plugin_registry, platform):
        if path.exists():
            path.unlink()
            user_log(logger, "Deleted credentials: %s", path)
            deleted.append(path)
        else:
            missing.append(path)
    return deleted, missing
