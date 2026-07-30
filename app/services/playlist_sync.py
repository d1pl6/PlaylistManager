"""
Playlist track import / reload service.

Extracted from ``app/ui/main_window.py`` (Issue #1).  Handles the
threading and integration-API calls so the UI only needs to provide
thin callbacks for status updates.
"""

import logging
import threading
from typing import Callable, Optional

from services.song_manager import SongManager

logger = logging.getLogger(__name__)

# Type alias for import / reload result callbacks.
# (playlist_name, inserted_count, status_text[, thumbnail_url])
OnDoneCallback = Callable[[str, int, str], None]
OnReloadDoneCallback = Callable[[str, int, str, Optional[str]], None]


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

        *on_done* is called from the worker thread — the caller is
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
                from services.database import DatabaseManager
                db_path = DatabaseManager.get_playlist_db_path_static(
                    playlist_name, platform
                )
                if db_path.exists():
                    db_path.unlink()
                    logger.info("Deleted database for '%s'", playlist_name)

                details = integration.get_playlist_details(playlist_id)
                thumbnails = details.get("thumbnails") or details.get("thumbnail")
                thumb_url: Optional[str] = None
                if isinstance(thumbnails, list):
                    from utils.thumbnail import ThumbnailService
                    thumb_url = ThumbnailService.get_smallest_thumbnail(thumbnails)
                elif isinstance(thumbnails, str):
                    thumb_url = thumbnails

                tracks = integration.get_playlist_tracks(playlist_id)
                if not tracks:
                    on_done(playlist_name, 0, "No tracks", None)
                    return

                sm = SongManager()
                inserted = sm.add_songs_bulk(
                    playlist_name, tracks, platform=platform
                )

                if thumb_url:
                    from services.playlist_store import PlaylistStore
                    PlaylistStore.update_thumbnail(
                        playlist_name, platform, thumb_url
                    )

                on_done(playlist_name, inserted, f"{inserted} new", thumb_url)

            except Exception as e:
                logger.error("Reload failed for '%s': %s", playlist_name, e)
                on_done(playlist_name, 0, "Error", None)

        threading.Thread(target=_run, daemon=True).start()
