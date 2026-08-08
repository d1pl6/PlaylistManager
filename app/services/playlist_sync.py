"""
Playlist track import / reload service.

Extracted from ``app/ui/main_window.py`` (Issue #1).  Handles the
threading and integration-API calls so the UI only needs to provide
thin callbacks for status updates.
"""

import logging
import threading
from typing import Callable, Optional

from constants import PLATFORM_YOUTUBE_MUSIC
from services.database import DatabaseManager
from services.playlist_store import PlaylistStore
from services.song_manager import SongManager
from utils.thumbnail import ThumbnailService

logger = logging.getLogger(__name__)

# Type alias for import / reload result callbacks.
# (playlist_name, inserted_count, status_text[, thumbnail_url])
OnDoneCallback = Callable[[str, int, str], None]
OnReloadDoneCallback = Callable[[str, int, str, Optional[str]], None]


def _extract_thumbnail(data: dict) -> Optional[str]:
    """Extract a thumbnail URL from an API response dict.

    Accepts either a ``thumbnails`` list of ``{"url": ...}`` dicts (the
    smallest by area wins) or a bare ``thumbnail`` URL string.
    """
    return ThumbnailService.from_data(data)


class PlaylistSyncService:
    """Import and reload playlist tracks in background threads."""

    def __init__(self, integrations) -> None:
        self.integrations = integrations

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def import_tracks(
        self,
        playlist_name: str,
        platform: str,
        playlist_id: str,
        on_done: OnDoneCallback,
    ) -> None:
        """Import tracks into the local database in a daemon thread.

        *on_done* is called from the worker thread - the caller is
        responsible for routing it back to the main thread (e.g. via
        ``root.after()``) if it touches tkinter widgets.
        """
        if not playlist_id:
            logger.warning("No playlist_id for '%s', skipping import", playlist_name)
            on_done(playlist_name, 0, "No tracks")
            return

        integration = self.integrations.get(platform)
        if integration is None:
            logger.warning("No integration for platform '%s'", platform)
            on_done(playlist_name, 0, "Error")
            return

        def _run() -> None:
            try:
                tracks = integration.get_playlist_tracks(playlist_id)
                if not tracks:
                    on_done(playlist_name, 0, "No tracks")
                    return
                sm = SongManager()
                inserted = sm.add_songs_bulk(playlist_name, tracks, platform=platform)
                on_done(playlist_name, inserted, f"{inserted} new")
            except Exception as e:
                logger.error("Import failed for '%s': %s", playlist_name, e)
                on_done(playlist_name, 0, "Error")

        threading.Thread(target=_run, daemon=True).start()

    def reload_database(
        self,
        playlist_name: str,
        platform: str,
        playlist_id: str,
        on_done: OnReloadDoneCallback,
    ) -> None:
        """Delete the local database and re-import all tracks.

        Also fetches the latest thumbnail URL and persists it to
        *PlaylistStore*.

        *on_done* is called from the worker thread.
        """
        if not playlist_id:
            logger.warning("No playlist_id for '%s', cannot reload", playlist_name)
            return

        integration = self.integrations.get(platform)
        if integration is None:
            on_done(playlist_name, 0, "Error", None)
            return

        def _run() -> None:
            try:
                DatabaseManager.delete_playlist_db(playlist_name, platform)
                logger.info("Deleted database for '%s'", playlist_name)

                details = integration.get_playlist_details(playlist_id)
                thumb_url = _extract_thumbnail(details)

                # Only the YouTube integration distinguishes custom
                # uploaded playlist images from auto-derived ones.  The
                # library listing is the same source the add flow uses
                # and reliably surfaces custom images, so prefer it.
                if platform == PLATFORM_YOUTUBE_MUSIC:
                    try:
                        for p in integration.get_library_playlists():
                            if p.get("playlistId") != playlist_id:
                                continue
                            lib_url = _extract_thumbnail(p)
                            if lib_url:
                                thumb_url = lib_url
                            break
                    except Exception as e:
                        logger.debug(
                            "Thumbnail library lookup failed for '%s': %s",
                            playlist_name,
                            e,
                        )

                tracks = integration.get_playlist_tracks(playlist_id)

                # Persist the thumbnail regardless of the track import -
                # an empty playlist must still get its cover refreshed.
                if thumb_url:
                    PlaylistStore.update_thumbnail(playlist_name, platform, thumb_url)

                if not tracks:
                    on_done(playlist_name, 0, "No tracks", thumb_url)
                    return

                sm = SongManager()
                inserted = sm.add_songs_bulk(
                    playlist_name, tracks, platform=platform
                )

                on_done(playlist_name, inserted, f"{inserted} new", thumb_url)

            except Exception as e:
                logger.error("Reload failed for '%s': %s", playlist_name, e)
                on_done(playlist_name, 0, "Error", None)

        threading.Thread(target=_run, daemon=True).start()
