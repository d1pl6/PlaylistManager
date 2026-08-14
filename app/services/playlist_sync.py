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
    # Public API (threaded, GUI path)
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

        if self.integrations.get(platform) is None:
            logger.warning("No integration for platform '%s'", platform)
            on_done(playlist_name, 0, "Error")
            return

        def _run() -> None:
            try:
                inserted, status = self.import_tracks_sync(
                    playlist_name, platform, playlist_id
                )
                on_done(playlist_name, inserted, status)
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
            on_done(playlist_name, 0, "No tracks", None)
            return

        integration = self.integrations.get(platform)
        if integration is None:
            on_done(playlist_name, 0, "Error", None)
            return

        def _run() -> None:
            try:
                inserted, status, thumb_url = self.reload_database_sync(
                    playlist_name, platform, playlist_id
                )
                on_done(playlist_name, inserted, status, thumb_url)
            except Exception as e:
                logger.error("Reload failed for '%s': %s", playlist_name, e)
                on_done(playlist_name, 0, "Error", None)

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Synchronous core (CLI path) - the threaded methods above delegate
    # here so GUI and CLI never drift apart.
    # ------------------------------------------------------------------

    def import_tracks_sync(
        self,
        playlist_name: str,
        platform: str,
        playlist_id: str,
    ) -> tuple:
        """Import tracks into the local database, blocking.

        Returns ``(inserted_count, status_text)``.  Raises
        :class:`RuntimeError` when the platform integration is unavailable.
        """
        if not playlist_id:
            raise RuntimeError(
                f"no playlist_id for '{playlist_name}' - cannot import"
            )
        integration = self.integrations.get(platform)
        if integration is None:
            raise RuntimeError(f"no integration for platform '{platform}'")

        tracks = integration.get_playlist_tracks(playlist_id)
        if not tracks:
            return 0, "No tracks"
        sm = SongManager()
        inserted = sm.add_songs_bulk(playlist_name, tracks, platform=platform)
        return inserted, f"{inserted} new"

    def reload_database_sync(
        self,
        playlist_name: str,
        platform: str,
        playlist_id: str,
    ) -> tuple:
        """Delete the local database and re-import all tracks, blocking.

        Also fetches the latest thumbnail URL and persists it to
        *PlaylistStore*.  Returns ``(inserted_count, status_text,
        thumb_url)``.  Raises :class:`RuntimeError` on platform failures
        (no integration, or the playlist details could not be fetched).
        """
        if not playlist_id:
            raise RuntimeError(
                f"no playlist_id for '{playlist_name}' - re-add it with "
                "'playlistmanager -p add <URL>'"
            )
        integration = self.integrations.get(platform)
        if integration is None:
            raise RuntimeError(f"no integration for platform '{platform}'")

        # Confirm the playlist is reachable BEFORE destroying the local DB -
        # a failed refresh (deleted/private playlist, network error) must
        # not lose the cached tracks.
        details = integration.get_playlist_details(playlist_id)
        if not details:
            raise RuntimeError(
                f"could not fetch details for '{playlist_name}' "
                f"from platform '{platform}'"
            )
        thumb_url = _extract_thumbnail(details)
        thumb_url = self.prefer_library_thumbnail(
            platform, integration, playlist_id, thumb_url
        )

        # Fetch the tracks BEFORE deleting the local database.  The details
        # check above proves the playlist is reachable, but a track-fetch
        # failure right after the delete (network flake) would still destroy
        # the cached tracks - and the fetch is the slowest step, so the old
        # DB keeps serving reads until the swap.  add_songs_bulk consumes the
        # already-fetched list, so nothing is fetched twice.
        tracks = integration.get_playlist_tracks(playlist_id)

        DatabaseManager.delete_playlist_db(playlist_name, platform)
        logger.info("Deleted database for '%s'", playlist_name)

        # Persist the thumbnail regardless of the track import -
        # an empty playlist must still get its cover refreshed.
        if thumb_url:
            PlaylistStore.update_thumbnail(playlist_name, platform, thumb_url)

        if not tracks:
            return 0, "No tracks", thumb_url

        sm = SongManager()
        inserted = sm.add_songs_bulk(playlist_name, tracks, platform=platform)

        return inserted, f"{inserted} new", thumb_url

    @staticmethod
    def prefer_library_thumbnail(
        platform: str,
        integration,
        playlist_id: str,
        details_thumb: Optional[str],
    ) -> Optional[str]:
        """Prefer the library-listing thumbnail for YouTube playlists.

        Only the YouTube integration distinguishes custom uploaded playlist
        images from auto-derived ones.  The library listing is the same
        source the add flow uses and reliably surfaces custom images, so it
        wins over the details header.  Other platforms return *details_thumb*
        unchanged.
        """
        if platform != PLATFORM_YOUTUBE_MUSIC:
            return details_thumb
        try:
            for p in integration.get_library_playlists():
                if p.get("playlistId") != playlist_id:
                    continue
                lib_url = _extract_thumbnail(p)
                if lib_url:
                    return lib_url
                break
        except Exception as e:
            logger.debug(
                "Thumbnail library lookup failed for '%s': %s", playlist_id, e
            )
        return details_thumb
