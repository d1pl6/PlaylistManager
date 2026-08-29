"""
Headless CLI: add the currently-playing song to playlists and manage the
playlist registry from the terminal.

Entry points (all equivalent):
    python -m app -a 1,2,3
    python -m app --add-song "Chill Mix"
    python -m app -s                    # scrobble the currently-playing song
    python -m app -p add "https://music.youtube.com/playlist?list=PL..."
    python -m app -p del 1,"Chill Mix"
    python -m app -p ref 1,"Chill Mix"
    python -m app --list
    python -m app --login youtube_music
    python -m app --login spotify            # interactive: prompts for the credentials
    python -m app --login spotify --client-id X --client-secret Y --refresh-token Z
    python -m app --logout youtube_music
    python -m app --logout spotify
    python -m app --install spotify            # download/install a platform plugin
    python -m app --install all                # install every supported platform
    python -m app --uninstall spotify          # remove a platform + all its local data
    python -m app --uninstall all              # remove every installed platform

Designed for compositor-owned shortcuts (i3 / sway / hyprland / KDE / GNOME
binds) - the Wayland-safe replacement for pynput global keybinds (plan.md W1,
Option A). The compositor grabs the key and runs this command; no global input
capture is needed.

The add pipeline is the same headless flow the GUI uses: auth from the on-disk
credential files, song source via the browser extension (YouTube Music) or the
Spotify API, platform-API-first writes. No tkinter is involved.
"""

from __future__ import annotations

import getpass
import json
import logging
import re
import sys
from typing import Dict, List, Optional, Tuple

from plugin_loader import PluginRegistry, get_default_registry
from services import auth_setup, integration_manager
from services.database import DatabaseManager
from services.integration import IntegrationRegistry
from services.playlist_store import PlaylistStore
from services.playlist_sync import PlaylistSyncService
from services.playlist_url import parse_playlist_url
from services.song_manager import SongManager
from utils.config import get_setting
from utils.logging_config import user_log
from utils.thumbnail import ThumbnailService

logger = logging.getLogger(__name__)

URL_WAIT_TIMEOUT = 30


