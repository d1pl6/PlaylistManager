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
    #   ...or keep the secret off argv (any local user can read
    #   /proc/<pid>/cmdline, and argv lands in the shell history):
    #   PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET=... python -m app --login spotify
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
import os
import re
import sys
import time

from plugin_loader import PluginRegistry, get_default_registry
from services import auth_setup, duplicate_queue, integration_manager
from services.database import DatabaseManager
from services.integration import IntegrationRegistry
from services.playlist_store import PlaylistStore
from services.playlist_sync import PlaylistSyncService
from services.playlist_url import parse_playlist_url
from services import scrobble_log
from services.scrobble import (
    SCROBBLE_WRITE,
    scrobble_backends,
    scrobble_enabled_for,
)
from services.scrobble_service import scrobble_current_song
from services.song_manager import SongManager
from utils.config import get_setting
from utils.i18n import tr, tr_status, trn
from utils.atomic_json import WRITE_ERRORS
from utils.logging_config import redact, register_secret, user_log
from utils.thumbnail import ThumbnailService

logger = logging.getLogger(__name__)

URL_WAIT_TIMEOUT = 30
YTM_LOGIN_POLL_INTERVAL = 2
YTM_LOGIN_MAX_POLLS = 150


def _wait_for_ytmusic_auth(auth_dir: str) -> tuple[bool, str]:
    """Poll for browser.json and verify YouTube Music authentication works.

    Returns (success, message). On success, message is empty. On failure,
    message describes the error.
    """
    from services import auth_setup

    for _ in range(YTM_LOGIN_MAX_POLLS):
        if auth_setup.browser_file_ready():
            try:
                from integrations.youtube_music.youtube_music import (
                    youtube_auth,
                )

                if youtube_auth.setup_auth():
                    return True, ""
            except Exception as e:
                logger.debug("YT Music auth verification failed during poll: %s", e)
        time.sleep(YTM_LOGIN_POLL_INTERVAL)
    return False, tr("login.browser_json_missing")


class UsageError(Exception):
    """Bad CLI input - mapped to exit code 2 (argparse's usage-error code)."""


# ---------------------------------------------------------------------------
# Target parsing: "1,2,3" / "1-3" / names / mixed
# ---------------------------------------------------------------------------


def _parse_token(token: str, allow_urls: bool = False) -> tuple[str, object]:
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
    spec: str, playlists: list[dict], allow_urls: bool = False
) -> list[tuple[int, dict]]:
    """
    Resolve a playlist spec ("1,2,3", "1-3", names, or a mix) to entries.

    Returns a list of (registry_number, entry) - registry_number is the 1-based
    display order the user sees in the GUI (what ``--list`` prints). Repeated
    targets are silently deduped by (platform, playlist_id), first occurrence
    kept.

    *allow_urls* enables URL tokens, resolved against the registry by
    (platform, playlist_id). Used by the add/del/ref commands: the del/ref
    path always allows them, and the song-add path enables them so ``-a
    <URL>`` targets a registered playlist by its link.

    Raises UsageError on malformed, out-of-range, unknown or ambiguous input.
    """
    if not spec.strip():
        raise UsageError(
            "No playlists given - e.g. '1,2,3', '1-3', or '\"Chill Mix\"'"
        )
    total = len(playlists)
    resolved: list[tuple[int, dict]] = []
    seen = set()

    def add(entry: dict, number: int) -> None:
        # Legacy entries may lack playlist_id (the platform field is always
        # present - the store stamps pre-platform rows at load).  Key on
        # (platform, playlist_id or name) so distinct legacy playlists are
        # not collapsed into a single dedup key.
        key = (
            entry.get("platform", ""),
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
    spec: str, playlists: list[dict]
) -> list[tuple[int, dict]]:
    """Resolve a del/ref spec: the ``all`` keyword, URLs, or a normal spec."""
    if spec.strip().lower() == "all":
        return [(i + 1, p) for i, p in enumerate(playlists)]
    return resolve_targets(spec, playlists, allow_urls=True)


# ---------------------------------------------------------------------------
# Auth bootstrap (generic - driven by the plugin manifests)
# ---------------------------------------------------------------------------


def _init_platform(plugin_registry, platform_id: str, integration=None):
    """Authenticate one platform via its plugin. Returns the integration
    with a live client, or ``None`` when unavailable/unconfigured.

    *integration* may be a prebuilt instance from an
    :class:`IntegrationRegistry` (already authenticated by the caller) -
    pass it when the caller built one, so the platform is never
    authenticated twice.
    """
    plugin = plugin_registry.get(platform_id)
    if plugin is None:
        return None
    if integration is None:
        try:
            auth_manager = None
            if plugin.auth_module:
                auth_manager = plugin.import_auth_attr()
            integration_cls = plugin.import_integration()
            integration = integration_cls(auth_manager=auth_manager)
        except Exception as e:
            logger.error(
                "%s integration unavailable: %s", platform_id, e, exc_info=True
            )
            return None

    try:
        # A prebuilt registry instance may already be authenticated
        # (the caller restricted the registry to exactly the platforms
        # it needs) - re-running authenticate() would repeat the
        # network round trip for nothing.
        if not integration.is_authenticated() and not integration.authenticate():
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
    if plugin is None:
        return "platform plugin not installed - run the plugin install first"
    return f"{plugin.display_name} not configured - run the GUI auth setup first"


