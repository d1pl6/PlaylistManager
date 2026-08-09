"""
Playlist URL parsing for the CLI.

Maps a platform playlist URL to a stable ``(platform, playlist_id)`` pair.
Used by ``playlistmanager -p add <URL>`` and by ``del`` / ``ref`` URL targets.
Pure stdlib, no network.
"""

from typing import Tuple
from urllib.parse import parse_qs, urlparse

from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC

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


def parse_playlist_url(url: str) -> Tuple[str, str]:
    """Return ``(platform, playlist_id)`` for a supported playlist URL.

    Raises :class:`ValueError` with a human-readable reason when the URL is
    not a recognizable playlist URL (including song URLs).
    """
    url = url.strip()
    if not url:
        raise ValueError("Empty URL")

    # Spotify URI form: spotify:playlist:<id>
    if url.startswith("spotify:"):
        parts = url.split(":")
        if len(parts) >= 3 and parts[1] == "playlist" and parts[2]:
            return PLATFORM_SPOTIFY, parts[2].split("?")[0]
        raise _unsupported(url)

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _unsupported(url)

    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/")

    # YouTube: /playlist?list=<id> on music.youtube.com or (www.)youtube.com
    if host in ("music.youtube.com", "www.youtube.com", "youtube.com"):
        if path == "/playlist":
            list_id = (parse_qs(parsed.query).get("list") or [""])[0]
            if not list_id:
                raise ValueError(
                    f"No 'list' query parameter in YouTube URL '{url}'"
                )
            return PLATFORM_YOUTUBE_MUSIC, list_id
        if path.startswith("/watch"):
            raise ValueError(
                "That is a song URL, not a playlist URL - use "
                "'playlistmanager -a' to add the currently-playing song"
            )
        raise ValueError(
            f"Unrecognized YouTube URL '{url}' - expected a /playlist?list=<id> URL"
        )

    # Spotify: /playlist/<id>, optionally prefixed by a locale segment:
    # /playlist/<id>, /intl-<locale>/playlist/<id>
    if host == "open.spotify.com":
        segments = [s for s in path.split("/") if s]
        try:
            idx = segments.index("playlist")
        except ValueError:
            raise ValueError(
                f"Unrecognized Spotify URL '{url}' - expected /playlist/<id>"
            )
        if len(segments) <= idx + 1 or not segments[idx + 1]:
            raise ValueError(f"No playlist ID in Spotify URL '{url}'")
        return PLATFORM_SPOTIFY, segments[idx + 1].split("?")[0]

    raise _unsupported(url)
