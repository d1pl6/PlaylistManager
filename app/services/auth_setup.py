"""
Authentication credential management.

Handles the credential-file lifecycle for YouTube Music and Spotify so
the UI layer only needs to call a function and handle the result.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from services import credential_store
from services import health_check
from services import profile_store as _profile_store

from utils.i18n import tr
from utils.logging_config import register_secret, user_log
from utils.platform import get_terminal_command, open_directory

logger = logging.getLogger(__name__)

AUTH_DIR = _profile_store.auth_dir()
SPOTIFY_FILE = AUTH_DIR / "spotify.json"
BROWSER_FILE = AUTH_DIR / "browser.json" # youtube music
YT_MUSIC_FILE = AUTH_DIR / "yt_music.json"
DEEZER_FILE = AUTH_DIR / "deezer.json"
TIDAL_FILE = AUTH_DIR / "tidal.json"


def complete_browser_file(path: Path) -> bool:
    """
    True when *path* is a browser.json that ``YTMusic`` can consume.

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
    """
    Whether a *complete, usable* browser.json exists.
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
    """
    Prepare the environment for ``ytmusicapi browser`` auth.
    """
    AUTH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode only applies to a NEW directory - enforce it also on
    # an existing one (created with a looser umask, or by an earlier
    # version) so the credentials stay private.
    try:
        AUTH_DIR.chmod(0o700)
    except OSError:
        logger.debug("Could not enforce 0o700 on %s", AUTH_DIR)

    # Try the terminal first
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
    """
    Verify manually pasted YT Music headers and persist them.

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
    """
    Write Spotify credentials to disk with secure permissions.
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
    # Mirror the freshly-written file into the OS keyring.
    credential_store.mirror("spotify.json")


def load_spotify_credentials() -> Dict[str, str]:
    """
    Load existing Spotify credentials from disk.
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
    """
    Test if Spotify credentials are valid by calling ``/v1/me``.
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
    """
    Verify Spotify credentials and persist them if they are valid.
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
            result = {"ok": False, "error": f"Failed to save credentials: {e}"}
    return result


# ---------------------------------------------------------------------------
# Last.fm
# ---------------------------------------------------------------------------

def load_lastfm_credentials() -> Dict[str, str]:
    """
    Load existing Last.fm credentials from disk.
    """
    try:
        from integrations.lastfm.lastfm import load_lastfm_credentials as load_impl
        return load_impl()
    except ImportError:
        logger.warning("Last.fm plugin not found - cannot load credentials")
        return {}


def validate_lastfm_credentials(api_key: str, api_secret: str) -> Dict[str, Any]:
    """
    Validate Last.fm api_key/secret WITHOUT opening a browser or waiting
    for authorization.
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
    """
    Remove the Last.fm credential file(s).
    """
    deleted, _ = delete_platform_credentials("lastfm")
    return bool(deleted)


def verify_lastfm_credentials(api_key: str, api_secret: str) -> Dict[str, Any]:
    """
    Test if Last.fm credentials are valid by calling auth.getSession.
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
    """
    Verify Last.fm credentials and persist them if they are valid.
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
# ListenBrainz
# ---------------------------------------------------------------------------


def load_listenbrainz_credentials() -> Dict[str, str]:
    """Load existing ListenBrainz credentials from disk.
    """
    try:
        from integrations.listenbrainz.listenbrainz import (
            load_listenbrainz_credentials as load_impl,
        )
        return load_impl()
    except ImportError:
        logger.warning(
            "ListenBrainz plugin not found - cannot load credentials"
        )
        return {}


def verify_listenbrainz_credentials(token: str) -> Dict[str, Any]:
    """
    Test a ListenBrainz user token against ``/1/validate-token``.
    """
    register_secret(token)
    try:
        from integrations.listenbrainz.listenbrainz import (
            verify_listenbrainz_credentials as verify_impl,
        )
        return verify_impl(token)
    except ImportError:
        return {
            "ok": False,
            "error": "ListenBrainz plugin not found",
        }
    except Exception as e:
        logger.error("ListenBrainz verification failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


def save_and_verify_listenbrainz_credentials(token: str) -> Dict[str, Any]:
    """
    Verify a ListenBrainz user token and persist it if it is valid.
    """
    register_secret(token)
    result = verify_listenbrainz_credentials(token)
    if result.get("ok"):
        try:
            from integrations.listenbrainz.listenbrainz import (
                save_listenbrainz_credentials_file,
            )

            save_listenbrainz_credentials_file(token, result.get("username", ""))
            credential_store.mirror("listenbrainz.json")
        except ImportError:
            logger.error("ListenBrainz plugin not found; credentials not saved")
            return {
                "ok": False,
                "error": "ListenBrainz plugin not found",
            }
        except Exception as e:
            logger.error("Failed to save ListenBrainz credentials: %s", e, exc_info=True)
            return {
                "ok": False,
                "error": f"Failed to save: {e}",
            }
    return result


def delete_listenbrainz_credentials() -> bool:
    deleted, _ = delete_platform_credentials("listenbrainz")
    return bool(deleted)


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------


def load_soundcloud_credentials() -> Dict[str, str]:
    """
    Load existing SoundCloud credentials from disk.
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
    """
    Test if SoundCloud credentials are valid by calling ``/me``.
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
    """
    Verify SoundCloud credentials and persist them if they are valid.
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
# Tidal
# ---------------------------------------------------------------------------