def _build_integrations(auth_platforms=None):
    """Return an IntegrationRegistry covering every discovered plugin.

    Mirrors App.__init__ headlessly: no tkinter, no messageboxes.  Every
    loadable integration is registered; consumers gate on
    ``is_authenticated()`` so an unconfigured or failed platform simply
    reports a per-platform error instead of disappearing.

    *auth_platforms* restricts authentication to those platform ids (an
    iterable of id strings); ``None`` authenticates every registered
    platform.  Registration itself is pure Python (no network), so a
    caller that only needs one or two platforms should pass their ids
    and never pay ``authenticate()`` for the rest - Spotify's /v1/me
    verification is a network round trip with a 15 s timeout, and a
    stale unrelated integration (e.g. expired YouTube credentials)
    would otherwise slow or fail an add that does not touch it.
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
            user_log(logger, tr("service.integration_unavailable", name=plugin.display_name, error=e))
    # Authenticate after registration - authenticate() is a network round
    # trip and must never keep a broken plugin from being registered.
    for integration in registry.get_all().values():
        if auth_platforms is not None and integration.id not in auth_platforms:
            continue
        try:
            integration.authenticate()
        except Exception as e:
            logger.error(
                "%s auth failed: %s", integration.display_name, e, exc_info=True
            )
    return registry


def _scrobble_backends(integrations=None):
    """Return the scrobble backends to write to (possibly several).

    Built lazily (and only on demand - the caller gates on auto-scrobble
    being enabled) so an add that is not going to scrobble never pays the
    full-registry authenticate() cost of every installed platform.
    Empty when no scrobble-capable backend is installed and enabled.

    Delegates to :func:`services.scrobble.scrobble_backends` so the CLI and
    the GUI always scrobble to the same set of services - a user decision
    (one ``[<platform>] scrobbles`` switch per backend), not a
    registry-order accident.
    """
    if integrations is None:
        integrations = _build_integrations()
    return scrobble_backends(integrations, SCROBBLE_WRITE)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run_list() -> int:
    """Print the numbered playlist registry (CLI numbers = GUI display order)."""
    playlists = PlaylistStore.load_playlists()
    if not playlists:
        print(tr("cli.no_playlists"), file=sys.stderr)
        return 0
    for i, playlist in enumerate(playlists, 1):
        print(tr("cli.list_item", i=i, name=playlist.get("name"), platform=playlist.get("platform", "")))
    return 0


def run_add(spec: str) -> int:
    """Add the currently-playing song to the requested playlists.

    Targets can be order numbers ("1,2,3"), ranges ("1-3"), names
    ("Chill Mix"), or a playlist URL ("-a <URL>") resolving to a registered
    playlist by (platform, playlist_id). URLs must already be in the
    registry - add them first with ``-p add <URL>``.
    """
    playlists = PlaylistStore.load_playlists()
    if not playlists:
        print(tr("cli.no_playlists"), file=sys.stderr)
        return 2

    try:
        targets = resolve_targets(spec, playlists, allow_urls=True)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    plugin_registry = get_default_registry()
    song_manager = SongManager()

    # Read the auto-scrobble master gate ONCE (not per target - get_setting
    # hits the settings INI).  The ScrobbleCapable backend is resolved only
    # when at least one TARGET platform scrobbles (per the per-platform
    # [scrobble] platforms list) - we never authenticate unrelated
    # platforms just to find a scrobble backend.
    auto_scrobble = get_setting("scrobble_on_add")

    # Build ONE registry, authenticating only the platforms this add
    # touches: the target platforms plus the scrobble backend when
    # auto-scrobble is on.  Authenticating every installed platform here
    # pays network round trips (Spotify /v1/me verification) for
    # integrations the add never uses, and a stale unrelated integration
    # could delay or fail a perfectly healthy add.
    target_platforms = {
        entry.get("platform", "") for _, entry in targets
    }
    registry = _build_integrations(auth_platforms=target_platforms)
    scrobble_integ = []
    if auto_scrobble and any(
        scrobble_enabled_for(p) for p in target_platforms
    ):
        for backend in _scrobble_backends(registry):
            if backend.id not in target_platforms:
                # A backend that is also a target authenticates itself
                # below; only the others need it here.  Skipping the
                # duplicate round trip is the point - authenticating
                # every installed platform was what this gate avoids.
                try:
                    backend.authenticate()
                except Exception as e:
                    logger.error(
                        "%s auth failed: %s",
                        backend.display_name, e, exc_info=True,
                    )
                    continue
            scrobble_integ.append(backend)

    # Every store row carries a platform (PlaylistStore stamps legacy rows
    # on load), so targets group by the field directly.
    targets_by_platform: dict[str, list] = {}
    for number, entry in targets:
        platform = entry.get("platform", "")
        targets_by_platform.setdefault(platform, []).append((number, entry))

    # Per platform: authenticate once, build its flow via the plugin
    # manifest, capture the current song ONCE and share it across all of
    # that platform's target playlists.
    contexts: dict[str, dict] = {}
    for platform, group in targets_by_platform.items():
        ctx = {"flow": None, "url": None, "song_data": None, "error": ""}
        contexts[platform] = ctx

        plugin = plugin_registry.get(platform)
        if plugin is None or not plugin.flow_class:
            ctx["error"] = f"unsupported platform '{platform}'"
            continue
        integration = _init_platform(
            plugin_registry, platform, integration=registry.get(platform)
        )
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
            ctx["flow_type"] = plugin.flow_type
            ctx["url"], ctx["song_data"], ctx["error"] = flow.capture(URL_WAIT_TIMEOUT)
        except Exception as e:
            logger.error("%s flow/capture failed: %s", platform, e, exc_info=True)
            ctx["flow"] = None
            ctx["error"] = str(e)

    failures = 0
    for number, entry in targets:
        platform = entry.get("platform", "")
        name = entry.get("name")
        ctx = contexts[platform]
        if ctx["flow"] is None:
            ok, message = False, f"Error: {ctx['error']}"
        elif ctx.get("flow_type") == "extension" and ctx["url"] is None:
            ok, message = False, f"Error: {ctx['error'] or 'Timeout: no URL received'}"
        elif ctx["song_data"] is None:
            ok, message = False, f"Error: {ctx['error'] or 'Nothing playing'}"
        else:
            ok, message = _run_flow(
                ctx["flow"], name, url=ctx["url"], song_data=ctx["song_data"],
                playlist_id=entry.get("playlist_id") or None,
                scrobble_integ=scrobble_integ,
                platform=platform,
            )

        line = tr("cli.result_line", number=number, name=name, platform=platform, message=message)
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

    integrations = _build_integrations(auth_platforms={platform})
    integration = integrations.get(platform)
    if integration is None or not integration.is_authenticated():
        plugin = get_default_registry().get(platform)
        print(f"error: {_auth_error(plugin)}", file=sys.stderr)
        return 1

    # Platform-first: never register a playlist the platform cannot
    # confirm exists (deleted, private, or an arbitrary bad URL).
    try:
        details = integration.get_playlist_details(playlist_id)
    except Exception as e:
        print(tr("cli.fetch_details_failed", error=e), file=sys.stderr)
        return 1
    name = details.get("title") if isinstance(details, dict) else None
    if not name:
        print(
            tr("cli.playlist_not_found", platform=platform),
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
                tr("cli.not_owner_warning", name=name),
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

    action = tr("cli.updated") if existed else tr("cli.added")
    sync = PlaylistSyncService(integrations)
    try:
        inserted, status = sync.import_tracks_sync(name, platform, playlist_id)
    except Exception as e:
        print(
            tr("cli.import_failed_line", number=number, name=name, platform=platform, action=action, error=e),
            file=sys.stderr,
        )
        return 1

    import_note = trn("cli.imported_tracks", inserted) if inserted else tr_status(status).lower()
    message = f"{action}, {import_note}"
    print(tr("cli.result_line", number=number, name=name, platform=platform, message=message), flush=True)
    return 0


def run_del(spec: str) -> int:
    """Remove playlist(s) from the registry and delete their local DBs.

    Local-only: never touches the playlist on the platform.
    """
    playlists = PlaylistStore.load_playlists()
    if not playlists:
        print(tr("cli.no_playlists_short"), file=sys.stderr)
        return 2

    try:
        targets = resolve_targets_for(spec, playlists)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for number, entry in targets:
        name = entry.get("name")
        platform = entry.get("platform", "")
        playlist_id = entry.get("playlist_id", "")
        PlaylistStore.delete_playlist(
            name, platform=platform, playlist_id=playlist_id
        )
        DatabaseManager.delete_playlist_db(name, platform, playlist_id=playlist_id)
        scrobble_log.remove_playlist_entries(platform, playlist_id)
        # Best-effort: a stale extra.json entry only points at a playlist
        # whose DB is already gone, so a failed purge must not abort the
        # remaining targets (or turn a completed delete into a failure).
        try:
            duplicate_queue.purge_playlist(platform, playlist_id, name or "")
        except WRITE_ERRORS as e:
            print(
                tr("cli.purge_failed", platform=platform, error=e),
                file=sys.stderr,
            )
        print(tr("cli.deleted_line", number=number, name=name, platform=platform), flush=True)
    return 0


def run_refresh(spec: str) -> int:
    """Re-import all tracks for playlist(s) from the platform."""
    playlists = PlaylistStore.load_playlists()
    if not playlists:
        print(tr("cli.no_playlists_short"), file=sys.stderr)
        return 2

    try:
        targets = resolve_targets_for(spec, playlists)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    integrations = _build_integrations(
        auth_platforms={
            entry.get("platform", "") for _, entry in targets
        }
    )
    sync = PlaylistSyncService(integrations)

    failures = 0
    for number, entry in targets:
        name = entry.get("name")
        platform = entry.get("platform", "")
        playlist_id = entry.get("playlist_id", "")
        if not playlist_id:
            print(
                tr("cli.no_playlist_id_line", number=number, name=name, platform=platform),
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

        reload_note = trn("cli.new_songs", inserted) if inserted else tr_status(status).lower()
        print(
            tr("cli.refreshed_line", number=number, name=name, platform=platform, note=reload_note),
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

    # Register every integration WITHOUT authenticating.  The capture
    # loop below authenticates one platform at a time right before its
    # attempt, so a scrobble never pays authenticate() (a /v1/me-style
    # network round trip) for platforms that are not the current song
    # source — the GUI equivalent runs against the app's already
    # authenticated live registry and pays nothing per action.
    integrations = _build_integrations(auth_platforms=set())

    # Authenticate every enabled scrobble backend up front - they are all
    # needed regardless of which platform produces the song.  One failing
    # (revoked token, service down) is reported and dropped rather than
    # aborting: the user asked for a scrobble, and the services that do work
    # should still get one.
    backends = _scrobble_backends(integrations)
    if not backends:
        print(tr("cli.no_scrobble_backend"), file=sys.stderr)
        return 1
    scrobble_integ = []
    for backend in backends:
        authenticated = _init_platform(
            plugin_registry, backend.id, integration=backend
        )
        if authenticated is None:
            user_log("Could not authenticate scrobble backend %s", backend.id)
            continue
        scrobble_integ.append(authenticated)
    if not scrobble_integ:
        print(tr("cli.no_scrobble_backend"), file=sys.stderr)
        return 1

    def flow_provider(platform_id: str):
        plugin = plugin_registry.get(platform_id)
        flow_cls = plugin.import_flow()
        integ = integrations.get(platform_id)
        if plugin.flow_type == "extension":
            return flow_cls(integ, song_manager, plugin.build_receiver())
        return flow_cls(integ, song_manager)

    def integration_provider(platform_id: str):
        # Lazy per-platform authentication: one platform at a time, and
        # only when it is about to attempt a capture.  The prebuilt
        # registry instance is passed along so a platform that was
        # already authenticated (or whose authenticate() needs no
        # network, e.g. YouTube's local browser.json) is never
        # re-verified.
        return _init_platform(
            plugin_registry, platform_id,
            integration=integrations.get(platform_id),
        )

    captured = scrobble_current_song(
        integrations, plugin_registry, flow_provider, scrobble_integ,
        URL_WAIT_TIMEOUT, integration_provider=integration_provider,
    )
    if captured is None:
        print(tr("cli.no_song_found"), file=sys.stderr)
        return 1
    song_data, ts = captured
    if ts:
        title = song_data.get("title", "unknown")
        artist = (song_data.get("artists") or ["unknown"])[0]
        print(tr("cli.scrobbled", artist=artist, title=title), flush=True)
        return 0
    print(tr("cli.scrobble_failed", title=song_data.get("title", "unknown")), file=sys.stderr)
    return 1


def _stored_refresh_token() -> str | None:
    """Read the refresh token from the saved spotify.json, if any.

    Used by the interactive login as the "default" for the refresh-token
    prompt - pressing Enter re-logs-in with the stored token (e.g. when
    only the client credentials changed).
    """
    return _stored_refresh_token_for(get_default_registry().get("spotify"))


def _stored_refresh_token_for(plugin) -> str | None:
    """Read the refresh token from a plugin's saved auth file, if any."""
    if plugin is None or not plugin.auth_path:
        return None
    try:
        data = json.loads(plugin.auth_path.read_text(encoding="utf-8"))
        return data.get("refresh_token") or None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Credential sources for --login
