"""
Connectivity and service-health probes.

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

``audit_path_modes`` is the one LOCAL health check: it compares the
on-disk permission bits of the credential paths against the 0600/0700
contract the write paths enforce, so a tree restored over a filesystem
that ignores modes (or hand-edited) is reported instead of silently
hosting world-readable tokens.  It is a pure, stdlib-only primitive that
takes the paths as arguments; resolving the profile-aware credential
paths is the caller's job
(:func:`services.auth_setup.audit_credential_permissions`).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from socket import timeout as SocketTimeout
from typing import Callable

logger = logging.getLogger(__name__)

PLATFORM_API_TARGETS: dict[str, tuple[str, int]] = {
    "youtube_music": ("music.youtube.com", 443),
    "spotify": ("api.spotify.com", 443),
    "soundcloud": ("api.soundcloud.com", 443),
    "deezer": ("pipe.deezer.com", 443),
    "tidal": ("api.tidal.com", 443),
}

_INTERNET_PROBE = ("1.1.1.1", 53)
_INTERNET_TIMEOUT_S = 5.0
_API_TIMEOUT_S = 10.0
_AUTH_PROBE_TIMEOUT_S = 15.0


def check_network(timeout: float = _INTERNET_TIMEOUT_S) -> bool:
    """
    Used as the CLI pre-flight gate so network-less runs fail fast with
    a clear message instead of confusing per-request errors mid-flow.
    """
    try:
        with socket.create_connection(_INTERNET_PROBE, timeout=timeout):
            return True
    except (OSError, SocketTimeout):
        return False


def probe_platform(platform: str, timeout: float = _API_TIMEOUT_S) -> bool:
    """
    Return True when *platform*'s API endpoint accepts TCP.
    Unknown platforms probe False - there is no endpoint to reach.
    """
    target = PLATFORM_API_TARGETS.get(platform)
    if target is None:
        return False
    try:
        with socket.create_connection(target, timeout=timeout):
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
    """
    Run a live authenticated round trip against the platform's API.

    * ``ok`` is ``True`` - the platform accepted the credentials.
    * ``ok`` is ``False`` - the authenticated call failed: revoked or
      expired credentials, or an API error - the verdict a TCP probe
      cannot produce.
    * ``ok`` is ``None`` - no probe possible (no credentials loaded this
      session, no probe implementation for the platform, or the probe
      exceeded *timeout*).
    * *detail* is a short best-effort reason when ``ok`` is ``False``.
    """
    pid = getattr(integration, "id", "") or ""
    capability = getattr(integration, "probe_authenticated", None)
    box: list = []

    def _run() -> None:
        try:
            if callable(capability):
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
        # Token refresh rejected
        return (False, str(e)[:160])
    except Exception as e:
        return (False, str(e)[:160])
    return (True, None) if me else (False, None)


def _probe_youtube_music(integration) -> "tuple[bool | None, str | None]":
    client = getattr(integration, "yt_client", None)
    if client is None:
        return (None, None)
    try:
        # The same authenticated round trip the login verification
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


def _probe_listenbrainz(integration) -> "tuple[bool | None, str | None]":
    client = getattr(integration, "client", None)
    # No client, or no username: the token was never validated, so the
    # listens/delete endpoints cannot even be addressed.  "(None, None)"
    # means "no probe possible", never a false "auth failed".
    if client is None or not getattr(client, "username", ""):
        return (None, None)
    try:
        # validate-token, NOT listen-count: ListenBrainz's listen-count and
        # listens endpoints are PUBLIC and answer 200 with an empty payload
        # for a dead token, so probing either one reports a revoked token as
        # healthy - the exact false positive the Last.fm probe above
        # guards against.  validate-token answers 200 for a bad token too,
        # but the body's "valid" flag is definitive.
        result = client.validate_token() or {}
    except Exception as e:
        return (False, str(e)[:160])
    if not result.get("ok"):
        return (False, (str(result.get("error") or "token rejected"))[:160])
    return (True, None)


def _probe_tidal(integration) -> "tuple[bool | None, str | None]":
    api = getattr(integration, "tidal_api", None)
    if api is None:
        return (None, None)
    try:
        # /v1/sessions is the round trip the login verification itself
        # uses: it exercises the refresh-token exchange (a dead refresh
        # token raises TidalError, a RuntimeError subclass, from
        # TidalAPI._headers) and returns the identity back.
        me = api.get_me()
    except Exception as e:
        return (False, str(e)[:160])
    return (True, None) if me else (False, None)


_AUTH_PROBES: dict[
    str, Callable[[object], "tuple[bool | None, str | None]"]
] = {
    "youtube_music": _probe_youtube_music,
    "spotify": _probe_spotify,
    "soundcloud": _probe_soundcloud,
    "deezer": _probe_deezer,
    "lastfm": _probe_lastfm,
    "listenbrainz": _probe_listenbrainz,
    "tidal": _probe_tidal,
}


# ---------------------------------------------------------------------------
# Credential-permission audit
#
# The credential write paths enforce owner-only access: the auth dir is
# created/chmod-ed 0700 (auth_setup.setup_ytmusic_auth) and every
# credential file is written 0600 (the plugin atomic writers, the
# keyring-restore path).  Those bits are set once, at write time - they
# are not re-checked afterwards, so anything that changes them behind the
# app's back (a backup restored onto a filesystem that ignores POSIX
# modes, a copy through a FAT/NTFS share, a hand edit, a permissive
# umask on a file a third-party flow wrote) leaves live tokens readable
# by other users with no visible symptom.  This audit is the read-only
# check for that regression; it reports, it never chmods.
# ---------------------------------------------------------------------------

# The enforced contract: auth dir owner-only, credential file owner
# read/write only.  Same values the write paths pass to mkdir/chmod.
AUTH_DIR_MODE = 0o700
CREDENTIAL_FILE_MODE = 0o600


def supports_posix_modes() -> bool:
    """Whether permission bits on this platform describe real access.

    Windows synthesizes ``st_mode`` from the read-only attribute, so its
    low bits say nothing about who can read a file - auditing them would
    report "open permissions" on every Windows install forever.  Only
    POSIX semantics are meaningful, so the audit no-ops elsewhere.
    """
    return os.name == "posix"


@dataclass(frozen=True)
class PermissionFinding:
    """One path whose mode grants access beyond its expected mode."""

    path: Path
    expected: int
    actual: int

    @property
    def expected_str(self) -> str:
        return f"{self.expected:04o}"

    @property
    def actual_str(self) -> str:
        return f"{self.actual:04o}"


@dataclass(frozen=True)
class PermissionReport:
    """Outcome of :func:`audit_path_modes`.

    ``checked`` counts the paths that exist and could be read;
    ``findings`` holds at most one entry per path (duplicates in *specs*
    are collapsed - callers assemble the list from several sources and a
    doubled finding would double the user-facing count).
    """

    findings: tuple[PermissionFinding, ...] = ()
    checked: int = 0
    supported: bool = True

    @property
    def ok(self) -> bool:
        """True when nothing is looser than its contract (always True
        where :func:`supports_posix_modes` is False - see the note on
        Windows there)."""
        return not self.findings

    @property
    def first_path(self) -> Path | None:
        """The first offending path, for a one-line log message."""
        return self.findings[0].path if self.findings else None


def audit_path_modes(
    specs: Iterable[tuple[Path, int]],
) -> PermissionReport:
    """Check each ``(path, expected_mode)`` pair for access beyond it.

    A path is a finding when it grants *any* bit the expected mode does
    not: ``mode & 0o777 & ~expected``.  Two consequences worth stating:

    * MORE private is never a finding.  A 0400 credential file is fine -
      tightening is not the regression being looked for.
    * Merely DIFFERENT is not the test either.  An unexpected owner
      execute bit on a credential file is as much a deviation from what
      the app writes as a group-read bit is.

    Missing paths are not findings: there is no mode to verify, the app
    creates them on demand, and the Settings credentials panel already
    labels them "missing".  An unreadable path (permissions, a stale
    mount) is skipped rather than reported - the check never raises and
    never turns a stat failure into a scary warning.
    """
    if not supports_posix_modes():
        return PermissionReport(supported=False)

    findings: list[PermissionFinding] = []
    seen: set[str] = set()
    checked = 0
    for path, expected in specs:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            mode = path.stat().st_mode & 0o777
        except OSError as e:
            logger.debug("Could not stat %s for the permission audit: %s", path, e)
            continue
        checked += 1
        if mode & ~expected:
            logger.debug(
                "Permission audit: %s is %04o, expected %04o",
                path,
                mode,
                expected,
            )
            findings.append(
                PermissionFinding(path=path, expected=expected, actual=mode)
            )
    return PermissionReport(findings=tuple(findings), checked=checked)
