"""
Headless CLI: add the currently-playing song to playlists and manage the
playlist registry from the terminal.

Entry points (all equivalent):
    python -m app -a 1,2,3
    python -m app --add-song "Chill Mix"
    python -m app -p add "https://music.youtube.com/playlist?list=PL..."
    python -m app -p del 1,"Chill Mix"
    python -m app -p ref 1,"Chill Mix"
    python -m app --list
    python -m app --login youtube_music
    python -m app --login spotify --client-id X --client-secret Y --refresh-token Z
    python -m app --logout youtube_music
    python -m app --logout spotify

Designed for compositor-owned shortcuts (i3 / sway / hyprland / KDE / GNOME
binds) — the Wayland-safe replacement for pynput global hotkeys (plan.md W1,
Option A). The compositor grabs the key and runs this command; no global input
capture is needed.

The add pipeline is the same headless flow the GUI uses: auth from the on-disk
credential files, song source via the browser extension (YouTube Music) or the
Spotify API, platform-API-first writes. No tkinter is involved.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Dict, List, Optional, Tuple

from constants import (
    KNOWN_PLATFORMS,
    PLATFORM_SPOTIFY,
    PLATFORM_YOUTUBE_MUSIC,
)
from integrations.music_youtube.music_youtube_receiver import URLReceiverManager
from services import auth_setup
from services.database import DatabaseManager
from services.playlist_store import PlaylistStore
from services.playlist_sync import PlaylistSyncService
from services.playlist_url import parse_playlist_url
from services.song_manager import SongManager
from utils.logging_config import user_log
from utils.thumbnail import ThumbnailService

logger = logging.getLogger(__name__)

URL_WAIT_TIMEOUT = 30


class UsageError(Exception):
    """Bad CLI input — mapped to exit code 2 (argparse's usage-error code)."""


# ---------------------------------------------------------------------------
# Target parsing: "1,2,3" / "1-3" / names / mixed
# ---------------------------------------------------------------------------


def _parse_token(token: str, allow_urls: bool = False) -> Tuple[str, object]:
    """
    Classify one comma-separated token.

    Returns ("number", int) for pure digits, ("range", (start, end)) for
    "N-M", ("url", (platform, playlist_id)) for a playlist URL when
    *allow_urls* is true, ("name", str) otherwise. Numeric-looking playlist
    names are always treated as order numbers (documented in README).
    """
    token = token.strip()
    if re.fullmatch(r"\d+", token):
        return "number", int(token)
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if start > end:
            raise UsageError(f"Invalid range '{token}': start is greater than end")
        return "range", (start, end)
    if allow_urls and (
        token.startswith("http://")
        or token.startswith("https://")
        or token.startswith("spotify:")
    ):
        try:
            return "url", parse_playlist_url(token)
        except ValueError as e:
            raise UsageError(str(e))
    return "name", token


def resolve_targets(
    spec: str, playlists: List[dict], allow_urls: bool = False
) -> List[Tuple[int, dict]]:
    """
    Resolve a playlist spec ("1,2,3", "1-3", names, or a mix) to entries.

    Returns a list of (registry_number, entry) — registry_number is the 1-based
    display order the user sees in the GUI (what ``--list`` prints). Repeated
    targets are silently deduped by (platform, playlist_id), first occurrence
    kept.

    *allow_urls* enables URL tokens, resolved against the registry by
    (platform, playlist_id) — used by the del/ref commands. The song-add path
    leaves it False so a URL there falls through to name lookup and fails
    with "not found".

    Raises UsageError on malformed, out-of-range, unknown or ambiguous input.
    """
    if not spec.strip():
        raise UsageError(
            "No playlists given - e.g. '1,2,3', '1-3', or '\"Chill Mix\"'"
        )
    total = len(playlists)
    resolved: List[Tuple[int, dict]] = []
    seen = set()

    def add(entry: dict, number: int) -> None:
        # Legacy entries may lack playlist_id (and even the platform field -
        # the store defaults those to YouTube Music).  Key on
        # (platform, playlist_id or name) so distinct legacy playlists are
        # not collapsed into a single dedup key.
        key = (
            entry.get("platform") or PLATFORM_YOUTUBE_MUSIC,
            entry.get("playlist_id") or entry.get("name"),
        )
        if key in seen:
            return
        seen.add(key)
        resolved.append((number, entry))

    for token in spec.split(","):
        kind, value = _parse_token(token, allow_urls=allow_urls)
        if kind == "number":
            if not 1 <= value <= total:
                raise UsageError(
                    f"Playlist #{value} out of range — valid: 1–{total}"
                )
            add(playlists[value - 1], value)
        elif kind == "range":
            start, end = value
            if start < 1 or end > total:
                raise UsageError(
                    f"Playlist range {start}–{end} out of range — valid: 1–{total}"
                )
            for number in range(start, end + 1):
                add(playlists[number - 1], number)
        elif kind == "url":
            platform, playlist_id = value
            matches = [
                p
                for p in playlists
                if p.get("platform") == platform
                and p.get("playlist_id") == playlist_id
            ]
            if not matches:
                raise UsageError(
                    "Playlist URL not in the registry - add it first with "
                    "'playlistmanager -p add <URL>'"
                )
            entry = matches[0]
            add(entry, playlists.index(entry) + 1)
        else:
            name = value
            # Prefer exact matches - only fall back to case-insensitive
            # lookup when nothing matches exactly.  Otherwise a playlist
            # sharing a case-variant name on another platform would turn an
            # otherwise unambiguous name into a false "ambiguous" error.
            matches = [p for p in playlists if p.get("name") == name]
            if not matches:
                matches = [
                    p
                    for p in playlists
                    if str(p.get("name", "")).lower() == name.lower()
                ]
            if not matches:
                raise UsageError(
                    f"Playlist '{name}' not found — run 'playlistmanager --list' "
                    "to see available playlists"
                )
            if len(matches) > 1:
                candidates = ", ".join(
                    f'#{i + 1} "{p.get("name")}" ({p.get("platform")})'
                    for i, p in enumerate(playlists)
                    if p in matches
                )
                raise UsageError(
                    f"Playlist '{name}' is ambiguous ({candidates}) — use numbers"
                )
            entry = matches[0]
            add(entry, playlists.index(entry) + 1)
    return resolved


def resolve_targets_for(
    spec: str, playlists: List[dict]
) -> List[Tuple[int, dict]]:
    """Resolve a del/ref spec: the ``all`` keyword, URLs, or a normal spec."""
    if spec.strip().lower() == "all":
        return [(i + 1, p) for i, p in enumerate(playlists)]
    return resolve_targets(spec, playlists, allow_urls=True)


# ---------------------------------------------------------------------------
# Auth bootstrap (mirrors App.__init__ in app/app.py)
# ---------------------------------------------------------------------------


def _init_yt_music():
    """Return an authenticated YTMusic client, or None if unavailable."""
    try:
        from integrations.music_youtube.music_youtube import youtube_auth

        if youtube_auth.setup_auth():
            return youtube_auth.get_yt_music()
        logger.warning("YouTube Music not configured (no browser.json)")
    except ImportError:
        user_log(
            logger, "ytmusicapi not installed - YouTube Music integration disabled"
        )
    except Exception as e:
        logger.error(f"YouTube Music auth failed: {e}", exc_info=True)
    return None


def _init_spotify():
    """Return an authenticated SpotifyIntegration, or None if unavailable."""
    try:
        from integrations.music_spotify.music_spotify import spotify_auth
        from services.integration import SpotifyIntegration

        if spotify_auth.setup_auth():
            integration = SpotifyIntegration(auth_manager=spotify_auth)
            integration.spotify_api = spotify_auth.get_api()
            return integration
        logger.warning("Spotify is not configured (no spotify.json)")
    except Exception as e:
        logger.error(f"Spotify auth failed: {e}", exc_info=True)
    return None


def _auth_error(platform: str) -> str:
    """Message for an unconfigured platform, matching the song-add path."""
    if platform == PLATFORM_YOUTUBE_MUSIC:
        return (
            "YouTube Music not configured - run the GUI auth setup first "
            "(ytmusicapi must be installed)"
        )
    return f"{platform} not configured - run the GUI auth setup first"


def _build_integrations():
    """Return an IntegrationRegistry with the authenticated integrations.

    Mirrors App.__init__ (app.py:33-75) headlessly: no tkinter, no
    messageboxes. Integrations without credentials stay registered with no
    client so PlaylistSyncService can still report a per-platform error.
    """
    from services.integration import IntegrationRegistry, YouTubeMusicIntegration

    registry = IntegrationRegistry()

    yt_client = _init_yt_music()
    yt_integration = YouTubeMusicIntegration()
    yt_integration.yt_client = yt_client
    registry.register(yt_integration)

    sp_integration = _init_spotify()
    if sp_integration is not None:
        registry.register(sp_integration)

    return registry


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run_list() -> int:
    """Print the numbered playlist registry (CLI numbers = GUI display order)."""
    playlists = PlaylistStore.load_playlists()
    if not playlists:
        print(
            "No playlists configured. Add one with "
            "'playlistmanager -p add <URL>' or from the GUI.",
            file=sys.stderr,
        )
        return 2
    for i, playlist in enumerate(playlists, 1):
        print(f'{i}. "{playlist.get("name")}" ({playlist.get("platform")})')
    return 0


def run_add(spec: str) -> int:
    """Add the currently-playing song to the requested playlists."""
    playlists = PlaylistStore.load_playlists()
    if not playlists:
        print(
            "No playlists configured. Add one with "
            "'playlistmanager -p add <URL>' or from the GUI.",
            file=sys.stderr,
        )
        return 2

    try:
        targets = resolve_targets(spec, playlists)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    song_manager = SongManager()

    yt_targets = [t for t in targets if t[1].get("platform") == PLATFORM_YOUTUBE_MUSIC]
    sp_targets = [t for t in targets if t[1].get("platform") == PLATFORM_SPOTIFY]

    yt = _init_yt_music() if yt_targets else None
    sp_integration = _init_spotify() if sp_targets else None

    # Capture ONE YouTube Music URL (+ song details) and share it across all
    # YT playlists — the browser extension delivers it to the receiver.
    keybind_flow = None
    yt_url = None
    yt_song_data = None
    yt_error = ""
    if yt_targets and yt is not None:
        from controllers.keybind_flow import KeybindFlowController

        receiver = URLReceiverManager()
        keybind_flow = KeybindFlowController(yt, song_manager, receiver)
        yt_url, yt_song_data, yt_error = _capture_yt_song(keybind_flow, receiver)

    sp_flow = None
    if sp_targets and sp_integration is not None:
        from controllers.keybind_flow import SpotifyFlowController

        sp_flow = SpotifyFlowController(sp_integration, song_manager)

    successes = 0
    failures = 0
    for number, entry in targets:
        platform = entry.get("platform") or PLATFORM_YOUTUBE_MUSIC
        name = entry.get("name")
        if platform == PLATFORM_YOUTUBE_MUSIC:
            if keybind_flow is None:
                ok, message = False, (
                    "Error: YouTube Music not configured — run the GUI auth setup "
                    "first (ytmusicapi must be installed)"
                )
            elif yt_url is None:
                ok, message = False, f"Error: {yt_error or 'Timeout: no URL received'}"
            else:
                ok, message = _run_flow(
                    keybind_flow, name, url=yt_url, song_data=yt_song_data
                )
        elif platform == PLATFORM_SPOTIFY:
            if sp_flow is None:
                ok, message = False, (
                    "Error: Spotify is not configured — run the GUI auth setup first"
                )
            else:
                ok, message = _run_flow(sp_flow, name)
        else:
            ok, message = False, f"Error: unsupported platform '{platform}'"

        line = f'#{number} "{name}" ({platform}): {message}'
        if ok:
            print(line, flush=True)
            successes += 1
        else:
            print(line, file=sys.stderr, flush=True)
            failures += 1

    return 0 if successes else 1


def run_add_url(url: str) -> int:
    """Register a playlist from its URL and import its tracks."""
    try:
        platform, playlist_id = parse_playlist_url(url)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    integrations = _build_integrations()
    integration = integrations.get(platform)
    if integration is None or not integration.is_authenticated():
        print(f"error: {_auth_error(platform)}", file=sys.stderr)
        return 1

    # Platform-first: never register a playlist the platform cannot
    # confirm exists (deleted, private, or an arbitrary bad URL).
    try:
        details = integration.get_playlist_details(playlist_id)
    except Exception as e:
        print(f"error: failed to fetch playlist details: {e}", file=sys.stderr)
        return 1
    name = details.get("title") if isinstance(details, dict) else None
    if not name:
        print(
            f"error: playlist not found on {platform} "
            "(deleted, private, or an invalid URL)",
            file=sys.stderr,
        )
        return 1

    thumb_url = ThumbnailService.from_data(details)
    thumb_url = PlaylistSyncService.prefer_library_thumbnail(
        platform, integration, playlist_id, thumb_url
    )

    existed = (
        PlaylistStore.find_playlist(name, platform=platform, playlist_id=playlist_id)
        is not None
    )
    PlaylistStore.add_playlist(
        name,
        platform=platform,
        playlist_id=playlist_id,
        thumbnail_url=thumb_url or "",
    )

    playlists = PlaylistStore.load_playlists()
    number = next(
        (
            i + 1
            for i, p in enumerate(playlists)
            if p.get("platform") == platform
            and p.get("playlist_id") == playlist_id
        ),
        len(playlists),
    )

    action = "Updated" if existed else "Added"
    sync = PlaylistSyncService(integrations)
    try:
        inserted, status = sync.import_tracks_sync(name, platform, playlist_id)
    except Exception as e:
        print(
            f'#{number} "{name}" ({platform}): {action}, track import failed: {e}',
            file=sys.stderr,
        )
        return 1

    import_note = f"imported {inserted} tracks" if inserted else status.lower()
    print(f'#{number} "{name}" ({platform}): {action}, {import_note}', flush=True)
    return 0


def run_del(spec: str) -> int:
    """Remove playlist(s) from the registry and delete their local DBs.

    Local-only: never touches the playlist on the platform.
    """
    playlists = PlaylistStore.load_playlists()
    if not playlists:
        print("No playlists configured.", file=sys.stderr)
        return 2

    try:
        targets = resolve_targets_for(spec, playlists)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for number, entry in targets:
        name = entry.get("name")
        # Legacy entries may lack the platform field - default to YT like
        # PlaylistStore does, so delete_playlist_db never sees None.
        platform = entry.get("platform") or PLATFORM_YOUTUBE_MUSIC
        PlaylistStore.delete_playlist(
            name, platform=platform, playlist_id=entry.get("playlist_id", "")
        )
        DatabaseManager.delete_playlist_db(name, platform)
        print(f'#{number} "{name}" ({platform}): deleted', flush=True)
    return 0


def run_refresh(spec: str) -> int:
    """Re-import all tracks for playlist(s) from the platform."""
    playlists = PlaylistStore.load_playlists()
    if not playlists:
        print("No playlists configured.", file=sys.stderr)
        return 2

    try:
        targets = resolve_targets_for(spec, playlists)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    integrations = _build_integrations()
    sync = PlaylistSyncService(integrations)

    failures = 0
    for number, entry in targets:
        name = entry.get("name")
        platform = entry.get("platform") or PLATFORM_YOUTUBE_MUSIC
        playlist_id = entry.get("playlist_id", "")
        if not playlist_id:
            print(
                f'#{number} "{name}" ({platform}): Error: no playlist_id - '
                "re-add it with 'playlistmanager -p add <URL>'",
                file=sys.stderr,
            )
            failures += 1
            continue

        DatabaseManager.close_thread_connections()
        try:
            inserted, status, _thumb = sync.reload_database_sync(
                name, platform, playlist_id
            )
        except Exception as e:
            print(f'#{number} "{name}" ({platform}): Error: {e}', file=sys.stderr)
            failures += 1
            continue

        print(
            f'#{number} "{name}" ({platform}): refreshed ({status.lower()})',
            flush=True,
        )
    return 0 if failures == 0 else 1


def run_login(
    platform: str,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    refresh_token: Optional[str] = None,
) -> int:
    """Log in to a platform. Returns the CLI exit code.

    youtube_music: mirrors the GUI tile - opens the auth folder and a
    terminal running ``ytmusicapi browser`` (manual instructions instead
    when no terminal emulator is installed).

    spotify: requires ``--client-id`` / ``--client-secret`` /
    ``--refresh-token``.  The file is written first, then verified
    against ``/v1/me`` - on verify failure the just-written file is
    rolled back and an error is shown (cli.md 15.12.4).
    """
    if platform not in KNOWN_PLATFORMS:
        print(
            f"error: unknown platform '{platform}' "
            f"(use {', '.join(KNOWN_PLATFORMS)})",
            file=sys.stderr,
        )
        return 2

    if platform == PLATFORM_YOUTUBE_MUSIC:
        result = auth_setup.setup_ytmusic_auth()
        if result.get("manual"):
            print(
                f"youtube_music: no terminal emulator found - manually run:\n"
                f"  cd {result['auth_dir']}\n"
                f"  ytmusicapi browser\n"
                f"and place the generated browser.json in {result['auth_dir']}",
                file=sys.stderr,
            )
            return 0
        print(
            f"youtube_music: opened {auth_setup.AUTH_DIR} and a terminal - "
            "run 'ytmusicapi browser' there and keep the generated "
            "browser.json in that folder",
            flush=True,
        )
        return 0

    # Spotify
    missing = [
        name
        for name, val in (
            ("--client-id", client_id),
            ("--client-secret", client_secret),
            ("--refresh-token", refresh_token),
        )
        if not val
    ]
    if missing:
        print(
            f"error: '--login spotify' requires {' '.join(missing)} "
            "(the refresh token comes from your Spotify app's dashboard)",
            file=sys.stderr,
        )
        return 2

    auth_setup.save_spotify_credentials(client_id, client_secret, refresh_token)
    result = auth_setup.verify_spotify_credentials(
        client_id, client_secret, refresh_token
    )
    if result.get("ok"):
        print(f"spotify: logged in as {result.get('display_name')}", flush=True)
        return 0

    try:
        auth_setup.delete_spotify_credentials()
    except OSError as e:
        logger.warning("Failed to roll back spotify.json after failed verify: %s", e)
    print(
        f"error: spotify login failed: {result.get('error')} "
        "(credentials rolled back)",
        file=sys.stderr,
    )
    return 1


def run_logout(platform: str) -> int:
    """Log out of a platform: delete its credentials, registry entries and
    local databases. Local-only - never touches the platform itself.

    Idempotent: a second run with nothing left prints "no credentials
    found" and still exits 0.
    """
    if platform not in KNOWN_PLATFORMS:
        print(
            f"error: unknown platform '{platform}' "
            f"(use {', '.join(KNOWN_PLATFORMS)})",
            file=sys.stderr,
        )
        return 2

    try:
        deleted, _missing = auth_setup.delete_platform_credentials(platform)
    except OSError as e:
        print(
            f"error: failed to delete {platform} credentials: {e}",
            file=sys.stderr,
        )
        return 1

    if deleted:
        print(
            f"{platform}: logged out (deleted {', '.join(str(p) for p in deleted)})",
            flush=True,
        )
    else:
        print(f"{platform}: no credentials found", flush=True)

    n_registry = PlaylistStore.delete_playlists_for_platform(platform)
    if n_registry:
        print(f"{platform}: removed {n_registry} playlist(s) from the registry")

    n_dbs = DatabaseManager.delete_platform_databases(platform)
    if n_dbs:
        print(f"{platform}: deleted {n_dbs} local database file(s)")
    return 0


def _capture_yt_song(
    keybind_flow, receiver
) -> Tuple[Optional[str], Optional[Dict], str]:
    """
    Capture the current YouTube Music URL from the browser extension and fetch
    its song details. Returns ``(url, song_data, error)`` - on failure both
    url and song_data are None and *error* describes what went wrong ("" on
    success).
    """
    url = None
    try:
        receiver.start()
        receiver.set_waiting(True)
        print(
            "Waiting for YouTube Music URL... "
            "(play the song in the browser with the extension installed)",
            flush=True,
        )
        url = receiver.get_received_url(timeout=URL_WAIT_TIMEOUT)
    except TimeoutError:
        return None, None, "Timeout: no URL received from the browser extension"
    except Exception as e:
        logger.error(f"Failed to capture the YouTube Music URL: {e}", exc_info=True)
        return None, None, f"failed to start the URL receiver: {e}"
    finally:
        try:
            receiver.set_waiting(False)
        except Exception:
            pass
        try:
            if receiver.is_running():
                receiver.stop()
        except Exception:
            pass

    video_id = URLReceiverManager._extract_video_id(url)
    if video_id is None:
        logger.error(f"Could not extract a video ID from '{url}'")
        return None, None, "could not extract a video ID from the received URL"
    try:
        song_data = keybind_flow.fetch_song_details(video_id)
    except Exception as e:
        logger.error(f"Failed to fetch song details: {e}", exc_info=True)
        return None, None, f"failed to fetch song details: {e}"
    return url, song_data, ""


def _run_flow(flow, playlist_name: str, url=None, song_data=None) -> Tuple[bool, str]:
    """Run one add-flow; returns (ok, message) — message is the line tail."""
    outcome = {"ok": None, "message": ""}

    def on_status(msg: str) -> None:
        logger.debug(f"[{playlist_name}] {msg}")

    def on_error(msg: str) -> None:
        outcome["ok"] = False
        outcome["message"] = msg

    def on_success(result: Dict) -> None:
        outcome["ok"] = True
        outcome["message"] = result.get("message", "Added")

    try:
        flow.execute_flow(
            playlist_name,
            on_status,
            on_error,
            on_success,
            url=url,
            song_data=song_data,
        )
    except Exception as e:
        logger.error(f"Flow failed for '{playlist_name}': {e}", exc_info=True)
        outcome["ok"] = False
        outcome["message"] = f"Error: {e}"
    return outcome["ok"] is True, outcome["message"]