# ---------------------------------------------------------------------------
# Three tiers, in this order: explicit flag, environment variable, hidden
# prompt.  The flag stays for scripted use but is the *worst* way to hand a
# secret to a process:
#
#   argv      /proc/<pid>/cmdline is mode 0444, so every local user can read
#             it with `ps`, and the value is persisted in the shell history
#             (and in tmux/screen scrollback, in the terminal emulator's
#             saved state, in process-accounting records).
#   env       /proc/<pid>/environ is mode 0400 and owned by the process, so
#             only the same user and root can read it, and nothing is written
#             to any history.  The cost: the value is inherited by every
#             child the exporting shell spawns, for as long as it is
#             exported - so `unset` it right after the login.
#   prompt    Nothing in argv, the environment or the history; the input is
#             not echoed.  The right choice interactively, and what
#             --login does on its own.
#
# See CLI.MD "Authentication" for the user-facing version.

_ENV_PREFIX = "PLAYLISTMANAGER_"

# field -> i18n key of the prompt label used when nothing else supplied it.
_CREDENTIAL_PROMPT_KEYS = {
    "client_id": "cli.prompt_client_id",
    "client_secret": "cli.prompt_client_secret",
    "refresh_token": "cli.prompt_refresh_token",
    "arl": "cli.prompt_arl_cookie",
    "token": "cli.prompt_listenbrainz_token",
}


