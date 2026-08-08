"""
Headless CLI: add the currently-playing song to playlists from the terminal.

Entry points (all equivalent):
    python -m app add 1,2,3
    python -m app add 1-3 "Chill Mix"
    python -m app -a 1,2 --add "Chill Mix"
    python -m app --list

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

from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC
from services.playlist_store import PlaylistStore
from services.song_manager import SongManager

logger = logging.getLogger(__name__)

URL_WAIT_TIMEOUT = 30


class UsageError(Exception):
    """Bad CLI input — mapped to exit code 2 (argparse's usage-error code)."""


# ---------------------------------------------------------------------------
# Target parsing: "1,2,3" / "1-3" / names / mixed
# ---------------------------------------------------------------------------


def _parse_token(token: str) -> Tuple[str, object]:
    """
    Classify one comma-separated token.

    Returns ("number", int) for pure digits, ("range", (start, end)) for
    "N-M", ("name", str) otherwise. Numeric-looking playlist names are always
    treated as order numbers (documented in README).
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
    return "name", token


def resolve_targets(
    spec: str, playlists: List[dict]
) -> List[Tuple[int, dict]]:
    """
    Resolve an add-spec ("1,2,3", "1-3", names, or a mix) to playlist entries.

    Returns a list of (registry_number, entry) — registry_number is the 1-based
    display order the user sees in the GUI (what ``--list`` prints). Repeated
    targets are silently deduped by (platform, playlist_id), first occurrence
    kept.

    Raises UsageError on malformed, out-of-range, unknown or ambiguous input.
    """
    if not spec.strip():
        raise UsageError(
            "No playlists given — e.g. 'add 1,2,3' or 'add \"Chill Mix\"'"
        )
    total = len(playlists)
    resolved: List[Tuple[int, dict]] = []
    seen = set()

    def add(entry: dict, number: int) -> None:
        key = (entry.get("platform"), entry.get("playlist_id"))
        if key in seen:
            return
        seen.add(key)
        resolved.append((number, entry))

    for token in spec.split(","):
        kind, value = _parse_token(token)
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
        else:
            name = value
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
        logger.info("ytmusicapi not installed — YouTube Music integration disabled")
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
        logger.warning("Spotify not configured (no spotify.json)")
    except Exception as e:
        logger.error(f"Spotify auth failed: {e}", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run_list() -> int:
    """Print the numbered playlist registry (CLI numbers = GUI display order)."""
    playlists = PlaylistStore.load_playlists()
    if not playlists:
        print(
            "No playlists configured. Add playlists from the GUI first.",
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
            "No playlists configured. Add playlists from the GUI first.",
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
    if yt_targets and yt is not None:
        from controllers.keybind_flow import KeybindFlowController
        from integrations.music_youtube.music_youtube_receiver import (
            URLReceiverManager,
        )

        receiver = URLReceiverManager()
        keybind_flow = KeybindFlowController(yt, song_manager, receiver)
        yt_url, yt_song_data = _capture_yt_song(keybind_flow, receiver)

    sp_flow = None
    if sp_targets and sp_integration is not None:
        from controllers.keybind_flow import SpotifyFlowController

        sp_flow = SpotifyFlowController(sp_integration, song_manager)

    successes = 0
    failures = 0
    for number, entry in targets:
        platform = entry.get("platform")
        name = entry.get("name")
        if platform == PLATFORM_YOUTUBE_MUSIC:
            if keybind_flow is None:
                ok, message = False, (
                    "Error: YouTube Music not configured — run the GUI auth setup "
                    "first (ytmusicapi must be installed)"
                )
            elif yt_url is None:
                ok, message = False, "Error: Timeout: no URL received"
            else:
                ok, message = _run_flow(
                    keybind_flow, name, url=yt_url, song_data=yt_song_data
                )
        elif platform == PLATFORM_SPOTIFY:
            if sp_flow is None:
                ok, message = False, (
                    "Error: Spotify not configured — run the GUI auth setup first"
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


def _capture_yt_song(keybind_flow, receiver) -> Tuple[Optional[str], Optional[Dict]]:
    """
    Capture the current YouTube Music URL from the browser extension and fetch
    its song details. Returns (url, song_data), or (None, None) on failure.
    """
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
        logger.error("Timed out waiting for the YouTube Music URL")
        return None, None
    except Exception as e:
        logger.error(f"Failed to capture the YouTube Music URL: {e}", exc_info=True)
        return None, None
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

    from integrations.music_youtube.music_youtube_receiver import (
        URLReceiverManager,
    )

    video_id = URLReceiverManager._extract_video_id(url)
    if video_id is None:
        logger.error(f"Could not extract a video ID from '{url}'")
        return None, None
    try:
        song_data = keybind_flow.fetch_song_details(video_id)
    except Exception as e:
        logger.error(f"Failed to fetch song details: {e}", exc_info=True)
        return None, None
    return url, song_data


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
