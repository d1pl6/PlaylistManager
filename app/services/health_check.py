"""
Connectivity and service-health probes.

The platform-API TCP targets plus the coarse internet probe are shared
between the main window's reachability banners and the CLI's pre-flight
network check - one implementation instead of two copies.

``probe_authenticated`` goes one layer deeper for the Manage dialog's
status refresh: a live, authenticated round trip against the platform's
API.  A TCP probe can report "up" while the OAuth token is dead (revoked
refresh token, expired session key) - only a real API call proves the
credentials still work.

The socket probes block on the CALLING thread: GUI callers run them in a
daemon thread and marshal results to the UI; the CLI calls them
synchronously as a pre-flight gate before starting a network-bound
workflow.  ``probe_authenticated`` additionally bounds itself with an
internal watchdog thread, because the plugin HTTP layers (unlike the
socket probes) do not all set request timeouts.
"""

from __future__ import annotations

import logging
import socket
import threading
from socket import timeout as SocketTimeout
from typing import Callable

logger = logging.getLogger(__name__)

# Lightweight TCP targets for service-health probes (host, port).
# A connect() success only proves the platform's public endpoint accepts
# TCP - exactly what a reachability banner needs and nothing more.
PLATFORM_API_TARGETS: dict[str, tuple[str, int]] = {
    "youtube_music": ("music.youtube.com", 443),
    "spotify": ("api.spotify.com", 443),
    "soundcloud": ("api.soundcloud.com", 443),
    "deezer": ("pipe.deezer.com", 443),
}

# Coarse "is there internet at all" probe: a public DNS-over-UDP endpoint
# that completes connect() without needing DNS resolution of our own.
_INTERNET_PROBE = ("1.1.1.1", 53)
_INTERNET_TIMEOUT_S = 5.0
_API_TIMEOUT_S = 10.0
# Bound for one authenticated probe.  The probe runs on an internal
# watchdog thread so a plugin request without its own socket timeout
# cannot hang the Manage dialog's refresh forever: when the bound elapses
# the verdict is reported as "unknown" and the next refresh can retry.
_AUTH_PROBE_TIMEOUT_S = 15.0


def check_network(timeout: float = _INTERNET_TIMEOUT_S) -> bool:
    """Return True when a coarse internet probe succeeds.

    Used as the CLI pre-flight gate so network-less runs fail fast with
    a clear message instead of confusing per-request errors mid-flow.
    """
    try:
        socket.create_connection(_INTERNET_PROBE, timeout=timeout)
        return True
    except (OSError, SocketTimeout):
        return False


def probe_platform(platform: str, timeout: float = _API_TIMEOUT_S) -> bool:
    """Return True when *platform*'s API endpoint accepts TCP.

    Unknown platforms probe False - there is no endpoint to reach.
    """
    target = PLATFORM_API_TARGETS.get(platform)
    if target is None:
        return False
    try:
        socket.create_connection(target, timeout=timeout)
        return True
    except (OSError, SocketTimeout):
        return False


def probe_platforms(
    platforms: set[str], timeout: float = _API_TIMEOUT_S
) -> dict[str, bool]:
    """Probe each platform that has a target; return platform -> ok map."""
    results: dict[str, bool] = {}
    for platform in platforms:
        if PLATFORM_API_TARGETS.get(platform) is not None:
            results[platform] = probe_platform(platform, timeout=timeout)
    return results