def env_credential(platform: str, field: str) -> str | None:
    """Read a credential from the environment; None when unset or blank.

    The name is ``PLAYLISTMANAGER_<PLATFORM>_<FIELD>`` - e.g.
    ``PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET`` or
    ``PLAYLISTMANAGER_DEEZER_ARL``.  A blank or whitespace-only value counts
    as unset, so `VAR= ... --login` falls through to the prompt instead of
    failing a verify with an empty secret.
    """
    name = f"{_ENV_PREFIX}{platform.upper()}_{field.upper()}"
    value = (os.environ.get(name) or "").strip()
    return value or None


def _stdin_is_tty() -> bool:
    """True when stdin is a usable terminal (False when closed/None)."""
    stream = sys.stdin
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _read_line(label: str) -> str | None:
    """Read one line from stdin, with *label* on stderr; None at EOF.

    Deliberately not ``input()``: that writes the prompt to *stdout*,
    polluting the single result line a script parses.  Nothing is echoed -
    stdin is a pipe or a file here, there is no terminal to echo to.
    """
    if sys.stdin is None:
        return None
    sys.stderr.write(label)
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError):
        return None
    if not line:  # closed / empty stdin -> clean failure upstream
        return None
    return line.strip()


def _prompt(label: str, hidden: bool = False) -> str | None:
    """Credential prompt: hidden on a TTY, a plain read otherwise.

    Returns None on a closed input stream (EOF) so non-interactive
    invocations fail cleanly instead of crashing.

    The hidden path is only taken when stdin is a TTY.  ``getpass`` opens
    ``/dev/tty`` itself and, when that fails, silently falls back to reading
    from stdin *with a warning that the input may be echoed* - which is both
    noisy and wrong here: under a compositor keybind, a pipe or CI there is no
    terminal to echo to, and the fallback would be the only warning the user
    ever sees.
    """
    try:
        if not hidden or not _stdin_is_tty():
            return _read_line(label)
        return getpass.getpass(label).strip()
    except EOFError:
        return None