def load_tidal_credentials() -> Dict[str, str]:
    if not TIDAL_FILE.exists():
        credential_store.restore("tidal.json")
        if not TIDAL_FILE.exists():
            return {}
    try:
        from integrations.tidal.tidal import (
            load_tidal_credentials as load_impl,
        )
    except ImportError:
        logger.warning("Tidal plugin not found - cannot load credentials")
        return {}
    return load_impl() or {}


def tidal_browser_login(
    client_id: str,
    client_secret: str,
    on_url: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Run the plugin's Tidal browser login and persist it on success.

    Tidal auth is an OAuth authorization-code flow with PKCE: the plugin
    serves the loopback redirect, opens the browser and exchanges the code
    for a refresh token, which is then verified against ``/v1/sessions``
    """
    register_secret(client_secret)
    try:
        from integrations.tidal.tidal import (
            login_with_browser as login_impl,
        )
        result = login_impl(client_id, client_secret, on_url=on_url)
        if result.get("ok"):
            credential_store.mirror("tidal.json")
        return result
    except ImportError:
        return {"ok": False, "error": "Tidal plugin not found"}
    except Exception as e:
        logger.error("Tidal login failed: %s", e, exc_info=True)
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
    "listenbrainz": [AUTH_DIR / "listenbrainz.json"],
    "soundcloud": [AUTH_DIR / "soundcloud.json"],
    "deezer": [DEEZER_FILE],
    "tidal": [TIDAL_FILE],
}


def _credential_paths(plugin_registry, platform: str) -> List[Path]:
    """
    Every file that may hold platform's credentials.
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
    """
    Delete every credential file listed for platform.
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
    """
    Inspect one credential path for the Settings credentials panel.
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


# ---------------------------------------------------------------------------
# Credential-permission audit (startup check + Settings panel warning)
# ---------------------------------------------------------------------------


def audit_credential_permissions(
    plugin_registry=None,
) -> health_check.PermissionReport:
    """
    Audit the auth dir and every known credential file's permissions.
    """
    specs: List[Tuple[Path, int]] = [
        (_profile_store.auth_dir(), health_check.AUTH_DIR_MODE)
    ]
    for platform in PLATFORM_CREDENTIAL_FILES:
        try:
            paths = _credential_paths(plugin_registry, platform)
        except Exception as e:  # a broken manifest must not void the audit
            logger.debug("Skipping %s credentials in the audit: %s", platform, e)
            continue
        specs.extend((path, health_check.CREDENTIAL_FILE_MODE) for path in paths)
    if plugin_registry is not None:
        try:
            plugins = plugin_registry.get_all().values()
        except Exception as e:
            logger.debug("Credential audit could not read the plugin registry: %s", e)
            plugins = ()
        for plugin in plugins:
            try:
                declared = plugin.auth_paths
            except Exception as e:
                logger.debug("Credential audit skipped a plugin: %s", e)
                continue
            specs.extend(
                (path, health_check.CREDENTIAL_FILE_MODE)
                for path in (declared or ())
            )
    return health_check.audit_path_modes(specs)
