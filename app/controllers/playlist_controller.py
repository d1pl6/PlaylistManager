"""
Playlist dialog workflow controller.

Extracted from ``app/ui/main_window.py`` (Issue #1).  Orchestrates the
"add playlist" flow (platform choice, playlist list, selection) without
directly creating tkinter widgets.  UI actions are delegated to callbacks
provided by the window layer.
"""

import logging
from typing import Callable, Optional

from constants import PLATFORM_YOUTUBE_MUSIC
from services.playlist_store import PlaylistStore

logger = logging.getLogger(__name__)


class PlaylistController:
    """Handles the business logic of the "add playlist" workflow.

    Callbacks received from the UI layer:

    * ``on_show_platform_picker(platforms)`` - user must pick a platform
    * ``on_show_playlist_dialog(playlists, integration)`` - show the
      playlist selection dialog
    * ``on_add_playlist_frame(name, platform, playlist_id, thumb_url)`` -
      a playlist was selected and persisted; the UI should add a frame
    * ``on_dialog_cancel()`` - restore UI after cancellation
    * ``on_show_error()`` - show the generic integration-error dialog
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
    ) -> None:
        self.parent = parent
        self.integrations = integrations

        # UI callbacks
        self._on_show_platform_picker = on_show_platform_picker
        self._on_show_playlist_dialog = on_show_playlist_dialog
        self._on_add_playlist_frame = on_add_playlist_frame
        self._on_dialog_cancel = on_dialog_cancel
        self._on_show_error = on_show_error

        # State
        self._choose_open = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def open_playlist_dialog(self) -> None:
        """Start the "add playlist" workflow.

        Called when the user clicks the + button.
        """
        if self._choose_open:
            return

        active = self.integrations.get_active()
        if not active:
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
        """Show the platform picker when multiple integrations are active."""
        platforms = list(active_integrations.values())

        def on_pick(integration):
            self._fetch_and_show_playlists(integration)

        self._on_show_platform_picker(platforms, callback=on_pick)

    def _fetch_and_show_playlists(self, integration) -> None:
        """Fetch playlists from *integration* and show them to the user."""
        self._choose_open = True

        try:
            playlists = integration.get_library_playlists()
        except Exception as e:
            logger.error("Failed to fetch playlists: %s", e)
            self._cancel_and_show_error()
            return

        if not playlists:
            self._cancel_and_show_error()
            return

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
        platform: str = PLATFORM_YOUTUBE_MUSIC,
        playlist_id: str = "",
        thumb_url: Optional[str] = None,
    ) -> None:
        """A playlist was chosen - persist and notify the UI."""
        PlaylistStore.add_playlist(
            playlist_name,
            platform=platform,
            playlist_id=playlist_id,
            thumbnail_url=thumb_url or "",
        )
        self._choose_open = False
        self._on_add_playlist_frame(playlist_name, platform, playlist_id, thumb_url)

    def _on_cancel(self) -> None:
        """User cancelled the dialog."""
        self._choose_open = False
        self._on_dialog_cancel()

    def _cancel_and_show_error(self) -> None:
        self._choose_open = False
        self._on_dialog_cancel()
        self._on_show_error()