def _resolve_credential(
    platform: str, field: str, flag_value: str | None, *, hidden: bool
) -> str | None:
    """Resolve one credential: flag > env > prompt.

    An explicit flag wins so a script can override an exported variable
    for a single run.  Only the *source* is ever logged, never the value.
    """
    if flag_value and flag_value.strip():
        return flag_value.strip()
    env_value = env_credential(platform, field)
    if env_value:
        logger.debug(
            "--login %s: %s from %s%s_%s",
            platform, field, _ENV_PREFIX, platform.upper(), field.upper(),
        )
        return env_value
    return _prompt(tr(_CREDENTIAL_PROMPT_KEYS[field]), hidden=hidden)


def _resolve_login_credentials(
    platform: str,
    client_id: str | None,
    client_secret: str | None,
    refresh_token: str | None,
    stored_refresh_token: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the three login fields, then fall back to the stored token.

    The client id is not a secret (it is a public identifier in the OAuth
    dance) so its prompt is visible; the secret and the token are hidden.
    An empty refresh-token prompt re-logs in with the stored token - it is
    long-lived, and re-entering it is only needed when the client
    credentials changed.
    """
    client_id = _resolve_credential(platform, "client_id", client_id, hidden=False)
    client_secret = _resolve_credential(
        platform, "client_secret", client_secret, hidden=True
    )
    refresh_token = _resolve_credential(
        platform, "refresh_token", refresh_token, hidden=True
    )
    # Register before anything can fail with an error string that echoes a
    # value: from here on redact() masks the secret/token in the log *and* in
    # the reason this function's caller prints.
    register_secret(client_secret, refresh_token)
    return client_id, client_secret, refresh_token or stored_refresh_token


def _run_soundcloud_login(
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
) -> int:
    """CLI SoundCloud login: prompts for credentials, verifies FIRST.

    Mirrors the Spotify login: the SoundCloud OAuth config
    (client id / client secret / refresh token) is verified against the
    live ``/me`` round-trip and only persisted on success - a typo can
    never destroy previously working auth.
    """
    plugin = get_default_registry().get("soundcloud")
    if plugin is None:
        print(tr("cli.soundcloud_plugin_not_found"), file=sys.stderr)
        return 2

    client_id, client_secret, refresh_token = _resolve_login_credentials(
        "soundcloud", client_id, client_secret, refresh_token,
        _stored_refresh_token_for(plugin),
    )
    if not client_id or not client_secret or not refresh_token:
        print(
            tr("cli.credentials_required_soundcloud"),
            file=sys.stderr,
        )
        return 2

    result = _soundcloud_save_and_verify(client_id, client_secret, refresh_token)
    if result.get("ok"):
        print(tr("cli.login_ok_soundcloud", display_name=result.get("display_name")), flush=True)
        return 0

    print(
        tr("cli.login_failed_soundcloud", reason=redact(str(result.get("error") or ""))),
        file=sys.stderr,
    )
    return 1


def _soundcloud_save_and_verify(client_id, client_secret, refresh_token) -> dict:
    """Call the SoundCloud plugin's own verify-first save helper.

    The plugin declares no core auth_setup functions (login is
    self-contained in the plugin via ``login_module``/``login_class``), so
    the CLI resolves the plugin's soundcloud module directly rather than
    hard-coding a path into app/services/auth_setup.py.
    """
    try:
        soundcloud_mod = __import__(
            f"integrations.{_soundcloud_dir_name()}.soundcloud",
            fromlist=["save_and_verify_soundcloud_credentials"],
        )
        return soundcloud_mod.save_and_verify_soundcloud_credentials(
            client_id, client_secret, refresh_token
        )
    except Exception as e:
        return {"ok": False, "error": f"SoundCloud plugin error: {e}"}


def _soundcloud_dir_name() -> str:
    """Directory name of the SoundCloud plugin (``integrations.<dir>``)."""
    return "soundcloud"


def _run_deezer_login() -> int:
    """CLI Deezer login: gets the ARL cookie, verifies FIRST.

    The ARL is obtained by logging into Deezer in a browser and copying
    the ``arl`` cookie value from DevTools → Application → Cookies.  It is
    a live session cookie, so it is read from
    ``PLAYLISTMANAGER_DEEZER_ARL`` or the hidden prompt - there is no flag
    for it, which used to make a scripted Deezer login impossible (the
    prompt was the only source).
    """
    arl = _resolve_credential("deezer", "arl", None, hidden=True)
    if not arl:
        print(
            tr("cli.arl_cookie_required"),
            file=sys.stderr,
        )
        return 2
    register_secret(arl)

    result = auth_setup.save_and_verify_deezer_credentials(arl.strip())
    if result.get("ok"):
        print(
            tr("cli.login_ok_deezer", display_name=result.get("display_name")),
            flush=True,
        )
        return 0

    print(
        tr("cli.login_failed_deezer", reason=redact(str(result.get("error") or ""))),
        file=sys.stderr,
    )
    return 1


def _run_listenbrainz_login() -> int:
    """CLI ListenBrainz login: gets the user token, verifies FIRST.

    The token is copied from https://listenbrainz.org/settings/ and is a
    live credential with no expiry and no second factor, so - like the
    Deezer ARL - it is read from ``PLAYLISTMANAGER_LISTENBRAINZ_TOKEN`` or
    the hidden prompt, with no flag: argv is world-readable through
    ``/proc/<pid>/cmdline`` and kept in the shell history.
    """
    token = _resolve_credential("listenbrainz", "token", None, hidden=True)
    if not token:
        print(
            tr("cli.listenbrainz_token_required"),
            file=sys.stderr,
        )
        return 2
    register_secret(token)

    result = auth_setup.save_and_verify_listenbrainz_credentials(token.strip())
    if result.get("ok"):
        print(
            tr("cli.login_ok_listenbrainz", display_name=result.get("display_name")),
            flush=True,
        )
        return 0

    print(
        tr("cli.login_failed_listenbrainz", reason=redact(str(result.get("error") or ""))),
        file=sys.stderr,
    )
    return 1


def _run_tidal_login() -> int:
    """CLI Tidal login: the app credentials, then the PKCE browser dance.

    Only the client id and secret are read here (flag >
    ``PLAYLISTMANAGER_TIDAL_{CLIENT_ID,CLIENT_SECRET}`` > hidden prompt).
    There is deliberately no refresh-token source: the plugin's loopback
    flow *mints* it, so a third credential would only invite a hand-pasted
    token that nothing can obtain.
    """
    client_id = _resolve_credential("tidal", "client_id", None)
    client_secret = _resolve_credential(
        "tidal", "client_secret", None, hidden=True
    )
    if not client_id or not client_secret:
        print(
            tr("cli.tidal_credentials_required"),
            file=sys.stderr,
        )
        return 2

    print(tr("cli.tidal_login_waiting"), flush=True)

    def _show_url(url: str) -> None:
        # Runs on the login's worker thread; print is thread-safe.
        print(tr("cli.tidal_login_url", url=url), flush=True)

    result = auth_setup.tidal_browser_login(
        client_id.strip(), client_secret.strip(), on_url=_show_url
    )
    if result.get("ok"):
        print(
            tr("cli.login_ok_tidal", display_name=result.get("display_name")),
            flush=True,
        )
        return 0

    print(
        tr("cli.login_failed_tidal", reason=redact(str(result.get("error") or ""))),
        file=sys.stderr,
    )
    return 1


def run_login(
    platform: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
) -> int:
    """Log in to a platform. Returns the CLI exit code.

    youtube_music: mirrors the GUI tile - opens the auth folder and a
    terminal running ``ytmusicapi browser`` (manual instructions instead
    when no terminal emulator is available).

    spotify: ``--login spotify`` alone prompts interactively (client id,
    client secret, refresh token - secret/token hidden like sudo).  Each
    of the three can also arrive from
    ``PLAYLISTMANAGER_SPOTIFY_{CLIENT_ID,CLIENT_SECRET,REFRESH_TOKEN}``
    or from stdin; the flags remain as overrides for
    scripting/compositor use, in the order flag > env > prompt.  argv is
    world-readable through ``/proc/<pid>/cmdline`` and is persisted in the
    shell history, so prefer the env var or the prompt for a real secret
    (CLI.MD "Authentication" has the full comparison).  The refresh
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
            tr("cli.unknown_platform", platform=platform, options=", ".join(known)),
            file=sys.stderr,
        )
        return 2

    if platform == "youtube_music":
        result = auth_setup.setup_ytmusic_auth()
        if not result.get("ok"):
            # Terminal could not be launched - the auth folder was opened
            # in the file manager as a manual fallback.
            print(
                tr("cli.ym_no_terminal", auth_dir=result.get("auth_dir", auth_setup.AUTH_DIR)),
                file=sys.stderr,
            )
            if result.get("error"):
                print(f"  {result['error']}", file=sys.stderr)
            return 1
        print(
            tr("cli.ym_opened_terminal", auth_dir=auth_setup.AUTH_DIR),
            flush=True,
        )
        # Poll for browser.json and verify authentication works
        print(tr("cli.ym_waiting_auth"), flush=True)
        success, error = _wait_for_ytmusic_auth(str(auth_setup.AUTH_DIR))
        if success:
            print(tr("cli.login_ok", platform="YouTube Music"), flush=True)
            return 0
        else:
            print(
                tr("cli.login_failed", platform="YouTube Music", reason=error),
                file=sys.stderr,
            )
            return 1

    if platform == "lastfm":
        # Last.fm auth needs the browser-authorization + auth.getSession
        # flow that only the GUI login dialog drives (api key + secret, then
        # approve in the browser).  No useful non-interactive path.
        print(
            tr("cli.lastfm_gui_only"),
            file=sys.stderr,
        )
        return 2

    if platform == "listenbrainz":
        return _run_listenbrainz_login()

    if platform == "deezer":
        return _run_deezer_login()

    if platform == "tidal":
        return _run_tidal_login()

    if platform == "soundcloud":
        return _run_soundcloud_login(client_id, client_secret, refresh_token)

    # Spotify.  flag > env > hidden prompt, then the stored refresh token.
    client_id, client_secret, refresh_token = _resolve_login_credentials(
        "spotify", client_id, client_secret, refresh_token, _stored_refresh_token(),
    )
    if not client_id or not client_secret or not refresh_token:
        print(
            tr("cli.credentials_required_spotify"),
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
        print(tr("cli.login_ok_spotify", display_name=result.get("display_name")), flush=True)
        return 0

    print(
        tr("cli.login_failed_spotify", reason=redact(str(result.get("error") or ""))),
        file=sys.stderr,
    )
    return 1


def run_logout(platform: str) -> int:
    """Log out of a platform: delete its credentials, registry entries,
    local databases, duplicate-queue records and scrobble-ledger entries.
    Local-only - never touches the platform itself.

    Idempotent: a second run with nothing left prints "no credentials
    found" and still exits 0.
    """
    known = get_default_registry().get_platform_ids()
    if platform not in known:
        print(
            tr("cli.unknown_platform", platform=platform, options=", ".join(known)),
            file=sys.stderr,
        )
        return 2

    try:
        deleted, _missing = auth_setup.delete_platform_credentials(
            platform, plugin_registry=get_default_registry()
        )
    except OSError as e:
        print(
            tr("cli.delete_credentials_failed", platform=platform, error=e),
            file=sys.stderr,
        )
        return 1

    if deleted:
        print(
            tr("cli.logged_out", platform=platform, deleted=", ".join(str(p) for p in deleted)),
            flush=True,
        )
    else:
        print(tr("cli.no_credentials", platform=platform), flush=True)

    n_registry = PlaylistStore.delete_playlists_for_platform(platform)
    if n_registry:
        print(f"{platform}: {trn('cli.removed_registry', n_registry)}")

    n_dbs = DatabaseManager.delete_platform_databases(platform)
    if n_dbs:
        print(f"{platform}: {trn('cli.deleted_dbs', n_dbs)}")

    # Drop the platform's pending duplicate-decisions and scrobble-ledger
    # records (matching integration_manager.uninstall_platform_data), so a
    # later login can't resurface stale decisions / unscrobbles for it.
    # Best-effort: the credentials, registry entries and databases are
    # already gone, so a failed purge is a warning, not a failed logout.
    try:
        n_pending, n_songs, n_errors = duplicate_queue.purge_platform(platform)
    except WRITE_ERRORS as e:
        print(
            tr("cli.purge_failed", platform=platform, error=e),
            file=sys.stderr,
        )
        n_pending = n_songs = n_errors = 0
    if n_pending or n_songs or n_errors:
        items = [
            trn("cli.purge_decisions", n_pending),
            trn("cli.purge_songs", n_songs),
            trn("cli.purge_errors", n_errors),
        ]
        print(f"{platform}: {', '.join(items)} {tr('cli.purged')}")
    scrobble_log.remove_platform_entries(platform)
    return 0


def _resolve_platform_arg(platform: str) -> list[str]:
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

    failures: list[str] = []
    for pid in targets:
        display = integration_manager.INTEGRATION_REPOS[pid].display_name
        target_dir = PluginRegistry().base_dir / pid
        already = target_dir.is_dir()
        try:
            integration_manager.download_integration(pid)
            action = tr("cli.install_status_updated") if already else tr("cli.install_status_installed")
            print(
                tr("cli.install_result", pid=pid, action=action, display=display),
                flush=True,
            )
        except Exception as e:
            logger.exception("Install failed for %s", pid)
            print(tr("cli.install_failed", pid=pid, error=e), file=sys.stderr)
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
        # Every installed plugin - catalog or third-party - plus catalog
        # platforms whose directory is on disk but whose manifest failed
        # to load (the registry cannot see those).
        registry = get_default_registry()
        targets = sorted(
            set(registry.get_all())
            | {
                pid
                for pid in catalog
                if (PluginRegistry().base_dir / pid).is_dir()
            }
        )
    else:
        # Accept ids that are installed but not in the catalog (they were
        # downloaded by hand or by the GUI) - uninstalling them by id is
        # the only CLI path to remove them.
        registry = get_default_registry()
        if platform not in catalog and registry.get(platform) is None:
            print(
                tr("cli.unknown_platform", platform=platform, options=", ".join(catalog)),
                file=sys.stderr,
            )
            return 2
        targets = [platform]

    failures: list[str] = []
    for pid in targets:
        registry = get_default_registry()
        plugin = registry.get(pid)
        if plugin is None and not (PluginRegistry().base_dir / pid).is_dir():
            print(tr("cli.not_installed", pid=pid))
            continue
        try:
            report = integration_manager.uninstall_platform_data(
                pid, plugin=plugin
            )
            parts = [
                trn("cli.uninstall_credentials", report["credentials"]),
                trn("cli.uninstall_playlists", report["playlists"]),
                trn("cli.uninstall_dbs", report["databases"]),
                trn("cli.uninstall_folders", report["plugin_dirs"]),
            ]
            print(
                f"{pid}: uninstalled ({', '.join(parts)})",
                flush=True,
            )
        except Exception as e:
            logger.exception("Uninstall failed for %s", pid)
            print(tr("cli.uninstall_failed", pid=pid, error=e), file=sys.stderr)
            failures.append(pid)

    if failures:
        return 1
    return 0


def _run_flow(
    flow, playlist_name: str, url=None, song_data=None, playlist_id=None,
    scrobble_integ=None, platform: str = "",
) -> tuple[bool, str]:
    """Run one add-flow; returns (ok, message) - message is the line tail.

    *scrobble_integ* is the pre-resolved list of ScrobbleCapable backends
    (possibly empty), resolved once by the caller when auto-scrobble is
    enabled.  Every one of them gets the listen.
    """
    # Flows declare their platform id (PLATFORM class attr); the caller
    # passes it where the platform is already resolved to be explicit.
    platform = platform or getattr(flow, "PLATFORM", "")
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

        # Grid "Used" sort: bump only on an actual add (the outcome was
        # "added", not exists/duplicate).
        if status == "added":
            PlaylistStore.mark_used(playlist_name, platform, playlist_id)

        # Scrobble the song if auto-scrobble is enabled and a ScrobbleCapable
        # integration was resolved.  EVERY resolved backend gets the listen,
        # and the ones that accepted it are recorded in the scrobble ledger
        # so the remove-song path can later delete THIS exact scrobble on
        # each of them (not the track's most recent one).  One backend
        # rejecting must not cost the others their scrobble, so each call
        # is isolated and its record written immediately.
        if status == "added" and scrobble_integ and scrobble_enabled_for(platform):
            result_song = result.get("song", {})
            if result_song:
                for backend in scrobble_integ:
                    try:
                        ts = backend.scrobble(result_song)
                    except Exception as e:
                        logger.debug("Failed to scrobble song: %s", e)
                        continue
                    song_id = result.get("song_id")
                    if ts is not None and song_id is not None:
                        try:
                            scrobble_log.record_scrobble(
                                platform,
                                playlist_id or "",
                                song_id,
                                ts,
                                [getattr(backend, "id", "")],
                            )
                        except Exception as e:
                            logger.debug("Recording the scrobble failed: %s", e)

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
