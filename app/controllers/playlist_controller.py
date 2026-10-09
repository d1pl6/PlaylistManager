"""
Playlist dialog workflow controller.

Extracted from ``app/ui/main_window.py``.  Orchestrates the "add
playlist" flow (platform choice, playlist list, selection) without
directly creating tkinter widgets.  UI actions are delegated to
callbacks provided by the window layer.
"""

import logging
import threading
from typing import Callable, Optional

from services.playlist_store import PlaylistStore

logger = logging.getLogger(__name__)


class PlaylistController:
    """
    Handles the "add playlist" workflow.
    """

    def __init__(
        self,
        parent,
        integrations,
        *,
        on_show_platform_picker: Callable,
        on_show_playlist_dialog: Callable,
        on_add_playlist_frame: Callable,
        on_dialog_cancel: Callable,
        on_show_error: Callable,
        on_refresh: Optional[Callable] = None,
    ) -> None:
        self.parent = parent
        self.integrations = integrations

        # UI callbacks
        self._on_show_platform_picker = on_show_platform_picker
        self._on_show_playlist_dialog = on_show_playlist_dialog
        self._on_add_playlist_frame = on_add_playlist_frame
        self._on_dialog_cancel = on_dialog_cancel
        self._on_show_error = on_show_error
        self._on_refresh = on_refresh

        # State
        self._workflow_open = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def open_playlist_dialog(self, attempt: int = 0) -> None:
        """
        Start the "add playlist" workflow.
        """
        if self._workflow_open:
            return
        self._workflow_open = True

        active = self.integrations.get_active()
        if not active:
            if attempt == 0 and self._on_refresh is not None:
                def retry() -> None:
                    self._workflow_open = False
                    self.open_playlist_dialog(attempt=1)

                self._on_refresh(on_done=retry)
                return
            self._workflow_open = False
            self._on_show_error()
            return

        if len(active) == 1:
            platform_id = next(iter(active))
            self._fetch_and_show_playlists(active[platform_id])
        else:
            self._choose_platform(active)

    # ------------------------------------------------------------------
    # Internal workflow steps
    # ------------------------------------------------------------------

    def _choose_platform(self, active_integrations) -> None:
        """
        Show the platform picker when multiple integrations are active.
        """
        platforms = list(active_integrations.values())

        def on_pick(integration):
            self._fetch_and_show_playlists(integration)

        self._on_show_platform_picker(
            platforms,
            callback=on_pick,
            on_cancel=self._on_cancel,
        )

    def _fetch_and_show_playlists(self, integration, attempt: int = 0) -> None:
        """
        Fetch playlists from *integration* and show them to the user.
        """


        def _worker() -> None:
            try:
                playlists = integration.get_library_playlists()
            except Exception as e:
                logger.error("Failed to fetch playlists: %s", e)
                self._async_ui(lambda: self._on_fetch_failed(integration, attempt))
                return

            if not playlists:
                self._async_ui(lambda: self._on_fetch_failed(integration, attempt))
                return

            self._async_ui(lambda: self._show_filtered_playlists(playlists, integration))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_fetch_failed(self, integration, attempt: int) -> None:
        """
        Handle a failed/empty playlist fetch (main thread).
        """

        if attempt == 0 and self._on_refresh is not None:
            def retry() -> None:
                self._fetch_and_show_playlists(integration, attempt=1)

            self._on_refresh(integration.id, on_done=retry)
            return
        self._cancel_and_show_error()

    def _async_ui(self, fn: Callable) -> None:
        """Run *fn* on the UI thread; drop it if the app is shutting down."""
        try:
            self.parent.after(0, fn)
        except Exception:
            logger.debug(
                "App is shutting down; dropped deferred playlist-dialog action"
            )

    def _show_filtered_playlists(self, playlists, integration) -> None:
        """Filter out already-added playlists and show the dialog."""
        # Exclude playlists already in the store - check both name and
        # playlist_id so renames don't cause duplicates.
        existing_names = PlaylistStore.get_existing_names(platform=integration.id)
        existing_ids = PlaylistStore.get_existing_ids_by_platform(integration.id)
        available = [
            p
            for p in playlists
            if p.get("title") not in existing_names
            and p.get("playlistId") not in existing_ids
        ]

        self._on_show_playlist_dialog(
            available,
            integration,
            on_select=self._on_playlist_selected,
            on_cancel=self._on_cancel,
        )

    def _on_playlist_selected(
        self,
        playlist_name: str,
        platform: str,
        playlist_id: str = "",
        thumb_url: Optional[str] = None,
        follower_count: int = 0,
    ) -> None:
        PlaylistStore.add_playlist(
            playlist_name,
            platform=platform,
            playlist_id=playlist_id,
            thumbnail_url=thumb_url or "",
        )
        if follower_count:
            PlaylistStore.update_metadata(
                playlist_name,
                platform,
                playlist_id,
                follower_count=follower_count,
            )
        self._workflow_open = False
        self._on_add_playlist_frame(playlist_name, platform, playlist_id, thumb_url)

    def _on_cancel(self) -> None:
        self._workflow_open = False
        self._on_dialog_cancel()

    def _cancel_and_show_error(self) -> None:
        self._workflow_open = False
        self._on_dialog_cancel()
        self._on_show_error()