class UsageError(Exception):
    """Bad CLI input - mapped to exit code 2 (argparse's usage-error code)."""


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
    if allow_urls:
        lowered = token.lower()
        if lowered.startswith(("http://", "https://", "spotify:")):
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

    Returns a list of (registry_number, entry) - registry_number is the 1-based
    display order the user sees in the GUI (what ``--list`` prints). Repeated
    targets are silently deduped by (platform, playlist_id), first occurrence
    kept.

    *allow_urls* enables URL tokens, resolved against the registry by
    (platform, playlist_id) - used by the del/ref commands. The song-add path
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
            entry.get("platform") or "youtube_music",
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
                    f"Playlist #{value} out of range - valid: 1-{total}"
                )
            add(playlists[value - 1], value)
        elif kind == "range":
            start, end = value
            if start < 1 or end > total:
                raise UsageError(
                    f"Playlist range {start}-{end} out of range - valid: 1-{total}"
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
                    f"Playlist '{name}' not found - run 'playlistmanager --list' "
                    "to see available playlists"
                )
            if len(matches) > 1:
                candidates = ", ".join(
                    f'#{i + 1} "{p.get("name")}" ({p.get("platform")})'
                    for i, p in enumerate(playlists)
                    if p in matches
                )
                raise UsageError(
                    f"Playlist '{name}' is ambiguous ({candidates}) - use numbers"
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
# Auth bootstrap (generic - driven by the plugin manifests)
# ---------------------------------------------------------------------------


def _init_platform(plugin_registry, platform_id: str):
    """Authenticate one platform via its plugin. Returns the integration
    with a live client, or ``None`` when unavailable/unconfigured."""
    plugin = plugin_registry.get(platform_id)
    if plugin is None:
        return None
    try:
        auth_manager = None
        if plugin.auth_module:
            auth_manager = plugin.import_auth_attr()
        integration_cls = plugin.import_integration()
    except Exception as e:
        logger.error(
            "%s integration unavailable: %s", platform_id, e, exc_info=True
        )
        return None

    integration = integration_cls(auth_manager=auth_manager)
    try:
        if not integration.authenticate():
            logger.warning(
                "%s is not configured - run the GUI auth setup first",
                plugin.display_name,
            )
            return None
    except Exception as e:
        logger.error("%s auth failed: %s", plugin.display_name, e, exc_info=True)
        return None
    return integration


def _auth_error(plugin) -> str:
    """Message for an unconfigured platform, matching the song-add path."""
    return f"{plugin.display_name} not configured - run the GUI auth setup first"


def _build_integrations():
    """Return an IntegrationRegistry covering every discovered plugin.

    Mirrors App.__init__ headlessly: no tkinter, no messageboxes.  Every
    loadable integration is registered; consumers gate on
    ``is_authenticated()`` so an unconfigured or failed platform simply
    reports a per-platform error instead of disappearing.
    """
    registry = IntegrationRegistry()
    plugin_registry = get_default_registry()
    for pid, plugin in plugin_registry.get_all().items():
        try:
            auth_manager = None
            if plugin.auth_module:
                auth_manager = plugin.import_auth_attr()
            integration_cls = plugin.import_integration()
            registry.register(integration_cls(auth_manager=auth_manager))
        except Exception as e:
            user_log(
                logger, "%s integration unavailable (%s)", plugin.display_name, e
            )
    # Authenticate after registration - authenticate() is a network round
    # trip and must never keep a broken plugin from being registered.
    for integration in registry.get_all().values():
        try:
            integration.authenticate()
        except Exception as e:
            logger.error(
                "%s auth failed: %s", integration.display_name, e, exc_info=True
            )
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

    plugin_registry = get_default_registry()
    song_manager = SongManager()
    integrations = _build_integrations()

    # Legacy registry entries predate the "platform" field - default them
    # to YouTube Music, matching PlaylistStore.
    targets_by_platform: Dict[str, list] = {}
    for number, entry in targets:
        platform = entry.get("platform") or "youtube_music"
        targets_by_platform.setdefault(platform, []).append((number, entry))

    # Per platform: authenticate once, build its flow via the plugin
    # manifest, capture the current song ONCE and share it across all of
    # that platform's target playlists.
    contexts: Dict[str, dict] = {}
    for platform, group in targets_by_platform.items():
        ctx = {"flow": None, "url": None, "song_data": None, "error": ""}
        contexts[platform] = ctx

        plugin = plugin_registry.get(platform)
        if plugin is None or not plugin.flow_class:
            ctx["error"] = f"unsupported platform '{platform}'"
            continue
        integration = _init_platform(plugin_registry, platform)
        if integration is None:
            ctx["error"] = _auth_error(plugin)
            continue
        try:
            flow_cls = plugin.import_flow()
            if plugin.flow_type == "extension":
                flow = flow_cls(
                    integration, song_manager, plugin.build_receiver()
                )
            else:
                # "api" type - reads the platform directly, no receiver.
                flow = flow_cls(integration, song_manager)
            ctx["flow"] = flow
            ctx["url"], ctx["song_data"], ctx["error"] = flow.capture(URL_WAIT_TIMEOUT)
        except Exception as e:
            logger.error("%s flow/capture failed: %s", platform, e, exc_info=True)
            ctx["flow"] = None
            ctx["error"] = str(e)

    failures = 0
    for number, entry in targets:
        platform = entry.get("platform") or "youtube_music"
        name = entry.get("name")
        ctx = contexts[platform]
        if ctx["flow"] is None:
            ok, message = False, f"Error: {ctx['error']}"
        elif plugin_registry.get(platform).flow_type == "extension" and ctx["url"] is None:
            ok, message = False, f"Error: {ctx['error'] or 'Timeout: no URL received'}"
        elif ctx["song_data"] is None:
            ok, message = False, f"Error: {ctx['error'] or 'Nothing playing'}"
        else:
            ok, message = _run_flow(
                ctx["flow"], name, url=ctx["url"], song_data=ctx["song_data"],
                playlist_id=entry.get("playlist_id") or None, integrations=integrations,
            )

        line = f'#{number} "{name}" ({platform}): {message}'
        if ok:
            print(line, flush=True)
        else:
            print(line, file=sys.stderr, flush=True)
            failures += 1

    return 0 if failures == 0 else 1


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

    # Non-editable playlists (followed, not collaborative) can be tracked
    # read-only, but adding songs to them fails on the platform - warn.
    if platform == "spotify" and isinstance(details, dict):
        try:
            user_id = (
                integration.spotify_api.get_user_id()
                if integration.spotify_api
                else None
            )
        except Exception:
            user_id = None
        if (
            user_id
            and details.get("owner_id")
            and details.get("owner_id") != user_id
            and not details.get("collaborative")
        ):
            print(
                f"warning: you are not the owner or a collaborator of "
                f"'{name}' - adding songs to it will fail on Spotify's side",
                file=sys.stderr,
            )

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
        platform = entry.get("platform") or "youtube_music"
        PlaylistStore.delete_playlist(
            name, platform=platform, playlist_id=entry.get("playlist_id", "")
        )
        DatabaseManager.delete_playlist_db(
            name,
            platform,
            playlist_id=entry.get("playlist_id", ""),
        )
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
        platform = entry.get("platform") or "youtube_music"
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


def run_scrobble() -> int:
    """Scrobble the currently-playing song without adding to any playlist.

    For ``api``-type platforms (Spotify), reads the currently-playing track
    directly.  For ``extension``-type platforms (YouTube Music), captures
    once via the browser extension (30 s wait).  Requires a
    ``ScrobbleCapable`` integration (Last.fm) to be loaded and
    authenticated.

    Composable with compositor shortcuts:
        playlistmanager -s
    """
    plugin_registry = get_default_registry()
    song_manager = SongManager()
    integrations = _build_integrations()

    # Find the ScrobbleCapable backend (Last.fm).
    scrobble_integ = next(
        (
            integ
            for integ in integrations.get_all().values()
            if getattr(integ, "scrobble", None) is not None
        ),
        None,
    )
    if scrobble_integ is None:
        print("error: no scrobble backend available (is Last.fm configured?)",
              file=sys.stderr)
        return 1

    # Try each authenticated platform's capture path until one produces a
    # song — mirrors keybind_controller._scrobble_current_action.
    for platform_id, integration in integrations.get_all().items():
        if not integration.is_authenticated():
            continue

        plugin = plugin_registry.get(platform_id)
        if plugin is None or not plugin.flow_class:
            continue

        try:
            flow_cls = plugin.import_flow()
            if plugin.flow_type == "extension":
                flow = flow_cls(integration, song_manager, plugin.build_receiver())
            else:
                flow = flow_cls(integration, song_manager)

            _url, song_data, error = flow.capture(URL_WAIT_TIMEOUT)
            if song_data:
                if scrobble_integ.scrobble(song_data):
                    title = song_data.get("title", "unknown")
                    artist = (song_data.get("artists") or ["unknown"])[0]
                    print(f"Scrobbled: {artist} - {title}", flush=True)
                    return 0
                print(f"error: scrobble failed for "
                      f"{song_data.get('title', 'unknown')}", file=sys.stderr)
                return 1
            # Nothing playing on this platform — try the next.
            logger.debug("No song playing on %s: %s", platform_id, error)
        except Exception as e:
            logger.debug("Capture failed on %s: %s", platform_id, e)
            continue

    print("error: no currently-playing song found", file=sys.stderr)
    return 1


def _stored_refresh_token() -> Optional[str]:
    """Read the refresh token from the saved spotify.json, if any.

    Used by the interactive login as the "default" for the refresh-token
    prompt - pressing Enter re-logs-in with the stored token (e.g. when
    only the client credentials changed).
    """
    try:
        plugin = get_default_registry().get("spotify")
        if plugin is None or not plugin.auth_path:
            return None
        data = json.loads(plugin.auth_path.read_text(encoding="utf-8"))
        return data.get("refresh_token") or None
    except (OSError, ValueError):
        return None


def _prompt(label: str, hidden: bool = False) -> Optional[str]:
    """Interactive credential prompt (getpass = hidden, like sudo).

    Returns None when the input stream is closed (EOFError) so non-
    interactive invocations fail cleanly instead of crashing.
    """
    try:
        if hidden:
            return getpass.getpass(label).strip()
        return input(label).strip()
    except EOFError:
        return None


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

    spotify: ``--login spotify`` alone prompts interactively (client id,
    client secret, refresh token - secret/token hidden like sudo).  The
    flags remain as overrides for scripting/compositor use.  The refresh
    token is long-lived (the 3600 s default is the *access* token's
    lifetime); leaving the prompt empty reuses the stored token.
    Credentials are verified against ``/v1/me`` FIRST and only persisted
    on success (the verify itself writes the refreshed - possibly rotated
    - refresh token through the canonical writer).  A typo'd login can no
    longer overwrite and then destroy previously working auth, matching
    the GUI's save_and_verify ordering
    (auth_setup.save_and_verify_spotify_credentials).
    """
    known = get_default_registry().get_platform_ids()
    if platform not in known:
        print(
            f"error: unknown platform '{platform}' (use {', '.join(known)})",
            file=sys.stderr,
        )
        return 2

    if platform == "youtube_music":
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

    if platform == "lastfm":
        # Last.fm auth needs the browser-authorization + auth.getSession
        # flow that only the GUI login dialog drives (api key + secret, then
        # approve in the browser).  No useful non-interactive path.
        print(
            "lastfm: GUI-only at the moment - open the app's Settings -> "
            "Login/Accounts -> Last.fm, enter your API key + secret, and "
            "approve the browser authorization",
            file=sys.stderr,
        )
        return 2

    # Spotify.  Flags override; missing values are prompted interactively
    # (hidden input for the secret and the token, like sudo).
    if not client_id:
        client_id = _prompt("Client id: ")
    if not client_secret:
        client_secret = _prompt("Client secret: ", hidden=True)
    if not refresh_token:
        stored = _stored_refresh_token()
        prompt_value = _prompt("Refresh token (leave empty to reuse stored): ", hidden=True)
        refresh_token = prompt_value or stored
    if not client_id or not client_secret or not refresh_token:
        print(
            "error: client id, client secret and a refresh token are all "
            "required (the refresh token comes from your Spotify app's "
            "dashboard)",
            file=sys.stderr,
        )
        return 2

    # Verify FIRST, persist only on success - see the docstring.  The
    # failed-login rollback (delete_spotify_credentials) is gone because
    # nothing was ever written; previously valid credentials survive a
    # typo intact.
    result = auth_setup.save_and_verify_spotify_credentials(
        client_id, client_secret, refresh_token
    )
    if result.get("ok"):
        print(f"spotify: logged in as {result.get('display_name')}", flush=True)
        return 0

    print(
        f"error: spotify login failed: {result.get('error')} "
        "(existing credentials left untouched)",
        file=sys.stderr,
    )
    return 1


def run_logout(platform: str) -> int:
    """Log out of a platform: delete its credentials, registry entries and
    local databases. Local-only - never touches the platform itself.

    Idempotent: a second run with nothing left prints "no credentials
    found" and still exits 0.
    """
    known = get_default_registry().get_platform_ids()
    if platform not in known:
        print(
            f"error: unknown platform '{platform}' (use {', '.join(known)})",
            file=sys.stderr,
        )
        return 2

    try:
        deleted, _missing = auth_setup.delete_platform_credentials(
            platform, plugin_registry=get_default_registry()
        )
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


def _resolve_platform_arg(platform: str) -> List[str]:
    """Expand a --install/--uninstall argument into platform ids.

    ``all`` expands to every catalog platform (downloadable); otherwise the
    id is validated against the catalog.  Returns the ids or raises
    ``UsageError`` (mapped to exit code 2).
    """
    catalog = integration_manager.installable_ids()
    if platform == "all":
        return catalog
    if platform not in catalog:
        raise UsageError(
            f"unknown platform '{platform}' (use {', '.join(catalog)})"
        )
    return [platform]


def run_install(platform: str) -> int:
    """Download and install one (or ``all``) platform plugin(s).

    Writes only the plugin directory under ``integrations/``; it does not
    authenticate (see ``--login``) or register any playlists.  Idempotent
    with respect to already-installed platforms - an existing install is
    replaced with a fresh copy.
    """
    try:
        targets = _resolve_platform_arg(platform)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    failures: List[str] = []
    for pid in targets:
        display = integration_manager.INTEGRATION_REPOS[pid].display_name
        missing = PluginRegistry().base_dir / pid
        already = missing.is_dir()
        try:
            integration_manager.download_integration(pid)
            print(
                f"{pid}: {'updated' if already else 'installed'} ({display})",
                flush=True,
            )
        except Exception as e:
            logger.exception("Install failed for %s", pid)
            print(f"error: failed to install {pid}: {e}", file=sys.stderr)
            failures.append(pid)

    if failures:
        return 1
    return 0


def run_uninstall(platform: str) -> int:
    """Remove one (or ``all``) platform plugin(s) and all their local data.

    Like the GUI's per-platform uninstall: deletes credentials, playlist
    registry entries, song databases, duplicate-queue records and the
    plugin directory itself - the online playlists are untouched.  The
    CLI has no running app (no receiver/listener to stop), so only the
    disk-level uninstall step applies.  Idempotent: a platform that is not
    installed is reported as such and not an error.
    """
    catalog = integration_manager.installable_ids()
    if platform == "all":
        registry = get_default_registry()
        targets = [
            pid for pid in catalog
            if (PluginRegistry().base_dir / pid).is_dir()
            or registry.get(pid) is not None
        ]
    else:
        if platform not in catalog:
            print(
                f"error: unknown platform '{platform}' "
                f"(use {', '.join(catalog)})",
                file=sys.stderr,
            )
            return 2
        targets = [platform]

    failures: List[str] = []
    for pid in targets:
        registry = get_default_registry()
        plugin = registry.get(pid)
        if plugin is None and not (PluginRegistry().base_dir / pid).is_dir():
            print(f"{pid}: not installed (nothing to uninstall)")
            continue
        try:
            report = integration_manager.uninstall_platform_data(
                pid, plugin=plugin
            )
            parts = [
                f"{report['credentials']} credential(s)",
                f"{report['playlists']} playlist(s)",
                f"{report['databases']} DB(s)",
                f"{report['plugin_dirs']} folder(s)",
            ]
            print(
                f"{pid}: uninstalled ({', '.join(parts)})",
                flush=True,
            )
        except Exception as e:
            logger.exception("Uninstall failed for %s", pid)
            print(f"error: failed to uninstall {pid}: {e}", file=sys.stderr)
            failures.append(pid)

    if failures:
        return 1
    return 0


def _run_flow(
    flow, playlist_name: str, url=None, song_data=None, playlist_id=None, integrations=None
) -> Tuple[bool, str]:
    """Run one add-flow; returns (ok, message) - message is the line tail."""
    outcome = {"ok": None, "message": ""}

    def on_status(msg: str) -> None:
        logger.debug("[%s] %s", playlist_name, msg)

    def on_error(msg: str) -> None:
        outcome["ok"] = False
        outcome["message"] = msg

    def on_success(result: Dict) -> None:
        status = result.get("status")
        outcome["ok"] = True
        if status == "duplicate":
            # Queued in db/extra.json, added nowhere - the CLI keeps
            # going (continue-on-error policy); resolution happens in
            # the GUI's activity window.
            outcome["message"] = (
                f"{result.get('message', 'maybe-duplicate')} "
                "[queued as maybe-duplicate - resolve in the GUI]"
            )
        else:
            outcome["message"] = result.get("message", "Added")

        # Scrobble the song if auto-scrobble is enabled and a ScrobbleCapable
        # integration is available.
        if status == "added" and get_setting("scrobble_on_add"):
            song_data = result.get("song", {})
            if song_data and integrations:
                try:
                    scrobble_integ = next(
                        (integ for integ in integrations.get_all().values()
                         if getattr(integ, "scrobble", None) is not None),
                        None,
                    )
                    if scrobble_integ is not None:
                        scrobble_integ.scrobble(song_data)
                except Exception as e:
                    logger.debug("Failed to scrobble song: %s", e)

    try:
        flow.execute_flow(
            playlist_name,
            on_status,
            on_error,
            on_success,
            url=url,
            song_data=song_data,
            playlist_id=playlist_id,
        )
    except Exception as e:
        logger.error("Flow failed for '%s': %s", playlist_name, e, exc_info=True)
        outcome["ok"] = False
        outcome["message"] = f"Error: {e}"
    return outcome["ok"] is True, outcome["message"]
