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
from typing import Any, Dict, List, Optional, Tuple

from platformdirs import user_config_dir

from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC
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
        subprocess.Popen(terminal_cmd)
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

    Delegates to :func:`integrations.music_spotify.music_spotify
    .save_spotify_credentials_file` so the token-refresh path (which
    lives on :class:`SpotifyAPI`) and the login UI write through the
    same single writer.

    Raises ``OSError`` on write failure.
    """
    from integrations.music_spotify.music_spotify import (
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
            logger.info("Deleted Spotify credentials")
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
    or ``error``.
    """
    try:
        from integrations.music_spotify.music_spotify import SpotifyAuthManager

        me = SpotifyAuthManager.verify_credentials(
            client_id, client_secret, refresh_token
        )
        if me and me.get("display_name"):
            return {"ok": True, "display_name": me["display_name"]}
        return {"ok": False, "error": "Authentication failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def save_and_verify_spotify_credentials(
    client_id: str, client_secret: str, refresh_token: str
) -> Dict[str, Any]:
    """Save Spotify credentials to disk and verify them.

    Convenience wrapper for the Save button flow.  Returns the same
    dict as :func:`verify_spotify_credentials`.
    """
    save_spotify_credentials(client_id, client_secret, refresh_token)
    return verify_spotify_credentials(client_id, client_secret, refresh_token)


# ---------------------------------------------------------------------------
# Multi-platform credential deletion (CLI --logout)
# ---------------------------------------------------------------------------

# platform -> primary credential file(s) deleted on logout.
# Deliberately only the auth-dir file - NOT the repo fallback browser.json
# copies (decision cli.md 15.12.2: CLI and GUI logins are never used
# simultaneously, so a stale fallback copy is not a practical concern).
PLATFORM_CREDENTIAL_FILES = {
    PLATFORM_YOUTUBE_MUSIC: [BROWSER_FILE],
    PLATFORM_SPOTIFY: [SPOTIFY_FILE],
}


def delete_platform_credentials(platform: str) -> Tuple[List[Path], List[Path]]:
    """Delete every credential file listed for *platform*.

    Returns ``(deleted, missing)`` - the files that were removed and the
    ones that did not exist.  Raises ``OSError`` on the first failed
    unlink so callers can report the error.

    Adding a future platform: add its identifier to
    ``constants.KNOWN_PLATFORMS`` and its credential file(s) to
    :data:`PLATFORM_CREDENTIAL_FILES`.
    """
    deleted: List[Path] = []
    missing: List[Path] = []
    for path in PLATFORM_CREDENTIAL_FILES.get(platform, []):
        if path.exists():
            path.unlink()
            logger.info("Deleted credentials: %s", path)
            deleted.append(path)
        else:
            missing.append(path)
    return deleted, missing