def probe_authenticated(
    integration, *, timeout: float = _AUTH_PROBE_TIMEOUT_S
) -> "tuple[bool | None, str | None]":
    """Run a live authenticated round trip against the platform's API.

    A TCP probe (:func:`probe_platform`) proves the endpoint accepts TCP
    connects, nothing more: the reachability banner can say "up" while
    the OAuth token is dead (revoked refresh token, expired session key).
    This probe makes a real authenticated API call so the credentials
    themselves are validated.

    *integration* is a live registered plugin integration object; it is
    duck-typed (no plugin modules are imported here, so this function
    stays import-order-safe and headless).  Returns ``(ok, detail)``:

    * ``ok`` is ``True`` - the platform accepted the credentials.
    * ``ok`` is ``False`` - the authenticated call failed: revoked or
      expired credentials, or an API error - the verdict a TCP probe
      cannot produce.
    * ``ok`` is ``None`` - no probe possible (no credentials loaded this
      session, no probe implementation for the platform, or the probe
      exceeded *timeout*).
    * *detail* is a short best-effort reason when ``ok`` is ``False``.

    A ``False`` verdict conflates a rejected token with a broken/erroring
    call: the platform APIs expose no clean in-band distinction, and the
    next refresh re-probes, so callers should treat ``False`` as
    "credentials need attention" rather than as a hard diagnosis.
    """
    pid = getattr(integration, "id", "") or ""
    capability = getattr(integration, "probe_authenticated", None)
    box: list = []

    def _run() -> None:
        try:
            if callable(capability):
                # Optional plugin-provided probe: takes no arguments and
                # returns True / False / None or an (ok, detail) tuple.
                raw = capability()
            else:
                probe = _AUTH_PROBES.get(pid)
                raw = probe(integration) if probe is not None else None
            box.append(_normalize_verdict(raw))
        except Exception as e:  # plugin error paths vary - never crash the refresh
            logger.debug("Authenticated probe failed for %s: %s", pid, e)
            box.append((False, str(e)[:160]))

    watchdog = threading.Thread(target=_run, daemon=True)
    watchdog.start()
    watchdog.join(timeout)
    return box[0] if box else (None, None)


def _normalize_verdict(raw) -> "tuple[bool | None, str | None]":
    """Normalize a probe result (bool | None | (ok, detail)) to (ok, detail)."""
    if isinstance(raw, tuple) and len(raw) == 2:
        ok, detail = raw
        if ok is not True and ok is not False:
            ok = None
        return (ok, detail if isinstance(detail, str) else None)
    if raw is True:
        return (True, None)
    if raw is False:
        return (False, None)
    return (None, None)


# ---------------------------------------------------------------------------
# Per-platform probes
#
# Keyed by plugin id; each reaches into the LIVE integration object's
# public handles (duck-typed attributes - nothing is imported from the
# plugin packages).  A missing handle (platform installed but never
# authenticated this session) means "no probe possible" -> (None, None),
# never a false "auth failed".
# ---------------------------------------------------------------------------


def _probe_spotify(integration) -> "tuple[bool | None, str | None]":
    api = getattr(integration, "spotify_api", None)
    if api is None:
        return (None, None)
    try:
        me = api.get_me()
    except RuntimeError as e:
        # Token refresh rejected - the definitive "credentials are dead"
        # signal (SpotifyAPI._request maps transport failures to None but
        # lets the failed-refresh RuntimeError propagate).
        return (False, str(e)[:160])
    except Exception as e:
        return (False, str(e)[:160])
    return (True, None) if me else (False, None)


def _probe_youtube_music(integration) -> "tuple[bool | None, str | None]":
    client = getattr(integration, "yt_client", None)
    if client is None:
        return (None, None)
    try:
        # The same cheap authenticated round trip the login verification
        # path uses (save_and_verify_ytmusic_auth).
        client.get_library_playlists(limit=1)
    except Exception as e:
        return (False, str(e)[:160])
    return (True, None)


def _probe_soundcloud(integration) -> "tuple[bool | None, str | None]":
    api = getattr(integration, "soundcloud_api", None)
    if api is None:
        return (None, None)
    try:
        me = api.get_me()
    except Exception as e:
        return (False, str(e)[:160])
    return (True, None) if me else (False, None)


def _probe_deezer(integration) -> "tuple[bool | None, str | None]":
    api = getattr(integration, "deezer_api", None)
    if api is None:
        return (None, None)
    try:
        me = api.get_me()
    except Exception as e:
        return (False, str(e)[:160])
    return (True, None) if me else (False, None)


def _probe_lastfm(integration) -> "tuple[bool | None, str | None]":
    client = getattr(integration, "client", None)
    if client is None or not getattr(client, "username", ""):
        return (None, None)
    try:
        # One page of user.getLovedTracks - a single call that raises
        # LastFMError when the session key is dead.  An empty username is
        # guarded above: without one the call returns {} with no round trip
        # at all, which would be a false positive.
        client.get_loved_tracks(max_pages=1)
    except Exception as e:
        return (False, str(e)[:160])
    return (True, None)


_AUTH_PROBES: dict[
    str, Callable[[object], "tuple[bool | None, str | None]"]
] = {
    "youtube_music": _probe_youtube_music,
    "spotify": _probe_spotify,
    "soundcloud": _probe_soundcloud,
    "deezer": _probe_deezer,
    "lastfm": _probe_lastfm,
}