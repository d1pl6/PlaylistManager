"""
Playlist track import / reload service.

Handles the threading and integration-API calls so the UI only needs to
provide thin callbacks for status updates.
"""

import logging
import threading
from typing import Callable

from services.database import DatabaseManager
from services.playlist_store import PlaylistStore
from services.song_manager import SongManager
from utils.thumbnail import ThumbnailService

logger = logging.getLogger(__name__)

# How long reload_database() waits for an in-flight add to drain before
# giving up and reporting an error (the add path can sit in a URL-receiver
# capture for up to URL_WAIT_TIMEOUT, plus the platform API round trip).
GATE_ACQUIRE_TIMEOUT = 60.0

# Type alias for import / reload result callbacks.
# (playlist_name, inserted_count, status_text[, thumbnail_url])
OnDoneCallback = Callable[[str, int, str], None]
OnReloadDoneCallback = Callable[[str, int, str, str | None], None]


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
            on_done(playlist_name, 0, "no_tracks")
            return

        if self.integrations.get(platform) is None:
            logger.warning("No integration for platform '%s'", platform)
            on_done(playlist_name, 0, "error")
            return

        def _run() -> None:
            try:
                inserted, status = self.import_tracks_sync(
                    playlist_name, platform, playlist_id
                )
                # "download" mode: persist every song thumb to disk so the
                # playlist is fully available offline (background thread).
                ThumbnailService.warm_song_cache(playlist_name, platform, playlist_id)
                on_done(playlist_name, inserted, status)
            except Exception as e:
                logger.error("Import failed for '%s': %s", playlist_name, e)
                on_done(playlist_name, 0, "error")

        threading.Thread(target=_run, daemon=True).start()

    def reload_database(
        self,
        playlist_name: str,
        platform: str,
        playlist_id: str,
        on_done: OnReloadDoneCallback,
        gate: threading.Lock | None = None,
    ) -> None:
        """Delete the local database and re-import all tracks.

        Also fetches the latest thumbnail URL and persists it to
        *PlaylistStore*.

        *on_done* is called from the worker thread.

        *gate* is an optional lock the worker holds (blocking acquire) for
        the whole reload.  The delete+reimport window races an in-flight
        keybind flow: the flow's write would land on the just-unlinked DB
        file (rows silently lost on the orphaned inode).  Passing the
        keybind controller's ``flow_busy()`` lock serializes the two - the
        reload waits for running adds to drain, and adds that fire during
        the reload are skipped by the keybind's non-blocking acquire and
        reported as "another add in progress".
        """
        if not playlist_id:
            logger.warning("No playlist_id for '%s', cannot reload", playlist_name)
            on_done(playlist_name, 0, "no_tracks", None)
            return

        integration = self.integrations.get(platform)
        if integration is None:
            on_done(playlist_name, 0, "error", None)
            return

        def _run() -> None:
            lock_gate = gate
            if lock_gate is not None:
                # Drain any in-flight add before deleting the DB (see
                # docstring).  The keybind side acquires non-blocking and
                # skips, so there is no circular wait.  A lock that never
                # frees (e.g. a hung platform API round trip) must not wedge
                # the reload thread forever - give up after a sane ceiling.
                if not lock_gate.acquire(timeout=GATE_ACQUIRE_TIMEOUT):
                    logger.error(
                        "Could not acquire the flow gate within %.0fs - "
                        "reload aborted for '%s'",
                        GATE_ACQUIRE_TIMEOUT, playlist_name,
                    )
                    on_done(playlist_name, 0, "error", None)
                    return
            try:
                inserted, status, thumb_url = self.reload_database_sync(
                    playlist_name, platform, playlist_id
                )
                # "download" mode: persist every song thumb to disk so the
                # playlist is fully available offline (background thread).
                ThumbnailService.warm_song_cache(playlist_name, platform, playlist_id)
                on_done(playlist_name, inserted, status, thumb_url)
            except Exception as e:
                logger.error("Reload failed for '%s': %s", playlist_name, e)
                on_done(playlist_name, 0, "error", None)
            finally:
                if lock_gate is not None:
                    lock_gate.release()

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
            return 0, "no_tracks"
        # The platform may have been uninstalled while the fetch was in
        # flight - a Manage-dialog uninstall deletes db/<platform>/ and this
        # call would otherwise silently recreate it.  Abort like a dead
        # integration (RuntimeError) instead of resurrecting the database.
        if self.integrations.get(platform) is None:
            raise RuntimeError(f"no integration for platform '{platform}'")
        sm = SongManager()
        inserted = sm.add_songs_bulk(
            playlist_name, tracks, platform=platform, playlist_id=playlist_id
        )
        return inserted, "ok"

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
        thumb_url = ThumbnailService.from_data(details)
        thumb_url = self.prefer_library_thumbnail(
            platform, integration, playlist_id, thumb_url
        )

        # Refresh follower count (Spotify playlists expose this in
        # details; YouTube Music playlists have no equivalent).
        follower_count = details.get("followerCount")
        if follower_count:
            PlaylistStore.update_metadata(
                playlist_name, platform, playlist_id,
                follower_count=follower_count,
            )

        # Fetch the tracks BEFORE deleting the local database.  The details
        # check above proves the playlist is reachable, but a track-fetch
        # failure right after the delete (network flake) would still destroy
        # the cached tracks - and the fetch is the slowest step, so the old
        # DB keeps serving reads until the swap.  add_songs_bulk consumes the
        # already-fetched list, so nothing is fetched twice.
        tracks = integration.get_playlist_tracks(playlist_id)

        # Platform uninstalled while the fetch was in flight: abort before
        # deleting/recreating the local DB - the uninstall cleanup must
        # not be undone from a stale worker (see import_tracks_sync).
        if self.integrations.get(platform) is None:
            raise RuntimeError(f"no integration for platform '{platform}'")

        DatabaseManager.delete_playlist_db(playlist_name, platform, playlist_id)
        logger.info("Deleted database for '%s'", playlist_name)

        # Persist the thumbnail regardless of the track import -
        # an empty playlist must still get its cover refreshed.
        if thumb_url:
            PlaylistStore.update_thumbnail(playlist_name, platform, thumb_url)

        if not tracks:
            return 0, "no_tracks", thumb_url

        sm = SongManager()
        inserted = sm.add_songs_bulk(
            playlist_name, tracks, platform=platform, playlist_id=playlist_id
        )

        return inserted, "ok", thumb_url

    @staticmethod
    def prefer_library_thumbnail(
        platform: str,
        integration,
        playlist_id: str,
        details_thumb: str | None,
    ) -> str | None:
        """Prefer the library-listing thumbnail for YouTube playlists.

        Only the YouTube integration distinguishes custom uploaded playlist
        images from auto-derived ones.  The library listing is the same
        source the add flow uses and reliably surfaces custom images, so it
        wins over the details header.  Other platforms return *details_thumb*
        unchanged.
        """
        if platform != "youtube_music":
            return details_thumb
        try:
            for p in integration.get_library_playlists():
                if p.get("playlistId") != playlist_id:
                    continue
                lib_url = ThumbnailService.from_data(p)
                if lib_url:
                    return lib_url
                break
        except Exception as e:
            logger.debug(
                "Thumbnail library lookup failed for '%s': %s", playlist_id, e
            )
        return details_thumb
