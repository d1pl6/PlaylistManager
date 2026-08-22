"""
YouTube Music integration.

The platform identifier is the plain string ``"youtube_music"``, matching
this plugin's plugin.json ``id`` - plugin.json is the single declaration;
the class attribute mirrors it for IntegrationRegistry keying.
"""

import logging
from typing import Optional

from services.integration import BaseIntegration

logger = logging.getLogger(__name__)


class YouTubeMusicIntegration(BaseIntegration):
    id = "youtube_music"
    display_name = "YouTube Music"

    def __init__(self, auth_manager=None):
        self._auth = auth_manager
        self.yt_client = None

    def authenticate(self) -> bool:
        if self._auth is None:
            return False
        if self._auth.setup_auth():
            self.yt_client = self._auth.get_yt_music()
            return True
        return False

    def is_authenticated(self) -> bool:
        return self.yt_client is not None

    def refresh_auth(self) -> bool:
        self.yt_client = None
        return self.authenticate()

    def get_library_playlists(self) -> list:
        if not self.yt_client:
            return []
        try:
            # Filter to playlists the user can actually add songs to.
            # ytmusicapi's parse marks each item with "owned" (the library
            # browse can surface followed/saved playlists the user is not a
            # collaborator on - adding to them fails on the platform side).
            # Items without the flag are kept defensively so a parser change
            # can never hide an owned playlist.
            return [
                p
                for p in self.yt_client.get_library_playlists()
                if p.get("owned") is not False
            ]
        except Exception as e:
            logger.error(f"YouTube Music: failed to get library playlists: {e}")
            return []

    def get_playlist_details(self, playlist_id: str, limit: int = 1) -> dict:
        if not self.yt_client:
            return {}
        try:
            return self.yt_client.get_playlist(playlist_id, limit=limit)
        except Exception as e:
            logger.error(f"YouTube Music: failed to get playlist details: {e}")
            return {}

    def get_playlist_id(self, name: str) -> Optional[str]:
        if not self.yt_client:
            return None
        try:
            playlists = self.yt_client.get_library_playlists()
            for playlist in playlists:
                if playlist.get("owned") is not False and playlist.get("title") == name:
                    return playlist.get("playlistId")
            return None
        except Exception as e:
            logger.error(f"YouTube Music: failed to get playlist ID: {e}")
            return None

    def get_playlist_tracks(self, playlist_id: str) -> list:
        if not self.yt_client:
            return []
        try:
            result = self.yt_client.get_playlist(playlist_id, limit=None)
            return result.get("tracks", [])
        except Exception as e:
            logger.error(f"YouTube Music: failed to get playlist tracks: {e}")
            return []

    def remove_track(self, playlist_id: str, track_id: str) -> bool:
        if not self.yt_client:
            return False
        try:
            # ytmusicapi's remove_playlist_items requires BOTH videoId and
            # setVideoId per item - setVideoId is the playlist-scoped id the
            # edit endpoint needs (only present when the playlist is
            # editable).  Fetch the playlist, locate the track and pass its
            # full item through; a track that is gone or lacks setVideoId
            # (non-owned playlist) is reported, not raised.
            playlist = self.yt_client.get_playlist(playlist_id, limit=None)
            tracks = playlist.get("tracks", [])
            target = next(
                (
                    t
                    for t in tracks
                    if t.get("videoId") == track_id and t.get("setVideoId")
                ),
                None,
            )
            if target is None:
                logger.warning(
                    "YouTube Music: track %s is not removable from playlist %s "
                    "(not in the playlist, or the playlist is not editable)",
                    track_id, playlist_id,
                )
                return False
            self.yt_client.remove_playlist_items(playlist_id, [target])
            logger.info(
                "YouTube Music: removed track %s from playlist %s",
                track_id, playlist_id,
            )
            return True
        except Exception as e:
            logger.error(
                f"YouTube Music: failed to remove track {track_id}: {e}"
            )
            return False
