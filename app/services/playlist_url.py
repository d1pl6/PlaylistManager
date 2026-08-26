"""
Playlist URL parsing and building.

Maps a platform playlist URL to a stable ``(platform, playlist_id)`` pair
(:func:`parse_playlist_url`), and builds browseable URLs from platform
metadata (:func:`build_playlist_url`, :func:`build_song_url`).

Used by ``playlistmanager -p add <URL>``, by ``del`` / ``ref`` URL
targets, and by the UI (click-to-open in card_grid / showcase_manager).

Host knowledge lives in the plugin manifests (``url_hosts`` in each
integrations/*/plugin.json); this module matches only generic URL shapes:

- ``/playlist?list=<id>`` query-parameter form (YouTube Music and friends)
- ``/playlist/<id>`` path form, optionally behind a locale segment
  (``/intl-<xx>/playlist/<id>``) - Spotify
- ``<platform>:playlist:<id>`` URI form - Spotify
"""

from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

_SUPPORTED_FORMS = (
    "YouTube Music: https://music.youtube.com/playlist?list=<id>",
    "YouTube: https://www.youtube.com/playlist?list=<id>",
    "Spotify: https://open.spotify.com/playlist/<id>",
    "Spotify URI: spotify:playlist:<id>",
)


def _unsupported(url: str) -> ValueError:
    return ValueError(
        f"Unrecognized playlist URL '{url}' - expected one of:\n"
        + "\n".join(f"  {form}" for form in _SUPPORTED_FORMS)
    )


def _song_url_hint(url: str) -> ValueError:
    return ValueError(
        "That is a song URL, not a playlist URL - use "
        "'playlistmanager -a' to add the currently-playing song"
    )


def _host_registry(plugin_registry=None):
    """Map every declared url_host -> platform id, plus id -> id for the
    URI-scheme form (``spotify:playlist:<id>``)."""
    if plugin_registry is None:
        # Late import: keeps this module importable without the loader
        # (and avoids any import cycle when embedded elsewhere).
        from plugin_loader import get_default_registry

        plugin_registry = get_default_registry()
    hosts = {}
    for pid, plugin in plugin_registry.get_all().items():
        for host in plugin.url_hosts:
            hosts[host.lower()] = pid
        hosts[pid.lower()] = pid  # URI scheme == platform id
    return hosts


def parse_playlist_url(
    url: str, plugin_registry=None
) -> Tuple[str, str]:
    """Return ``(platform, playlist_id)`` for a supported playlist URL.

    The platform is resolved from the manifests' declared ``url_hosts``
    (or the URI scheme matching a platform id).  Raises :class:`ValueError`
    with a human-readable reason when the URL is not a recognizable
    playlist URL (including song URLs).
    """
    url = url.strip()
    if not url:
        raise ValueError("Empty URL")

    host_map = _host_registry(plugin_registry)

    # URI form: <platform-id>:playlist:<id>, e.g. spotify:playlist:<id>
    # (strip any ?query/#fragment). Scheme and type token are matched
    # case-insensitively (RFC 3986); the ID keeps its case (Spotify IDs
    # are case-sensitive base62).
    parts = url.split(":")
    if len(parts) >= 2 and parts[0].lower() in host_map:
        pid = parts[0].lower()
        if len(parts) >= 3 and parts[1].lower() == "playlist" and parts[2]:
            return pid, parts[2].split("?")[0].split("#")[0]
        raise _unsupported(url)

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _unsupported(url)

    # urlparse normalizes the scheme and hostname but not the path - compare
    # host and path tokens case-insensitively so pasted/typed URL variants
    # work.
    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/")

    # youtu.be shortlinks are always song URLs, never playlists.  Not a
    # declared plugin host - shared hint before the generic failure.
    if host == "youtu.be":
        raise _song_url_hint(url)

    pid = host_map.get(host)
    if pid is None:
        raise _unsupported(url)

    # Query-parameter form: /playlist?list=<id>.
    if path.lower() == "/playlist":
        list_id = (parse_qs(parsed.query).get("list") or [""])[0]
        if not list_id:
            raise ValueError(f"No 'list' query parameter in URL '{url}'")
        return pid, list_id
    if path.lower().startswith("/watch"):
        raise _song_url_hint(url)

    # Path form: /playlist/<id>, optionally prefixed by a locale segment:
    # /playlist/<id>, /intl-<locale>/playlist/<id>. The "playlist" token is
    # matched case-insensitively, but the ID keeps its original case
    # (Spotify IDs are case-sensitive base62).
    segments = [s for s in path.split("/") if s]
    try:
        idx = [s.lower() for s in segments].index("playlist")
    except ValueError:
        raise ValueError(
            f"Unrecognized URL '{url}' on {host} - expected "
            "/playlist?list=<id> or /playlist/<id>"
        )
    if len(segments) <= idx + 1 or not segments[idx + 1]:
        raise ValueError(f"No playlist ID in URL '{url}'")
    return pid, segments[idx + 1]


# ------------------------------------------------------------------
# URL building (inverse of parsing)
# ------------------------------------------------------------------

# Per-platform URL templates.  The first path segment is the playlist
# URL shape, the second is the song/track URL shape.  ``None`` means
# "use the plugin host with a generic ``/<id>`` fallback".
_PLATFORM_URL_TEMPLATES: dict[str, tuple[str | None, str | None]] = {
    "youtube_music": (
        "https://{host}/playlist?list={id}",
        "https://{host}/watch?v={id}",
    ),
    "spotify": (
        "https://{host}/playlist/{id}",
        "https://{host}/track/{id}",
    ),
}

# Hardcoded fallback hosts (used when the plugin manifest doesn't declare
# url_hosts or the registry can't be loaded).
_PLATFORM_DEFAULT_HOSTS: dict[str, str] = {
    "youtube_music": "music.youtube.com",
    "spotify": "open.spotify.com",
}


def _resolve_host(
    platform: str,
    plugin_registry=None,
) -> str | None:
    """Return the best-guess host for *platform* from the plugin registry."""
    try:
        if plugin_registry is None:
            from plugin_loader import get_default_registry
            plugin_registry = get_default_registry()
        plugin = plugin_registry.get(platform)
        if plugin and plugin.url_hosts:
            return plugin.url_hosts[0]
    except Exception:
        pass
    return _PLATFORM_DEFAULT_HOSTS.get(platform)


def build_playlist_url(
    platform: str,
    playlist_id: str,
    plugin_registry=None,
) -> str | None:
    """Build a browseable playlist URL from platform metadata.

    Returns ``None`` when *playlist_id* is empty or the platform is unknown.
    """
    if not playlist_id:
        return None
    templates = _PLATFORM_URL_TEMPLATES.get(platform)
    if templates and templates[0]:
        host = _resolve_host(platform, plugin_registry) or ""
        return templates[0].format(host=host, id=playlist_id)
    # Unknown platform — try generic ``/<id>`` with the resolved host.
    host = _resolve_host(platform, plugin_registry)
    if host:
        return f"https://{host}/{playlist_id}"
    return None


def build_song_url(
    platform: str,
    track_id: str,
    plugin_registry=None,
) -> str | None:
    """Build a browseable song/track URL from platform metadata.

    Returns ``None`` when *track_id* is empty or the platform is unknown.
    """
    if not track_id:
        return None
    templates = _PLATFORM_URL_TEMPLATES.get(platform)
    if templates and templates[1]:
        host = _resolve_host(platform, plugin_registry) or ""
        return templates[1].format(host=host, id=track_id)
    host = _resolve_host(platform, plugin_registry)
    if host:
        return f"https://{host}/{track_id}"
    return None
