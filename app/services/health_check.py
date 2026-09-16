"""
Connectivity and service-health probes.

The platform-API TCP targets plus the coarse internet probe are shared
between the main window's reachability banners and the CLI's pre-flight
network check - one implementation instead of two copies.

Probes are blocking socket connects on the CALLING thread: GUI callers
run them in a daemon thread and marshal results to the UI; the CLI calls
them synchronously as a pre-flight gate before starting a network-bound
workflow.
"""

from __future__ import annotations

import logging
import socket
from socket import timeout as SocketTimeout

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