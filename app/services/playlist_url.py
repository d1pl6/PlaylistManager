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
- ``<host>/<path>`` path form for hosts whose every path is a resource
  URL (SoundCloud: ``/user/sets/<slug>``, ``/user/<slug>``,
  ``on.soundcloud.com/<token>``) - see ``_PATH_FORM_PLATFORMS``
"""

from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

_SUPPORTED_FORMS = (
    "YouTube Music: https://music.youtube.com/playlist?list=<id>",
    "YouTube: https://www.youtube.com/playlist?list=<id>",
    "Spotify: https://open.spotify.com/playlist/<id>",
    "Spotify URI: spotify:playlist:<id>",
    "SoundCloud: https://soundcloud.com/<user>/sets/<slug>",
)

# Platforms whose playlist URLs are just "<host>/<path>" - every path on a
# declared host is a resource URL, so there is no shared /playlist/ shape
# to match.  The path (without the leading slash) is returned as the
# stored playlist id; the integration resolves it via /resolve on first
# use.  A platform opting in here never reaches the "/playlist" token
# scan below (a SoundCloud user could legitimately be named "playlist").
_PATH_FORM_PLATFORMS = frozenset({"soundcloud"})


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

    # Path-form platforms (SoundCloud): skip the /playlist/ token scan -
    # every path on a declared host is a valid resource URL and the
    # "playlist" segment would mis-parse hosts where that is just a
    # username.  The stored id is the full path; the integration resolves
    # it through /resolve on first use (see _normalize_playlist_id).
    if pid in _PATH_FORM_PLATFORMS:
        if not segments:
            raise ValueError(f"No playlist path in URL '{url}'")
        return pid, "/".join(segments)

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

# Per-platform URL templates — fallback for plugins that don't declare
# their own in plugin.json.  The first path segment is the playlist
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


# SoundCloud URN prefixes per kind.  The SoundCloud flows store URNs
# (soundcloud:playlists:123 / soundcloud:tracks:123); the standalone
# repos follow the same convention.
_SOUNDCLOUD_URN_PREFIXES = {
    "playlist": ("soundcloud:playlists:", "soundcloud:playlist:"),
    "song": ("soundcloud:tracks:", "soundcloud:track:"),
}


def _soundcloud_url(playlist_id: str, kind: str) -> Optional[str]:
    """Map a NON-path SoundCloud id to a browseable URL, or ``None``.

    SoundCloud ids come in two shapes: URNs (what the official flows
    store) and ``user/slug`` paths (URL-registered entries).  Path ids
    map cleanly through the plugin's ``https://{host}/{id}`` template,
    so only URN / bare-numeric ids are routed here.

    A playlist URN maps to the numeric set page
    (``https://soundcloud.com/sets/123`` - reachable, verified 2026-09).
    A track URN has NO numeric page (``https://soundcloud.com/tracks/123``
    is a 404 that redirects to the charts) - ``None`` means "not
    browseable" and the caller skips the affordance instead of producing
    a dead link.
    """
    for prefix in _SOUNDCLOUD_URN_PREFIXES[kind]:
        if playlist_id.startswith(prefix):
            num = playlist_id[len(prefix):]
            if not num.isdigit():
                return None
            if kind == "playlist":
                return f"https://soundcloud.com/sets/{num}"
            return None
    # Bare numeric id (no "soundcloud:" prefix) - only playlists have a
    # browseable numeric page.
    if playlist_id.isdigit() and kind == "playlist":
        return f"https://soundcloud.com/sets/{playlist_id}"
    return None


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

    Template resolution order:

    1. Plugin-declared ``playlist_url_template`` in plugin.json
       (new platforms declare their own URL shape).
    2. Hardcoded ``_PLATFORM_URL_TEMPLATES`` (backward-compatible).
    3. Generic ``https://<host>/<id>`` with the resolved host.
    """
    if not playlist_id:
        return None

    # SoundCloud stores URNs (soundcloud:playlists:123); the only
    # browseable numeric page is /sets/<id>.  Path-shaped ids
    # ("user/sets/slug") fall through to the template machinery below.
    if platform == "soundcloud" and "/" not in playlist_id:
        return _soundcloud_url(playlist_id, "playlist")

    template = None

    # 1. Try plugin-declared template.
    try:
        if plugin_registry is None:
            from plugin_loader import get_default_registry
            plugin_registry = get_default_registry()
        plugin = plugin_registry.get(platform)
        if plugin and plugin.playlist_url_template:
            template = plugin.playlist_url_template
    except Exception:
        pass

    # 2. Fall back to hardcoded template.
    if not template:
        templates = _PLATFORM_URL_TEMPLATES.get(platform)
        if templates and templates[0]:
            template = templates[0]

    if template:
        host = _resolve_host(platform, plugin_registry) or ""
        return template.format(host=host, id=playlist_id)

    # 3. Unknown platform — try generic ``/<id>`` with the resolved host.
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

    Template resolution mirrors :func:`build_playlist_url`.
    """
    if not track_id:
        return None

    # SoundCloud track URNs have no browseable page (see _soundcloud_url);
    # path-shaped ids fall through to the template machinery below.
    if platform == "soundcloud" and "/" not in track_id:
        return _soundcloud_url(track_id, "song")

    template = None

    # 1. Try plugin-declared template.
    try:
        if plugin_registry is None:
            from plugin_loader import get_default_registry
            plugin_registry = get_default_registry()
        plugin = plugin_registry.get(platform)
        if plugin and plugin.song_url_template:
            template = plugin.song_url_template
    except Exception:
        pass

    # 2. Fall back to hardcoded template.
    if not template:
        templates = _PLATFORM_URL_TEMPLATES.get(platform)
        if templates and templates[1]:
            template = templates[1]

    if template:
        host = _resolve_host(platform, plugin_registry) or ""
        return template.format(host=host, id=track_id)

    host = _resolve_host(platform, plugin_registry)
    if host:
        return f"https://{host}/{track_id}"
    return None
