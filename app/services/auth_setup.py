"""
Authentication credential management.

Handles the credential-file lifecycle for YouTube Music and Spotify so
the UI layer only needs to call a function and handle the result.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from services import credential_store
from services import health_check
from services import profile_store as _profile_store

from utils.i18n import tr
from utils.logging_config import register_secret, user_log
from utils.platform import get_terminal_command, open_directory

logger = logging.getLogger(__name__)

AUTH_DIR = _profile_store.auth_dir()
SPOTIFY_FILE = AUTH_DIR / "spotify.json"
BROWSER_FILE = AUTH_DIR / "browser.json"
YT_MUSIC_FILE = AUTH_DIR / "yt_music.json"


def complete_browser_file(path: Path) -> bool:
    """True when *path* is a browser.json that ``YTMusic`` can consume.

    Valid JSON is not enough on its own - a headers file without the
    auth cookie constructs an UNAUTHORIZED client whose every browse
    fails.  The check mirrors ytmusicapi's own requirement (its setup
    refuses to write a file without ``cookie``).

    Auth-dir files observed complete are ALSO mirrored into the OS
    keyring (services/credential_store.py): the terminal flow writes
    browser.json itself, outside the app, so first observation is the
    only core-side write point for it.  Mirroring is self-quiescing
    (identical payloads are skipped) and a no-op without a backend.
    """
    try:
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        keys = {k.lower() for k in data}
        ready = bool(keys & {"cookie", "access_token"})
    except Exception:
        return False
    if ready and path.parent == AUTH_DIR:
        credential_store.mirror(path.name)
    return ready


def browser_file_ready() -> bool:
    """Whether a *complete, usable* browser.json exists.

    The auth manager (``YouTubeAuthManager._find_browser_file``) accepts
    the primary yt_music.json plus browser.json and the legacy fallback
    locations; the poll must mirror that, or a fallback copy would never
    trigger the refresh and the login would silently only apply on
    restart.  ``browser_file_candidates`` is the plugin's single source
    of truth for the lookup order.
    """
    try:
        from integrations.youtube_music.youtube_music import (
            browser_file_candidates,
        )
    except Exception:
        return complete_browser_file(YT_MUSIC_FILE) or complete_browser_file(
            BROWSER_FILE
        )
    return any(complete_browser_file(p) for p in browser_file_candidates())

# ---------------------------------------------------------------------------
# YouTube Music
# ---------------------------------------------------------------------------


def setup_ytmusic_auth() -> Dict[str, Any]:
    """Prepare the environment for ``ytmusicapi browser`` auth.

    Tries to launch a terminal first.  On success the auth folder is
    *not* opened (the terminal already sits in it).  When no terminal
    can be started, the auth folder is opened in the file manager as a
    manual fallback.

    Returns a dict with keys ``ok`` (bool) and ``manual`` (bool).
    When ``ok`` is ``False`` and ``manual`` is ``True`` the caller
    should show a manual-step message and the error detail.
    """
    AUTH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode only applies to a NEW directory - enforce it also on
    # an existing one (created with a looser umask, or by an earlier
    # version) so the credentials stay private.
    try:
        AUTH_DIR.chmod(0o700)
    except OSError:
        logger.debug("Could not enforce 0o700 on %s", AUTH_DIR)

    # Try the terminal first - the terminal's cwd is already AUTH_DIR so
    # the user can drop browser.json straight in.
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
        # Fall back to the file manager so the user can place
        # browser.json in place manually.
        open_directory(AUTH_DIR)
        return {
            "ok": False,
            "manual": True,
            "auth_dir": str(AUTH_DIR),
            "error": str(e),
        }


def save_and_verify_ytmusic_auth(
    authorization: str, cookie: str
) -> Dict[str, Any]:
    """Verify manually pasted YT Music headers and persist them.

    The values are assembled into the six-key headers dict ytmusicapi
    consumes directly (no ``headers`` wrapper) and verified against the
    live API FIRST - a real authenticated browse round trip - so a typo
    in the pasted values can never destroy previously working auth
    (VERIFY-FIRST, same contract as the Spotify/Last.fm/Deezer
    ``save_and_verify_*`` helpers).  Only a successful check writes
    ``yt_music.json`` via the plugin's atomic 0o600 writer.

    Runs a blocking network round trip (15s timeout): call from a worker
    thread, never the UI thread (PROJECT_RULES #198).

    Returns ``{"ok": True}`` or ``{"ok": False, "error": str}``.
    """
    authorization = (authorization or "").strip()
    cookie = (cookie or "").strip()
    # Mask these values in every log line from here on - even the failure
    # path below (a network error echo) must never print them.
    register_secret(authorization, cookie)
    if not authorization or not cookie:
        return {"ok": False, "error": tr("login.all_fields_required")}
    headers = {
        "Accept": "*/*",
        "Authorization": authorization,
        "Content-Type": "application/json",
        "X-Goog-AuthUser": "0",
        "x-origin": "https://music.youtube.com",
        "Cookie": cookie,
    }
    try:
        from integrations.youtube_music.youtube_music import (
            save_ytmusic_headers_file,
        )
        from ytmusicapi import YTMusic
    except ImportError:
        return {"ok": False, "error": tr("login.ytmusic_api_missing")}

    import requests

    session = requests.Session()
    # ytmusicapi has no per-call timeout knob; defaulting the timeout
    # at the session level keeps a dead network from hanging the verify.
    original_request = session.request
    session.request = lambda *args, **kw: original_request(
        *args, **{**kw, "timeout": kw.get("timeout", 15)}
    )
    try:
        YTMusic(headers, requests_session=session).get_library_playlists(
            limit=1
        )
    except Exception as e:
        logger.warning("YT Music credential verification failed: %s", e)
        return {"ok": False, "error": str(e)}

    try:
        save_ytmusic_headers_file(headers)
    except OSError as e:
        logger.error("Failed to save YT Music credentials: %s", e)
        return {"ok": False, "error": str(e)}
    credential_store.mirror("yt_music.json")
    logger.info("YT Music credentials verified and saved")
    return {"ok": True}

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
    register_secret(client_secret, refresh_token)
    try:
        from integrations.spotify.spotify import (
            save_spotify_credentials_file,
        )
    except ImportError:
        logger.warning("Spotify plugin not found - cannot save credentials")
        raise OSError(
            "Spotify plugin not found - cannot save credentials"
        ) from None

    save_spotify_credentials_file(client_id, client_secret, refresh_token)
    # Mirror the freshly-written file into the OS keyring (best-effort).
    credential_store.mirror("spotify.json")


def load_spotify_credentials() -> Dict[str, str]:
    """Load existing Spotify credentials from disk.

    A missing file is first re-materialized from the OS keyring mirror
    (when present), so a lost/copied auth dir can be rebuilt.  Returns
    an empty dict when no file exists - even after a restore attempt -
    or on parse failure.
    """
    if not SPOTIFY_FILE.exists():
        credential_store.restore("spotify.json")
        if not SPOTIFY_FILE.exists():
            return {}
    try:
        data = json.loads(SPOTIFY_FILE.read_text(encoding="utf-8"))
        register_secret(
            data.get("client_secret", ""),
            data.get("refresh_token", ""),
        )
        return data
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
            credential_store.delete("spotify.json")  # clear the keyring mirror
            user_log(logger, tr("auth.deleted_spotify"))
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
    register_secret(client_secret, refresh_token)
    result = verify_spotify_credentials(client_id, client_secret, refresh_token)
    if result.get("ok"):
        try:
            save_spotify_credentials(
                client_id,
                client_secret,
                result.get("refresh_token") or refresh_token,
            )
        except OSError as e:
            # Match the Last.fm/SoundCloud/Deezer contract: verification
            # succeeded but the write failed - report it as a failure so
            # the CLI login handler can surface the error instead of
            # crashing on an unhandled exception.
            result = {"ok": False, "error": f"Failed to save credentials: {e}"}
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
    """Remove the Last.fm credential file(s).

    Delegates to :func:`delete_platform_credentials` so the delete path
    stays a single source of truth (the ``PLATFORM_CREDENTIAL_FILES`` map
    plus any manifest-declared fallback files) instead of a second
    hardcoded copy of the path.

    Returns ``True`` when at least one file was deleted, ``False`` when
    none existed.  Raises ``OSError`` on a failed unlink (same contract
    as :func:`delete_spotify_credentials`).
    """
    deleted, _ = delete_platform_credentials("lastfm")
    return bool(deleted)


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
    register_secret(api_key, api_secret)
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
            credential_store.mirror("lastfm.json")
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
# SoundCloud
# ---------------------------------------------------------------------------


def load_soundcloud_credentials() -> Dict[str, str]:
    """Load existing SoundCloud credentials from disk.

    Delegates to :func:`integrations.soundcloud.soundcloud
    .load_soundcloud_credentials` so the plugin owns the read contract.
    Returns an empty dict when the plugin is missing or the file does
    not exist.
    """
    try:
        from integrations.soundcloud.soundcloud import (
            load_soundcloud_credentials as load_impl,
        )
        result = load_impl()
        return result if result else {}
    except ImportError:
        logger.warning("SoundCloud plugin not found - cannot load credentials")
        return {}


def delete_soundcloud_credentials() -> bool:
    """Remove the SoundCloud credential file.

    Returns ``True`` when the file was deleted, ``False`` when it
    did not exist.
    """
    try:
        from integrations.soundcloud.soundcloud import (
            delete_soundcloud_credentials as delete_impl,
        )
        deleted = delete_impl()
        if deleted:
            credential_store.delete("soundcloud.json")  # clear the keyring mirror
        return deleted
    except ImportError:
        logger.warning("SoundCloud plugin not found - cannot delete credentials")
        return False


def verify_soundcloud_credentials(
    client_id: str, client_secret: str, refresh_token: str
) -> Dict[str, Any]:
    """Test if SoundCloud credentials are valid by calling ``/me``.

    Delegates to the plugin's ``verify_soundcloud_credentials`` so the
    API client logic lives in the plugin.

    Returns a dict with keys ``ok`` (bool) and either ``display_name``
    or ``error``.
    """
    try:
        from integrations.soundcloud.soundcloud import (
            verify_soundcloud_credentials as verify_impl,
        )
        me, api = verify_impl(client_id, client_secret, refresh_token)
        if me:
            name = me.get("username") or me.get("full_name") or me.get("id") or ""
            return {"ok": True, "display_name": str(name)}
        return {"ok": False, "error": "Authentication failed"}
    except ImportError:
        return {"ok": False, "error": "SoundCloud plugin not found"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def save_and_verify_soundcloud_credentials(
    client_id: str, client_secret: str, refresh_token: str
) -> Dict[str, Any]:
    """Verify SoundCloud credentials and persist them if they are valid.

    Verification runs FIRST: only a successful ``/me`` round trip
    persists the credentials (verify-first pattern, repo rule #69).

    Delegates the actual save to the plugin's
    ``save_and_verify_soundcloud_credentials``.

    Returns the same dict as :func:`verify_soundcloud_credentials`.
    """
    register_secret(client_secret, refresh_token)
    try:
        from integrations.soundcloud.soundcloud import (
            save_and_verify_soundcloud_credentials as save_impl,
        )
        result = save_impl(client_id, client_secret, refresh_token)
        if result.get("ok"):
            credential_store.mirror("soundcloud.json")
        return result
    except ImportError:
        return {"ok": False, "error": "SoundCloud plugin not found"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Deezer
# ---------------------------------------------------------------------------

DEEZER_FILE = AUTH_DIR / "deezer.json"


def verify_deezer_credentials(arl: str) -> tuple:
    """Verify a Deezer ARL with a live Pipe API round trip.

    Returns ``(me, api)`` or ``(None, None)`` on failure.  The *api*
    is a temporary :class:`DeezerAPI` whose state carried through the
    check.
    """
    register_secret(arl)
    try:
        from integrations.deezer.deezer import (
            verify_deezer_credentials as verify_impl,
        )
        return verify_impl(arl)
    except ImportError:
        return None, None
    except Exception as e:
        logger.error("Deezer credential verification failed: %s", e)
        return None, None


def save_and_verify_deezer_credentials(arl: str) -> dict:
    """VERIFY-FIRST credential save for the CLI ``--login deezer`` path.

    The plugin declares ``login_module``/``login_class`` for the GUI tile,
    so this is the CLI entrypoint that calls into the plugin's own
    verify-first helper.
    """
    register_secret(arl)
    try:
        from integrations.deezer.deezer import (
            save_and_verify_deezer_credentials as save_impl,
        )
        result = save_impl(arl)
        if result.get("ok"):
            credential_store.mirror("deezer.json")
        return result
    except ImportError:
        return {"ok": False, "error": "Deezer plugin not found"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Multi-platform credential deletion (CLI --logout)
# ---------------------------------------------------------------------------

# platform id -> credential file(s) deleted on logout, used as the
# fallback when the manifest cannot be read (plugin dir missing or a
# broken plugin.json).  The primary source is the plugin manifest: the
# auth-dir file declared via ``auth_file`` plus any ``auth_file_fallbacks``
# (legacy repo-root copies), resolved by PluginInfo.auth_paths.
PLATFORM_CREDENTIAL_FILES = {
    "youtube_music": [YT_MUSIC_FILE, BROWSER_FILE],
    "spotify": [SPOTIFY_FILE],
    "lastfm": [AUTH_DIR / "lastfm.json"],
    "soundcloud": [AUTH_DIR / "soundcloud.json"],
    "deezer": [DEEZER_FILE],
}


def _credential_paths(plugin_registry, platform: str) -> List[Path]:
    """Every file that may hold *platform*'s credentials.

    Hardcoded map entries (known platforms, from
    :data:`PLATFORM_CREDENTIAL_FILES`) first, then the manifest-declared
    paths (``PluginInfo.auth_paths``: auth-dir file + declared repo-root
    fallbacks) appended when the plugin is present.  Deleting all of them
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
            # Only auth-dir files are ever mirrored; legacy repo-root
            # fallbacks have no keyring entry to clear.
            if path.parent == AUTH_DIR:
                credential_store.delete(path.name)
            user_log(logger, tr("auth.deleted_credential_path", path=path))
            deleted.append(path)
        else:
            missing.append(path)
    return deleted, missing


def credential_path_status(path: Path) -> Dict[str, Any]:
    """Inspect one credential path for the Settings credentials panel.

    Returns ``{"exists": bool, "mode": str | None, "private": bool}``:

    * ``mode`` - octal permission string ("0600") when the path exists;
    * ``private`` - True when neither group nor other has any access bit
      set.  Credential files are written 0600 and the auth dir is enforced
      0700, so "not private" means the path is looser than the app wrote
      it (a restoring umask, a hand edit, a shared copy) and worth
      surfacing in the panel.

    Never raises: a missing or unreadable path reports ``exists`` False.
    """
    try:
        st = path.stat()
    except OSError:
        return {"exists": False, "mode": None, "private": False}
    mode = st.st_mode & 0o777
    return {
        "exists": True,
        "mode": f"{mode:04o}",
        "private": (mode & 0o077) == 0,
    }
