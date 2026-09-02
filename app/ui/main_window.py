"""Main application window.

Responsible for widget creation, layout, theme application, frame
management, and thin hook methods that delegate business logic to
controllers and services.
"""

import logging
import socket
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

# Platform ids are declared by the plugin manifests (integrations/*/
# plugin.json); these local constants mirror the built-in ids.
PLATFORM_YOUTUBE_MUSIC = "youtube_music"
PLATFORM_SPOTIFY = "spotify"
PLATFORM_SOUNDCLOUD = "soundcloud"
from controllers.keybind_registry import KeybindCallbacks
from controllers.playlist_controller import PlaylistController
from services import duplicate_queue
from services.database import DatabaseManager
from services.duplicate_check import find_duplicate_pairs
from services.playlist_store import PlaylistStore, playlist_still_registered
from services.playlist_sync import PlaylistSyncService
from services.song_manager import SongManager
from utils.icons import IconService
from utils.scaling import px, ui_font
from ui.activity_window import show_activity_window
from ui.card_grid import CardGridManager
from ui.search_manager import SearchManager
from ui.showcase_manager import ShowcaseManager
from ui.login_ui import show_login_dialog
from ui.playlist_dialog import PlaylistDialog
from ui.settings_ui import show_settings_dialog
from ui.tooltip import ToolTip
from utils.window import center_window, resize_window
from utils.config import get_setting, get_setting_value
from ui.scrollable import ScrollableFrame
from utils.theme import C, load_theme, btn_colors, hover_bg
from utils.platform import is_wayland_session, x11_root_desktop_state

logger = logging.getLogger(__name__)

INTEGRATION_ERROR_MSG = (
    "Add integrations following INTEGRATIONS.MD. "
    "Check your internet connection and check if the API is down."
)

# Lightweight TCP targets for service-health probes (host, port).
_PLATFORM_API_TARGETS: dict[str, tuple[str, int]] = {
    PLATFORM_YOUTUBE_MUSIC: ("music.youtube.com", 443),
    PLATFORM_SPOTIFY: ("api.spotify.com", 443),
    PLATFORM_SOUNDCLOUD: ("api.soundcloud.com", 443),
}

assets_dir = Path(__file__).resolve().parents[2] / "assets"
playlist_cover_img_path = assets_dir / "playlist_image.png"
close_playlist_img_path = assets_dir / "close_playlist.png"
reload_database_img_path = assets_dir / "reloadCache.png"
loading_img_path = assets_dir / "hourglass.png"
heart_empty_img_path = assets_dir / "heart_empty.png"
heart_full_img_path = assets_dir / "heart_full.png"


class MainWindow:
    def __init__(
        self,
        root,
        *,
        integrations,
        keybind_controller,
        app_controller,
        plugin_registry=None,
    ) -> None:
        self.root = root
        self.integrations = integrations
        self.kc = keybind_controller
        self.ac = app_controller
        # The live PluginRegistry (shared with App / KeybindController);
        # None in legacy/test call sites - flows keep working, only the
        # Manage dialog's integration-set display needs it.
        self.plugin_registry = plugin_registry

        self._recording_frame_idx: int | None = None

        self._auto_resize_enabled = self._read_auto_resize_setting()
        self._hide_to_tray = self._read_hide_to_tray_setting()
        self._showcase_count = self._read_showcase_count_setting()
        self._show_log = self._read_show_log_setting()
        self._show_stats = self._read_show_stats_setting()
        self._columns = self._read_columns_setting()
        self._song_manager = SongManager()

        # Status-banner state: active warnings keyed by source
        # ("connectivity" / "service:<platform>").
        self._warnings: dict[str, str] = {}
        self._connectivity_after_id: str | None = None
        self._service_after_id: str | None = None
        self._activity_poll_after_id: str | None = None

        # Set by App._start_tray() once the window exists; None when no
        # tray backend is available.
        self.tray_service = None
        # Pending hide-to-tray after() id (cancelled if the window is
        # mapped again before the WM settles the minimize).
        self._hide_after_id: str | None = None
        # Virtual desktop the window was last mapped on (X11
        # ``_NET_CURRENT_DESKTOP``); None until the first map or when the
        # property cannot be read.  Used to distinguish a desktop switch
        # (which the WM may carry out by minimizing the windows it
        # leaves behind) from a genuine minimize of this window.
        self._last_desktop: int | None = None

        self._sync_service = PlaylistSyncService(integrations)

        # ----- controller ----------------------------------------------
        self._playlist_controller = PlaylistController(
            self.root,
            integrations,
            on_show_platform_picker=self._show_platform_picker,
            on_show_playlist_dialog=self._show_playlist_dialog,
            on_add_playlist_frame=self._on_add_playlist_frame,
            on_dialog_cancel=self._on_dialog_cancel,
            on_show_error=self._show_integration_error,
            on_refresh=self.ac.refresh_auth,
        )

        # ----- theme & layout ------------------------------------------
        style = ttk.Style(self.root)
        style.theme_use("clam")

        self.root.title("PlaylistManager")
        self.root.configure(background=C["root_bg"])
        self.root.geometry(f"{px(650)}x{px(460)}")
        self.root.minsize(px(325), px(150))
        self.root.maxsize(999999, 999999)

        icon_path = assets_dir / "app_image.png"
        self.icon = IconService.get(icon_path, 32)
        self.root.iconphoto(False, self.icon)

        self.playlist_cover_img = IconService.get(playlist_cover_img_path, 64)
        self.close_playlist_img = IconService.get(close_playlist_img_path, 16)
        self.reload_database_img = IconService.get(reload_database_img_path, 16)
        self.loading_img = IconService.get(loading_img_path, 32)
        self.heart_empty_img = IconService.get(heart_empty_img_path, 16)
        self.heart_full_img = IconService.get(heart_full_img_path, 16)

        self.root.grid_rowconfigure(0, weight=0)
        self.root.grid_rowconfigure(1, weight=0)   # search bar
        self.root.grid_rowconfigure(2, weight=1)
        for c in range(self._columns):
            self.root.grid_columnconfigure(c, weight=1)

        header_bg = C["frame_head_bg"]
        self.header_frame = tk.Frame(self.root, background=header_bg, pady=5, padx=5)
        self.header_frame.bind("<B1-Motion>", self.on_drag)

        self._create_widgets()
        self.header_frame.bind("<Button-1>", self.start_drag)
        self.root.bind("<Button-1>", self._on_root_click, add="+")
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.bind("<Map>", self._on_map)
        self.root.protocol("WM_DELETE_WINDOW", self.ac.quit_app)

        # ----- cards area (scrollable) ----------------------------------
        self.main_area = tk.Frame(self.root, background=C["root_bg"])
        self.main_area.grid(
            row=2, column=0, columnspan=self._columns, sticky="nsew"
        )
        self.sf = ScrollableFrame(
            self.main_area, bg=C["scrollable_frame_bg"],
            show_scrollbar=False,
            bind_all_mousewheel=True,
        )
        self.sf.pack(side="left", fill="both", expand=True)
        self.sf.style_scrollbar(
            hover_bg(C["button_main_bg"]), C["scrollable_frame_bg"],
        )
        self.scrollbar = self.sf.scrollbar

        self.card_grid = CardGridManager(
            self.main_area,
            self.sf.content,
            self.sf,
            self.root,
            columns=self._columns,
            song_manager=self._song_manager,
            keybind_controller=self.kc,
            show_log=self._show_log,
            show_stats=self._show_stats,
            showcase_count=self._showcase_count,
            playlist_cover_img=self.playlist_cover_img,
            close_playlist_img=self.close_playlist_img,
            reload_database_img=self.reload_database_img,
            make_keybind_callbacks=self._make_keybind_callbacks,
            on_reload_requested=self._on_reload_requested,
            start_recording=self._start_recording,
            auto_resize=self._auto_resize,
            before_card_close=self._before_card_close,
            after_card_close=self._after_card_close,
            prune_frame_imgs=lambda c: self.showcase._prune_frame_imgs(c),
            get_search_results_height=lambda i: self.search.get_search_results_height(i),
            is_recording=self._is_recording,
            open_playlist_dialog=self._open_playlist_dialog,
        )

        self.search = SearchManager(
            self.root,
            self.card_grid,
            self._song_manager,
            columns=self._columns,
            close_img=self.close_playlist_img,
            show_main_content=self.card_grid._show_main_content,
            sync_empty_state=self.card_grid._sync_empty_state,
            update_card_height=self.card_grid._update_card_height,
            update_scrollregion=self.sf.update_scrollregion,
        )
        # Search keyboard shortcuts
        self.root.bind("<Control-f>", self.search.toggle_playlist_search)
        self.root.bind("<Control-F>", self.search.toggle_playlist_search)
        self.root.bind("<Control-Shift-f>", self.search.toggle_song_search)
        self.root.bind("<Control-Shift-F>", self.search.toggle_song_search)

        self.showcase = ShowcaseManager(
            self.root,
            self.card_grid,
            self._song_manager,
            close_playlist_img=self.close_playlist_img,
            heart_empty_img=self.heart_empty_img,
            heart_full_img=self.heart_full_img,
            integrations=self.integrations,
            card_index_fn=self._card_index,
            search_results=self.search._search_results,
            playlist_cover_img_path=playlist_cover_img_path,
        )

    def _card_index(self, card) -> int | None:
        try:
            return self.card_grid.cards.index(card)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # CardGridManager callbacks
    # ------------------------------------------------------------------

    def _before_card_close(self, index: int) -> None:
        """Called by CardGridManager before removing a card from the list."""
        if self._recording_frame_idx == index:
            self.kc.stop_recording()
            self._recording_frame_idx = None

    def _after_card_close(self, index: int) -> None:
        """Called by CardGridManager after a card is popped from the list."""
        self.search.on_card_closed(index)
        if (
            self._recording_frame_idx is not None
            and self._recording_frame_idx > index
        ):
            self._recording_frame_idx -= 1

    def _is_recording(self, frame_idx: int) -> bool:
        """Return True if keybind recording is active for *frame_idx*."""
        return self._recording_frame_idx == frame_idx

    # ------------------------------------------------------------------
    # Theme helpers
    # ------------------------------------------------------------------

    def apply_theme(self) -> None:
        load_theme()
        self.root.configure(background=C["root_bg"])

        self.main_area.configure(background=C["root_bg"])
        self.sf.canvas.configure(background=C["root_bg"])
        self.sf.content.configure(background=C["root_bg"])
        self.sf.style_scrollbar(hover_bg(C["button_main_bg"]), C["root_bg"])

        header_bg = C["frame_head_bg"]
        self.header_frame.configure(background=header_bg)
        self._header_right.configure(background=header_bg)

        def _style_header_button(widget) -> None:
            widget.configure(**btn_colors(C["button_head_bg"], C["button_head_fg"]))

        for widget in self.header_frame.winfo_children():
            if isinstance(widget, tk.Button):
                _style_header_button(widget)
            elif isinstance(widget, tk.Frame):
                # The right-side cluster (Activity + Settings buttons).
                for sub in widget.winfo_children():
                    if isinstance(sub, tk.Button):
                        _style_header_button(sub)

        # Re-style the status warning banner.
        warn_bg = C["label_playlist_warn_bg"]
        warn_fg = C["label_playlist_warn_fg"]
        self._warning_frame.configure(background=warn_bg)
        self._warning_label.configure(background=warn_bg, foreground=warn_fg)

        self.card_grid.apply_theme()
        self.search.apply_theme()

    # ------------------------------------------------------------------
    # Widget creation
    # ------------------------------------------------------------------

    def _create_widgets(self) -> None:
        btn_header_colors = btn_colors(C["button_head_bg"], C["button_head_fg"])

        login_img_path = assets_dir / "login.png"
        self.login_img = IconService.get(login_img_path, 32)
        self.btn_login = tk.Button(
            self.header_frame,
            image=self.login_img,
            cursor="hand2",
            **btn_header_colors,
            highlightthickness=0,
            relief="raised",
            command=lambda: show_login_dialog(
                self.root,
                on_success=self.ac.refresh_auth,
                integrations=list(self.integrations.get_all().values()),
                integration_registry=self.integrations,
                keybind_controller=self.kc,
                plugin_registry=self.plugin_registry,
                on_uninstall=self._on_uninstall_integration,
                on_plugins_changed=self._on_plugins_changed,
            ),
        )
        ToolTip(self.btn_login, "Log in to music services")

        add_playlist_img_path = assets_dir / "addPlaylist.png"
        self.add_playlist_img = IconService.get(add_playlist_img_path, 32)
        self.btn_add_playlist = tk.Button(
            self.header_frame,
            image=self.add_playlist_img,
            cursor="hand2",
            **btn_header_colors,
            highlightthickness=0,
            relief="raised",
            command=self._open_playlist_dialog,
        )
        ToolTip(self.btn_add_playlist, "Add a playlist")

        open_settings_img_path = assets_dir / "settings.png"
        self.open_settings_img = IconService.get(open_settings_img_path, 32)
        # Right-side header cluster: Activity badge button + Settings.
        self._header_right = tk.Frame(
            self.header_frame, background=C["frame_head_bg"]
        )
        self.btn_open_settings = tk.Button(
            self._header_right,
            image=self.open_settings_img,
            cursor="hand2",
            **btn_header_colors,
            highlightthickness=0,
            relief="raised",
            command=lambda: show_settings_dialog(
                self.root,
                keybind_controller=self.kc,
                on_theme_change=self.apply_theme,
                tray_available=self.tray_service,
                on_tray_toggle=self.set_hide_to_tray,
                on_auto_resize_toggle=self.set_auto_resize,
                on_showcase_count_change=self.set_showcase_count,
                on_showcase_log_change=self.set_showcase_log,
                on_playlist_stats_change=self.set_playlist_stats,
                on_columns_change=self.set_columns,
                on_check_updates_now=lambda on_done=None: self.ac.check_updates(force=True, on_done=on_done),
                on_check_duplicates_now=self._run_duplicate_scan,
                on_like_button_change=self._apply_like_button_visibility,
                on_scrobble_keybind_change=self._register_scrobble_keybind,
            ),
        )
        ToolTip(self.btn_open_settings, "Settings")

        self.btn_activity = tk.Button(
            self._header_right,
            text="Activity",
            cursor="hand2",
            **btn_header_colors,
            highlightthickness=0,
            relief="raised",
            font=ui_font(11),
            command=self._open_activity_window,
        )
        ToolTip(self.btn_activity, "Errors and duplicate songs")
        self.btn_activity.pack(side="left", padx=(0, 4))
        self.btn_open_settings.pack(side="left")

        # Span the whole card grid so the header background bar matches the
        # window width at any column count.
        self.header_frame.grid(row=0, column=0, columnspan=self._columns, sticky="nsew")
        # `uniform` forces the two side columns to EQUAL width even though
        # the right cluster (Activity + Settings) is naturally wider than
        # the lone login button - without it, "Add playlist" drifts off
        # center toward the narrow side.
        self.header_frame.grid_columnconfigure(0, weight=1, uniform="header_sides")
        self.header_frame.grid_columnconfigure(1, weight=0)
        self.header_frame.grid_columnconfigure(2, weight=1, uniform="header_sides")

        # --- status warning banner (row 0, hidden by default) -----------
        warn_bg = C["label_playlist_warn_bg"]
        warn_fg = C["label_playlist_warn_fg"]
        self._warning_frame = tk.Frame(
            self.header_frame, background=warn_bg, padx=6, pady=1,
        )
        self._warning_label = tk.Label(
            self._warning_frame,
            text="",
            font=ui_font(10),
            background=warn_bg,
            foreground=warn_fg,
            anchor="center",
        )
        self._warning_label.pack(fill="x")
        # Start hidden; _update_banner() grids/removes it as warnings arrive.
        self._warning_frame.grid_remove()

        self.btn_login.grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.btn_add_playlist.grid(row=1, column=1, padx=4, pady=4)
        self._header_right.grid(row=1, column=2, sticky="e", padx=4, pady=4)

    # ------------------------------------------------------------------
    # Playlist dialog workflow (delegates to PlaylistController)
    # ------------------------------------------------------------------

    def _open_playlist_dialog(self) -> None:
        self._playlist_controller.open_playlist_dialog()

    def _show_platform_picker(self, platforms, callback, on_cancel=None) -> None:
        """Create a Toplevel to pick a platform.

        ``on_cancel`` (the controller's cancel path) fires when the user
        dismisses the picker without choosing, so the controller can
        release its re-entrancy guard.
        """
        win_bg = C["frame_main_bg"]
        label_fg = C["label_def_fg"]
        btn_cols = btn_colors(C["button_main_bg"], C["button_main_fg"])
        cancel_cols = btn_colors(C["button_head_bg"], C["button_head_fg"])

        def _do_cancel() -> None:
            if on_cancel:
                on_cancel()
            win.destroy()

        win = tk.Toplevel(self.root)
        win.title("Choose Platform")
        win.configure(background=win_bg)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", _do_cancel)

        tk.Label(
            win,
            text="Select platform to fetch playlists from:",
            background=win_bg,
            foreground=label_fg,
            font=ui_font(11),
        ).pack(pady=10, padx=20)

        for integration in platforms:
            tk.Button(
                win,
                text=integration.display_name,
                **btn_cols,
                highlightthickness=0,
                relief="raised",
                font=ui_font(11),
                width=30,
                command=lambda i=integration: (win.destroy(), callback(i)),
            ).pack(pady=4, padx=20)

        tk.Button(
            win,
            text="Cancel",
            **cancel_cols,
            highlightthickness=0,
            relief="raised",
            font=ui_font(10),
            command=_do_cancel,
        ).pack(pady=10)

        # Center only after the widgets are packed - centering an empty
        # Toplevel (1x1) and letting it grow produces an off-center dialog.
        center_window(win)
        win.grab_set()

    def _show_playlist_dialog(self, playlists, integration, on_select, on_cancel) -> None:
        """Create the playlist selection dialog."""
        self.btn_add_playlist.configure(state="disabled", image=self.loading_img)
        if self.search.search_mode is not None:
            self.search.dismiss()
        self.card_grid._hide_main_content()

        dialog = PlaylistDialog(
            self.root,
            lambda name, pid, thumb_url, fc: on_select(
                name, integration.id, pid, thumb_url, fc
            ),
            on_cancel=on_cancel,
            columns=self._columns,
        )
        dialog.show(playlists)

    def _show_integration_error(self) -> None:
        messagebox.showerror("Integration Error", INTEGRATION_ERROR_MSG)

    def _on_uninstall_integration(self, platform_id: str) -> None:
        """Close every playlist card for *platform_id* (uninstall path).

        Invoked by the Manage dialog BEFORE its disk cleanup: home
        ``close_main_frame`` is the canonical per-playlist teardown
        (keybind unregister, registry entry, song DB, widget destroy +
        grid renumber), so cards of a removed platform never linger
        showing dead data once the plugin directory is gone.

        Best-effort per card: one card failing to close (e.g. a DROP
        fighting a locked DB) must not leave the rest of the platform's
        cards open - the disk cleanup that follows would then delete the
        db files out from under their widgets.
        """
        failure: Optional[str] = None
        for card in list(self.card_grid.cards):
            platform = (getattr(card, "platform", None) or "").strip()
            if platform and platform == platform_id:
                try:
                    self.card_grid.close_main_frame(card, delete_db=True)
                except Exception:
                    logger.exception(
                        "Failed to close card for '%s' during uninstall of %s",
                        getattr(card, "name_label", None) and card.name_label.cget("text"),
                        platform_id,
                    )
                    if failure is None:
                        failure = "one or more playlist cards could not be closed"
        if failure:
            user_log(
                logger,
                "Uninstalling %s: %s - their keybinds/databases may remain",
                platform_id, failure,
            )

    def _on_plugins_changed(self) -> None:
        """Re-scan integrations/ after a Manage dialog download/uninstall.

        Re-discovering the live registry lets a freshly downloaded plugin
        be used (login tiles, keybinds, URL parsing) without a restart;
        uninstalled plugins simply stop existing.  The plugin registry is
        shared with App and KeybindController, and
        ``App.reload_plugins`` re-registers new integrations into the
        shared IntegrationRegistry.
        """
        app = getattr(self.ac, "app", None)
        if app is not None and hasattr(app, "reload_plugins"):
            app.reload_plugins()

    def _on_dialog_cancel(self) -> None:
        """Restore UI after playlist dialog is cancelled."""
        self.btn_add_playlist.configure(state="normal", image=self.add_playlist_img)
        self.card_grid._show_main_content()

    def _on_add_playlist_frame(
        self, playlist_name: str, platform: str, playlist_id: str, thumb_url: str | None
    ) -> None:
        """Create a new frame for the selected playlist."""
        self.btn_add_playlist.configure(state="normal", image=self.add_playlist_img)
        self.card_grid._show_main_content()
        self.card_grid.create_main_frame(1)

        if self.card_grid.cards:
            card = self.card_grid.cards[-1]
            card.name_label.config(text=playlist_name)
            card.platform = platform
            card.playlist_id = playlist_id

            status_label = card.log_status
            status_label.config(text="Sync", background=C["label_playlist_warn_bg"])

            if thumb_url:
                self.showcase.set_playlist_cover(card.cover_label, thumb_url, card=card)

            frame_idx = len(self.card_grid.cards) - 1
            self.showcase.refresh_stats(frame_idx, playlist_name, platform)
            self._import_playlist_tracks(playlist_name, platform, playlist_id, frame_idx)

    # ------------------------------------------------------------------
    # Track import / reload (delegates to PlaylistSyncService)
    # ------------------------------------------------------------------

    def _import_playlist_tracks(
        self, playlist_name: str, platform: str, playlist_id: str, frame_idx: int
    ) -> None:
        """Start importing tracks in a background thread."""
        def on_done(name: str, count: int, status_text: str) -> None:
            try:
                self.root.after(
                    0, self._on_import_done, name, count, status_text, frame_idx
                )
            except Exception:
                # App quit while the import ran - the after() call comes
                # from the sync worker thread and fails against a
                # destroyed root.  The result matters only for the UI.
                logger.debug(
                    "App is shutting down; dropped import-done update",
                    exc_info=True,
                )

        self._sync_service.import_tracks(
            playlist_name, platform, playlist_id, on_done
        )

    def _find_frame_index_by_name(self, playlist_name: str) -> int | None:
        for i, card in enumerate(self.card_grid.cards):
            if card.name_label.cget("text") == playlist_name:
                return i
        return None

    def _on_import_done(
        self,
        playlist_name: str,
        count: int,
        status_text: str,
        frame_idx: int | None = None,
    ) -> int | None:
        """Handle a finished track import; returns the resolved frame index."""
        if frame_idx is not None:
            in_range = frame_idx < len(self.card_grid.cards)
            matches_name = (
                in_range
                and self.card_grid.cards[frame_idx].name_label.cget("text")
                == playlist_name
            )
            if not matches_name:
                frame_idx = self._find_frame_index_by_name(playlist_name)
        else:
            frame_idx = self._find_frame_index_by_name(playlist_name)
        if frame_idx is None or frame_idx >= len(self.card_grid.cards):
            return None
        card = self.card_grid.cards[frame_idx]
        card.syncing = False
        if count > 0:
            card.log_status.config(text="OK", background=C["label_playlist_good_bg"])
        elif status_text == "Error":
            card.log_status.config(text=status_text, background=C["label_playlist_error_bg"])
            # Persist for the activity window's Errors tab.
            try:
                duplicate_queue.record_error(
                    playlist_name, card.platform, "Track import failed"
                )
            except Exception:
                logger.debug("Could not log import error", exc_info=True)
        else:
            card.log_status.config(text=status_text, background=C["label_playlist_warn_bg"])
        self.showcase.update_log_labels_from_db(
            frame_idx, playlist_name, card.platform
        )
        self.showcase.refresh(
            frame_idx, playlist_name, card.platform
        )
        self.showcase.refresh_stats(
            frame_idx, playlist_name, card.platform
        )
        logger.info("Import finished for '%s': %s", playlist_name, status_text)
        return frame_idx

    def _on_reload_done(
        self,
        playlist_name: str,
        count: int,
        status_text: str,
        thumb_url: str | None,
        frame_idx: int | None = None,
    ) -> None:
        frame_idx = self._on_import_done(playlist_name, count, status_text, frame_idx)
        if frame_idx is not None:
            try:
                card = self.card_grid.cards[frame_idx]
            except IndexError:
                return
            try:
                card.reload_btn.configure(state="normal")
            except tk.TclError:
                pass
            if thumb_url:
                self.showcase.set_playlist_cover(card.cover_label, thumb_url, card=card)

    # ------------------------------------------------------------------
    # Keybind setup (called once after __init__)

    def _make_keybind_callbacks(self, frame_idx: int) -> KeybindCallbacks:
        """Build a :class:`KeybindCallbacks` bound to *frame_idx* widgets.

        All callbacks are scheduled on the main thread (tkinter must be
        accessed from the main thread).
        """
        card = self.card_grid.cards[frame_idx]
        # Capture the card, not the index: the callbacks object can
        # outlive close_main_frame() renumbering, so on_song_added resolves
        # the live index at callback time (memory: never capture an index).
        main_frame = card.frame

        def _set(widget, **kwargs) -> None:
            """Configure *widget* if it still exists.

            The flow runs on a worker thread and the frame can be closed
            while it is in flight - touching a destroyed widget raises
            TclError from the ``after`` callback.
            """
            try:
                if widget.winfo_exists():
                    widget.configure(**kwargs)
            except tk.TclError:
                pass

        def on_status(text: str, background: str) -> None:
            _set(card.log_status, text=text, background=background)

        def on_song_info(artist: str, name: str) -> None:
            _set(card.log_artist, text=artist)
            _set(card.log_name, text=name)

        def on_entry_state(state: str) -> None:
            _set(card.keybind_entry, state=state)

        def on_reset(entry_state: str) -> None:
            _set(card.keybind_entry, state=entry_state)
            _set(card.log_status, text="", background=C["frame_playlist_bg"])
            _set(card.log_artist, text="")
            _set(card.log_name, text="")

        def on_song_added() -> None:
            # Runs on the main thread (the flow schedules _apply there).
            # Resolve the card's live index - the card may have been
            # closed or renumbered since the callbacks were created.
            cur_idx = self._card_index(card)
            if cur_idx is None:
                return
            playlist_name = card.name_label.cget("text")
            self.showcase.refresh(
                cur_idx, playlist_name, card.platform
            )
            self.showcase.refresh_stats(
                cur_idx, playlist_name, card.platform
            )

        return KeybindCallbacks(
            on_status=on_status,
            on_song_info=on_song_info,
            on_entry_state=on_entry_state,
            on_reset=on_reset,
            on_song_added=on_song_added,
        )

    # ------------------------------------------------------------------
    # Activity window (errors + duplicate songs)
    # ------------------------------------------------------------------

    def _open_activity_window(self) -> None:
        """Open (or re-lift) the non-modal activity window."""
        show_activity_window(
            self.root,
            load_data=self._load_activity_data,
            on_song=self._handle_activity_song,
            on_close=self.refresh_activity_badge,
        )
        self.refresh_activity_badge()

    def _load_activity_data(self) -> dict:
        """Fresh data snapshot for the activity window (UI thread)."""
        return {
            "pending": duplicate_queue.list_pending(),
            "errors": duplicate_queue.list_errors(),
            "songs": duplicate_queue.list_songs(),
            "pairs": list(getattr(self, "_last_scan_pairs", []) or []),
        }

    def refresh_activity_badge(self) -> None:
        """Update the header button text: 'Activity' / 'Activity (N)'."""
        try:
            n = duplicate_queue.activity_count()
        except Exception:
            n = 0
        try:
            self.btn_activity.configure(
                text="Activity" if not n else f"Activity ({n})"
            )
        except tk.TclError:
            pass

    def _poll_activity_stamp(self) -> None:
        """2 s badge poll driven by extra.json's mtime.

        Catches mutations from every source - keybind flows, this UI, and
        even the CLI process writing the same file - without any pub-sub
        plumbing.  A stat() per tick is far cheaper than re-reading.
        """
        current = duplicate_queue.stamp()
        if current != getattr(self, "_activity_stamp", None):
            self._activity_stamp = current
            self.refresh_activity_badge()
        try:
            self._activity_poll_after_id = self.root.after(
                2000, self._poll_activity_stamp
            )
        except tk.TclError:
            pass  # app shutting down

    # -- song dispatch ------------------------------------------------

    def _handle_activity_song(self, record: dict, action: str) -> None:
        """Dispatcher passed to the activity window (runs on UI thread)."""
        kind = record.get("kind")
        if kind == "pair":
            pair_key = record["pair_key"]
            if action == "not_duplicate":
                duplicate_queue.set_song(pair_key, "not_duplicate")
            elif action == "remove_newer":
                self._remove_scanner_duplicate(record)
            elif action == "undo":
                duplicate_queue.delete_song(pair_key)
            return

        if kind == "clear_errors" and action == "clear":
            duplicate_queue.clear_errors()
            self.refresh_activity_badge()
            return

        # Pending add songs.
        if action == "dismiss":
            duplicate_queue.remove_pending(record["id"])
            duplicate_queue.set_song(record["pair_key"], "dismissed")
        elif action == "add":
            duplicate_queue.remove_pending(record["id"])
            alive = playlist_still_registered(
                record.get("playlist_name", ""),
                record.get("platform", ""),
                record.get("playlist_id") or None,
            )
            if not alive:
                duplicate_queue.record_error(
                    record.get("playlist_name", ""),
                    record.get("platform", ""),
                    "Playlist closed before the queued song was added",
                )
            else:
                duplicate_queue.set_song(record["pair_key"], "added")
                threading.Thread(
                    target=self._pending_add_worker,
                    args=(dict(record),),
                    daemon=True,
                ).start()
        else:
            logger.warning("Unknown activity action %r", action)
        self.refresh_activity_badge()

    def _pending_add_worker(self, rec: dict) -> None:
        """Daemon-thread half of the Add action.

        Re-runs the real flow with the queued record's pre-captured
        url/song_data; ``skip_duplicate_check`` prevents Add from
        re-triggering the very check that queued it.
        """
        platform = rec.get("platform", "")

        def _ui(fn) -> None:
            try:
                self.root.after(0, fn)
            except Exception:
                pass  # app shutting down (rule: guard every worker after())

        flow = self.kc.get_flow(platform)
        if flow is None:
            duplicate_queue.record_error(
                rec.get("playlist_name", ""),
                platform,
                f"{platform} unavailable - could not add '{rec.get('title', '')}'",
            )
            _ui(self.refresh_activity_badge)
            return

        song_data = {
            "title": rec.get("title", ""),
            "artists": rec.get("artists") or [],
            "duration": rec.get("duration"),
            "track_id": rec.get("track_id"),
            "thumbnail": rec.get("thumbnail_url"),
        }

        def on_status(msg: str) -> None:
            pass

        def on_error(msg: str) -> None:
            duplicate_queue.add_pending(rec)  # re-enqueue for another try
            duplicate_queue.record_error(rec.get("playlist_name", ""), platform, msg)
            _ui(self.refresh_activity_badge)

        def on_success(result: dict) -> None:
            # added / exists / duplicate all resolve the queue entry;
            # badge refresh keeps the count honest either way.
            _ui(self.refresh_activity_badge)

        try:
            flow.execute_flow(
                rec.get("playlist_name", ""),
                on_status,
                on_error,
                on_success,
                url=rec.get("url"),
                song_data=song_data,
                playlist_id=rec.get("playlist_id") or None,
                skip_duplicate_check=True,
            )
        except Exception as e:
            logger.error("Activity Add failed: %s", e, exc_info=True)
            duplicate_queue.add_pending(rec)
            duplicate_queue.record_error(
                rec.get("playlist_name", ""), platform, str(e)
            )
            _ui(self.refresh_activity_badge)

    def _remove_scanner_duplicate(self, pair_record: dict) -> None:
        """Remove-newer action: platform-first removal, then the local row.

        Mirrors the add-path ordering and showcase_manager's established
        removal chain (find_playlist -> authenticated integration ->
        remove_track -> delete_song): removing only locally would let the
        next reload resurrect the pair from platform truth.
        """
        newer = pair_record.get("newer") or {}
        name = pair_record.get("playlist_name", "")
        platform = pair_record.get("platform", "")
        pid = pair_record.get("playlist_id", "") or ""
        playlist_data = PlaylistStore.find_playlist(
            name, platform, playlist_id=pid or None
        )
        platform_playlist_id = (
            playlist_data.get("playlist_id", "") if playlist_data else ""
        )
        integration = (
            self.integrations.get(platform) if self.integrations else None
        )

        def work():
            ok = False
            reason = ""
            if not platform_playlist_id:
                reason = f"No platform playlist id for '{name}' - cannot remove"
            elif integration is None or not integration.is_authenticated():
                reason = f"{platform} not authenticated - cannot remove"
            else:
                ok = integration.remove_track(
                    platform_playlist_id, newer.get("track_id")
                )
                if not ok:
                    reason = "Platform refused the removal"

            def apply():
                if not ok:
                    duplicate_queue.record_error(name, platform, reason)
                    self.refresh_activity_badge()
                    return
                if SongManager().delete_song(
                    name, newer.get("id"), platform=platform, playlist_id=pid
                ):
                    duplicate_queue.set_song(
                        pair_record["pair_key"], "added"
                    )
                    idx = self._find_frame_index_by_name(name)
                    if idx is not None:
                        self.showcase.refresh(idx, name, platform)
                        self.showcase.refresh_stats(idx, name, platform)
                else:
                    # Platform side already removed; surface the mirror
                    # drift instead of pretending all went well.
                    duplicate_queue.record_error(
                        name,
                        platform,
                        "Removed from the platform but the local row "
                        "could not be deleted",
                    )
                self.refresh_activity_badge()

            try:
                self.root.after(0, apply)
            except Exception:
                pass  # app shutting down

        threading.Thread(target=work, daemon=True).start()

    # -- manual scan ("Check for duplicates now") --------------------------

    def _run_duplicate_scan(self, on_done) -> None:
        """Settings-button scan: fuzzy-pair every registered playlist.

        Runs off-thread; *on_done(found_count, error_or_None)* marshals
        back to the Settings dialog on the UI thread (same contract as
        update checks).
        """

        def _scan():
            found = 0
            error = None
            pairs = []
            try:
                song_manager = SongManager()
                for pl in PlaylistStore.load_playlists():
                    name = pl.get("name", "")
                    platform = pl.get("platform", "")
                    pid = pl.get("playlist_id") or ""
                    songs = song_manager.get_all_songs(
                        name, platform=platform, playlist_id=pid
                    )
                    for match in find_duplicate_pairs(songs):
                        key = duplicate_queue.make_pair_key(
                            platform,
                            pid,
                            match["older"].get("track_id"),
                            match["newer"].get("track_id"),
                        )
                        song = (
                            duplicate_queue.get_song(key) or {}
                        ).get("song")
                        if song in ("added", "not_duplicate"):
                            continue  # already resolved / whitelisted
                        found += 1
                        pairs.append(
                            {
                                "kind": "pair",
                                "pair_key": key,
                                "playlist_name": name,
                                "platform": platform,
                                "playlist_id": pid,
                                "newer": match["newer"],
                                "older": match["older"],
                                "similarity": match["similarity"],
                            }
                        )
            except Exception as e:
                logger.error("Duplicate scan failed: %s", e, exc_info=True)
                error = str(e)

            # Publish results on the UI thread; the window renders them.
            def _publish():
                self._last_scan_pairs = pairs
                if callable(on_done):
                    try:
                        on_done(found, error)
                    except tk.TclError:
                        pass
                if found and not error:
                    self._open_activity_window()

            try:
                self.root.after(0, _publish)
            except Exception:
                pass  # app shutting down

        threading.Thread(target=_scan, daemon=True).start()

    @staticmethod
    def _filter_available_playlists(playlists, available_platforms):
        """Pair each store entry with its platform, skipping dead ones.

        Returns ``[(playlist_dict, platform)]`` for every entry whose
        *platform* is in *available_platforms*.  A playlist whose plugin
        directory is absent (or whose optional dependency failed to
        import) has no working flow behind it - its card would offer
        keybinds and reloads that can only fail.  The entry itself stays
        untouched in db/playlists.json: restoring the integration brings
        the card back on the next launch.  Legacy entries without a
        *platform* field were all YouTube Music.
        """
        visible = []
        for playlist in playlists:
            platform = playlist.get("platform") or PLATFORM_YOUTUBE_MUSIC
            if platform in available_platforms:
                visible.append((playlist, platform))
            else:
                logger.info(
                    "Hiding playlist '%s' (no %s integration loaded)",
                    playlist.get("name"), platform,
                )
        return visible

    def setup(self) -> None:
        self.kc.set_root(self.root)
        visible = self._filter_available_playlists(
            PlaylistStore.load_playlists(),
            set(self.integrations.get_all()),
        )
        if visible:
            self.card_grid.create_main_frame(len(visible))
            for i, (playlist, platform) in enumerate(visible):
                if i < len(self.card_grid.cards):
                    card = self.card_grid.cards[i]
                    name = playlist.get("name", f"Playlist {i + 1}")
                    playlist_id = playlist.get("playlist_id", "")
                    card.name_label.config(text=name)
                    card.platform = platform
                    card.playlist_id = playlist_id

                    keybind = playlist.get("keybind", "")
                    if keybind:
                        entry = card.keybind_entry
                        entry.config(state="normal")
                        entry.insert(0, keybind)
                        entry.config(state="readonly")
                        displaced = self.kc.register_keybind(
                            name,
                            keybind,
                            self._make_keybind_callbacks(i),
                            platform=platform,
                            playlist_id=playlist_id,
                        )
                        if displaced:
                            self._clear_displaced_keybind(displaced)

                    self.showcase.update_log_labels_from_db(i, name, platform)
                    self.showcase.refresh(i, name, platform)
                    self.showcase.refresh_stats(i, name, platform)

                    thumb_url = playlist.get("thumbnail_url", "")
                    if thumb_url:
                        self.showcase.set_playlist_cover(card.cover_label, thumb_url, card=card)

        self.card_grid._sync_empty_state()

        # Register the scrobble keybind (standalone action, not tied to a playlist)
        self._register_scrobble_keybind()

        # Start periodic connectivity and service-health probes.
        self._start_background_checks()

    # ------------------------------------------------------------------
    # Drag-to-move window
    # ------------------------------------------------------------------

    def start_drag(self, event) -> None:
        self._drag_x = event.x
        self._drag_y = event.y

    def on_drag(self, event) -> None:
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # Hide to tray / restore from tray
    # ------------------------------------------------------------------

    def _on_unmap(self, event) -> None:
        """Hide the window to the tray when minimized (if enabled).

        ``<Unmap>`` fires for reasons other than minimize (our own
        ``withdraw()``, WM restarts), so the song is deferred and
        gated on ``state() == "iconic"`` - that distinguishes a real
        minimize from ``withdraw()`` (state ``withdrawn``), which
        prevents recursion.  The WM may take a few event-loop ticks to
        flip the state, so the gate is re-checked for a short while
        before giving up.

        Some Wayland compositors unmap the XWayland window on minimize
        without ever setting WM_STATE Iconic (plan.md W4), so once the
        retries run out, a window that is still unmapped (and not
        withdrawn) is treated as a minimize too - but only on Wayland
        sessions.  On X11 an unmap that never turns "iconic" is not a
        minimize (e.g. a slow WM still completing a restore), and hiding
        the window would yank it out from under the user, so X11 waits
        for the iconic state exclusively.
        """
        if event.widget is not self.root:
            return

        def _maybe_hide(attempts: int = 10) -> None:
            self._hide_after_id = None
            tray_ok = (
                self.tray_service is not None and self.tray_service.available
            )
            try:
                state = self.root.state()
                viewable = self.root.winfo_viewable()
            except tk.TclError:
                return
            if not tray_ok or not self._hide_to_tray:
                return
            if state == "withdrawn":
                # Our own withdraw() (or an external one) - not a minimize.
                return
            if state == "iconic" or (
                is_wayland_session() and attempts == 0 and not viewable
            ):
                if not self._is_user_minimize():
                    # The WM minimized this window while moving to
                    # another virtual desktop (common when a fullscreen
                    # game is open on the desktop being left, and in
                    # non-composited sessions) or entered "show the
                    # desktop" mode.  Hiding to tray would yank the
                    # window out of the taskbar prematurely - leave it
                    # minimized so the WM restores it like any other
                    # window.
                    return
                self.root.withdraw()
            elif attempts > 0:
                # WM hasn't settled the minimize yet - try again shortly.
                self._hide_after_id = self.root.after(
                    50, lambda: _maybe_hide(attempts - 1)
                )

        # Let the WM settle the state before deciding (X11 race).
        self._hide_after_id = self.root.after(0, _maybe_hide)

    def _on_map(self, event) -> None:
        """Cancel a pending hide-to-tray song once the window maps.

        If the user restores the window within the retry window, the
        pending ``after`` callback must not hide it again.
        """
        if event.widget is not self.root:
            return
        if self._hide_after_id is not None:
            try:
                self.root.after_cancel(self._hide_after_id)
            except tk.TclError:
                pass
            self._hide_after_id = None
        # Record which desktop this window came back on.  A later unmap
        # whose desktop differs from this one is a WM desktop switch
        # (non-composited WMs map/unmap the windows they leave behind,
        # and some minimize them when a fullscreen game is open on the
        # desktop), not a user minimize of this window.
        self._last_desktop, _ = x11_root_desktop_state()

    def _is_user_minimize(self) -> bool:
        """Whether the minimize was aimed at this window (not the WM
        switching desktops or entering show-desktop mode).

        Windows get minimized for reasons unrelated to the user's intent
        for *this* window: virtual desktop switches with a fullscreen
        game open on the desktop being left (non-composited WMs minimize
        the windows they abandon, KWin does this for game desktops), and
        "show the desktop" (minimize-all).  Hiding to tray in those
        cases would remove the app from the taskbar although the user
        never minimized it - it must stay minimized so the WM restores
        it when the desktop comes back.

        Returns True (hide to tray) when the minimize happened while the
        desktop stayed put and show-desktop mode is off; False (leave
        minimized) on a desktop switch or show-desktop mode.  When the
        X11 root properties cannot be read (non-X11 session, no
        ``xprop``), True is returned to preserve the original behavior.
        """
        desktop, showing = x11_root_desktop_state()
        if showing:
            return False
        if (
            desktop is not None
            and self._last_desktop is not None
            and desktop != self._last_desktop
        ):
            return False
        return True

    def set_hide_to_tray(self, enabled: bool) -> None:
        """Live-apply the hide-to-tray setting (called from Settings)."""
        self._hide_to_tray = enabled

    def set_auto_resize(self, enabled: bool) -> None:
        """Live-apply the auto-resize setting (called from Settings).

        The flag is read on every frame add/close, so without this
        callback a Settings toggle would only take effect after restart.
        """
        self._auto_resize_enabled = enabled
        if enabled:
            self._auto_resize()

    def show_from_tray(self) -> None:
        """Restore/raise the main window (tray "Open app" + default click).

        Best-effort under Wayland: compositors may refuse unsolicited
        raise/focus requests (focus-stealing prevention), leaving the
        window unfocused or below others (plan.md W3).  No app-side fix
        exists - XDG Activation is compositor-granted, and the
        ``-topmost`` hack is ignored by most compositors.
        """
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        if self.root.state() in ("iconic", "withdrawn"):
            self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # ------------------------------------------------------------------
    # Auto-resize
    # ------------------------------------------------------------------

    @staticmethod
    def _read_auto_resize_setting() -> bool:
        """Read the auto-resize setting once."""
        return get_setting("auto_resize", False)

    @staticmethod
    def _read_hide_to_tray_setting() -> bool:
        """Read the hide-to-tray setting once."""
        return get_setting("hide_to_tray", False)

    @staticmethod
    def _read_showcase_count_setting() -> int:
        """Read the showcase count once; clamp to [0, 10].

        0 (the default) turns the showcase off entirely.
        """
        try:
            raw = get_setting_value("showcase", "count", "0")
            return min(10, max(0, int(str(raw).strip())))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _read_show_log_setting() -> bool:
        """Read the show-log-row setting once."""
        return get_setting("showcase_log", True)

    @staticmethod
    def _read_show_stats_setting() -> bool:
        """Read the playlist-stats-row setting once."""
        return get_setting("playlist_stats", True)

    @staticmethod
    def _read_columns_setting() -> int:
        """Read the card-column count once; clamp to [1, 4]."""
        try:
            raw = get_setting_value("layout", "columns", "2")
            return min(4, max(1, int(str(raw).strip())))
        except (ValueError, TypeError):
            return 2

    def _fit_window(self) -> None:
        """Re-fit the window to the cards - only when auto-resize is on.

        With auto-resize off the window size is the user's to control:
        the scrollable cards area takes over when the content overflows,
        so no programmatic resize is ever applied (not even on explicit
        showcase/columns changes).
        """
        if not self._auto_resize_enabled:
            return
        try:
            resize_window(self.root)
        except Exception as e:
            logger.debug("Window fit failed: %s", e)

    def set_showcase_count(self, count: int) -> None:
        """Live-apply the showcase count (called from the Settings dialog).

        Rebuilds every card's showcase section in place - no window
        rebuild.  Falls back to a full rebuild when no frames exist yet.
        The window always re-fits afterwards, growing or shrinking (an
        explicit count decrease shrinks the window back to the cards).
        """
        try:
            self._showcase_count = min(10, max(0, int(count)))
        except (ValueError, TypeError):
            return
        self.card_grid._showcase_count = self._showcase_count
        for frame_idx in range(len(self.card_grid.cards)):
            try:
                playlist_name = self.card_grid.cards[frame_idx].name_label.cget("text")
                platform = self.card_grid.cards[frame_idx].platform
            except (IndexError, tk.TclError):
                continue
            self.showcase.refresh(frame_idx, playlist_name, platform)
            self.showcase.refresh_stats(frame_idx, playlist_name, platform)
        # The window re-fits only when auto-resize is on (see _fit_window);
        # otherwise the scrollable cards area handles the overflow.
        self._fit_window()

    def set_showcase_log(self, show: bool) -> None:
        """Live-apply the show-log-row setting (called from Settings)."""
        self._show_log = bool(show)
        self.card_grid._show_log = self._show_log
        for frame_idx in range(len(self.card_grid.cards)):
            self.showcase.apply_log_visibility(frame_idx)
        self._fit_window()

    def set_playlist_stats(self, show: bool) -> None:
        """Live-apply the show-playlist-stats setting (called from Settings)."""
        self._show_stats = bool(show)
        self.card_grid._show_stats = self._show_stats
        for frame_idx in range(len(self.card_grid.cards)):
            self.showcase.apply_stats_visibility(frame_idx)
            if self._show_stats:
                try:
                    pname = self.card_grid.cards[frame_idx].name_label.cget("text")
                    self.showcase.refresh_stats(
                        frame_idx, pname, self.card_grid.cards[frame_idx].platform
                    )
                except (IndexError, tk.TclError):
                    pass
        self._fit_window()

    def set_columns(self, count: int) -> None:
        """Live-apply the card column count (called from the Settings dialog).

        Re-lays out every existing card in place - no window rebuild.  The
        window re-fits only when auto-resize is on; otherwise the cards
        area keeps its size and scrolls when the new grid overflows.
        """
        try:
            self._columns = min(4, max(1, int(count)))
        except (ValueError, TypeError):
            return
        self.card_grid._columns = self._columns
        self.search.update_columns(self._columns)
        for c in range(self._columns):
            self.root.grid_columnconfigure(c, weight=1)
            self.card_grid.content_frame.grid_columnconfigure(c, weight=1)
        # Zero weight on columns above the new count so the grid doesn't
        # allocate dead space if the user manually resizes wider.
        for c in range(self._columns, 4):
            self.root.grid_columnconfigure(c, weight=0)
            self.card_grid.content_frame.grid_columnconfigure(c, weight=0)
        self.header_frame.grid_configure(columnspan=self._columns)
        self.main_area.grid_configure(columnspan=self._columns)
        self.card_grid._layout_frames()
        self.card_grid._sync_empty_state()
        self._fit_window()

    def _apply_like_button_visibility(self, enabled: bool) -> None:
        """Live-apply the like button visibility setting (called from Settings).

        Rebuilds every card's showcase section to show/hide the like button.
        """
        for frame_idx in range(len(self.card_grid.cards)):
            try:
                playlist_name = self.card_grid.cards[frame_idx].name_label.cget("text")
                platform = self.card_grid.cards[frame_idx].platform
            except (IndexError, tk.TclError):
                continue
            self.showcase.refresh(frame_idx, playlist_name, platform)
        self._fit_window()

    def _register_scrobble_keybind(self) -> None:
        """Register the scrobble keybind (standalone action) if one is set.

        Called once at setup and by the Settings dialog when the keybind
        changes (Record or Clear). Re-running it replaces the prior action
        binding: ``register`` unregisters the existing "scrobble" action
        first, so a cleared (empty) keybind removes the live binding and a
        changed combo replaces it.
        """
        keybind = get_setting_value("scrobble_keybind", "keybind") or ""

        def _on_scrobble_status(msg, bg):
            # For now, scrobble actions don't have a display widget like
            # playlists do; they just log silently. Future: could add a
            # small status label or activity indicator.
            logger.debug("Scrobble action: %s", msg)

        callbacks = KeybindCallbacks(
            on_status=_on_scrobble_status,
        )
        self.kc.registry.register(
            "scrobble",
            keybind,
            callbacks,
            platform="",  # action bindings have no platform
            playlist_id="",
            kind="action",  # distinguish from playlist bindings
        )

    def _auto_resize(self) -> None:
        """Resize the window to fit playlist frames."""
        if not self._auto_resize_enabled:
            return
        try:
            resize_window(self.root)
        except Exception as e:
            logger.debug("Auto-resize failed: %s", e)

    # ------------------------------------------------------------------
    # Keybind recording
    # ------------------------------------------------------------------

    def _clear_displaced_keybind(self, displaced: dict) -> None:
        """Clear a keybind that was just taken over by another playlist.

        Recording the same combo on playlist B silently displaces playlist
        A's binding (``KeybindRegistry.register`` returns the displaced
        info).  A's entry must stop showing the stolen combo and its
        persisted keybind must be cleared - otherwise the app would
        display a keybind that fires B, and a restart would resurrect the
        collision (leaving the combo bound to nothing once B's frame is
        closed).
        """
        name = displaced.get("playlist_name")
        platform = displaced.get("platform", PLATFORM_YOUTUBE_MUSIC)
        displaced_id = displaced.get("playlist_id", "") or ""
        if not name:
            return
        for i, card in enumerate(self.card_grid.cards):
            if (
                card.name_label.cget("text") == name
                and card.platform == platform
                and card.playlist_id == displaced_id
            ):
                entry = card.keybind_entry
                if entry is not None:
                    try:
                        entry.config(state="normal")
                        entry.delete(0, tk.END)
                        entry.config(state="readonly")
                    except tk.TclError:
                        pass
                break
        PlaylistStore.update_keybind(name, platform, "", playlist_id=displaced_id)

    def _start_recording(self, frame_idx: int | None) -> str:
        if frame_idx is None or frame_idx >= len(self.card_grid.cards):
            return "break"
        if self._recording_frame_idx is not None:
            self._stop_recording(self._recording_frame_idx)

        self._recording_frame_idx = frame_idx
        recording_card = self.card_grid.cards[frame_idx]
        entry = recording_card.keybind_entry
        entry.config(
            state="normal",
            readonlybackground=C["label_playlist_error_bg"],
            background=C["entry_playlist_bg"],
        )
        entry.delete(0, tk.END)

        def _live_index() -> int | None:
            # close_main_frame() renumbers self.card_grid.cards after deleting a
            # card; resolve the recording card's current index at
            # callback time so a mid-recording close can't commit against
            # the wrong playlist (or IndexError when out of range).
            try:
                return self.card_grid.cards.index(recording_card)
            except ValueError:
                return None

        def on_combo(combo: str) -> None:
            entry.config(state="normal")
            entry.delete(0, tk.END)
            entry.insert(0, combo)

        def on_stop() -> None:
            cur_idx = _live_index()
            if cur_idx is None:
                self._recording_frame_idx = None
                return
            # Only commit the empty keybind if this frame was still the
            # active recording target when the stop fired.
            was_recording_here = self._recording_frame_idx == cur_idx
            self._recording_frame_idx = None
            entry.config(
                state="readonly", readonlybackground=C["entry_playlist_ro_bg"]
            )
            entry.delete(0, tk.END)
            if not was_recording_here:
                return
            # Escape / focus-out during recording: commit the empty combo so
            # the previously registered keybind is removed and the store
            # matches what the entry now shows - a stale keybind firing with
            # a blank entry is confusing.
            playlist_name = self.card_grid.cards[cur_idx].name_label.cget("text")
            platform = self.card_grid.cards[cur_idx].platform
            playlist_id = recording_card.playlist_id
            PlaylistStore.update_keybind(
                playlist_name, platform, "", playlist_id=playlist_id
            )
            self.kc.unregister_keybind(
                playlist_name, platform=platform, playlist_id=playlist_id
            )

        self.kc.start_recording(on_combo, on_stop=on_stop)
        return "break"

    def _stop_recording(self, frame_idx: int) -> None:
        if self._recording_frame_idx != frame_idx:
            return
        self._recording_frame_idx = None
        combo = self.kc.stop_recording()

        card = self.card_grid.cards[frame_idx]
        entry = card.keybind_entry
        entry.config(
            state="readonly", readonlybackground=C["entry_playlist_ro_bg"]
        )
        entry.delete(0, tk.END)

        playlist_name = card.name_label.cget("text")
        platform = card.platform
        playlist_id = card.playlist_id

        if combo:
            entry.insert(0, combo)
            PlaylistStore.update_keybind(
                playlist_name, platform, combo, playlist_id=playlist_id
            )
            displaced = self.kc.register_keybind(
                playlist_name,
                combo,
                self._make_keybind_callbacks(frame_idx),
                platform=platform,
                playlist_id=playlist_id,
            )
            if displaced:
                self._clear_displaced_keybind(displaced)
        else:
            PlaylistStore.update_keybind(
                playlist_name, platform, "", playlist_id=playlist_id
            )
            self.kc.unregister_keybind(
                playlist_name, platform=platform, playlist_id=playlist_id
            )

    def _on_root_click(self, event) -> None:
        if self._recording_frame_idx is not None:
            entry = self.card_grid.cards[self._recording_frame_idx].keybind_entry
            if event.widget != entry:
                self.root.after(1, self._stop_recording, self._recording_frame_idx)

    # ------------------------------------------------------------------
    # Reload database (delegates to PlaylistSyncService)
    # ------------------------------------------------------------------

    def _on_reload_requested(self, frame_idx: int | None) -> None:
        """User clicked the reload button for a playlist frame."""
        if frame_idx is None or frame_idx >= len(self.card_grid.cards):
            return
        card = self.card_grid.cards[frame_idx]
        playlist_name = card.name_label.cget("text")
        platform = card.platform
        playlist_id = card.playlist_id
        playlist_data = PlaylistStore.find_playlist(
            playlist_name, platform, playlist_id=playlist_id
        )
        playlist_id = playlist_data.get("playlist_id", "") if playlist_data else ""

        if not playlist_id:
            logger.warning("No playlist_id for '%s', cannot reload", playlist_name)
            return

        # Prevent a remove click mid-reload from racing the re-import
        # (_on_remove_song checks this flag; the reload button is also
        # disabled below as a UX guard against double-clicks).
        card.syncing = True

        # Dismiss active song search since the DB is about to change.
        if self.search.search_mode == "song":
            self.search.dismiss()

        status_label = card.log_status
        status_label.config(text="Sync", background=C["label_playlist_warn_bg"])

        # Disable the reload button for this card during the sync to
        # prevent double-clicks; re-enabled in _on_reload_done.
        reload_btn = card.reload_btn
        if reload_btn is not None:
            try:
                reload_btn.configure(state="disabled")
            except tk.TclError:
                pass

        def on_done(
            name: str, count: int, status_text: str, thumb_url: str | None
        ) -> None:
            try:
                self.root.after(
                    0,
                    self._on_reload_done,
                    name,
                    count,
                    status_text,
                    thumb_url,
                    frame_idx,
                )
            except Exception:
                # App quit while the reload ran - after() comes from the
                # sync worker thread and fails against a destroyed root.
                logger.debug(
                    "App is shutting down; dropped reload-done update",
                    exc_info=True,
                )
        try:
            DatabaseManager.close_thread_connections()
        except Exception as e:
            logger.warning("Failed to close DB connections before reload: %s", e)

        self._sync_service.reload_database(
            playlist_name, platform, playlist_id, on_done
        )

    # ------------------------------------------------------------------
    # Status banner - connectivity & service-health warnings
    # ------------------------------------------------------------------

    def _set_warning(self, key: str, message: str | None) -> None:
        """Show or dismiss a single warning by *key*."""
        if message:
            self._warnings[key] = message
        else:
            self._warnings.pop(key, None)
        self._update_banner()

    def _update_banner(self) -> None:
        """Rebuild the banner text and show/hide it."""
        try:
            if self._warnings:
                text = " \u2022 ".join(self._warnings.values())
                self._warning_label.config(text=text)
                if not self._warning_frame.winfo_ismapped():
                    self._warning_frame.grid(
                        row=0, column=0, columnspan=3, sticky="ew",
                    )
            else:
                if self._warning_frame.winfo_ismapped():
                    self._warning_frame.grid_remove()
            # Trigger auto-resize so the window accommodates the banner.
            self.card_grid._auto_resize_cb()
        except tk.TclError:
            pass

    # --- connectivity (every 30 s) ----------------------------------

    def _start_background_checks(self) -> None:
        """Kick off periodic connectivity and service-health checks."""
        # First connectivity probe after 3 s; recurring every 30 s.
        try:
            self._connectivity_after_id = self.root.after(
                3000, self._run_connectivity_check,
            )
        except tk.TclError:
            pass
        # First service-health probe after 10 s; recurring every 5 min.
        try:
            self._service_after_id = self.root.after(
                10000, self._run_service_health_check,
            )
        except tk.TclError:
            pass
        # Activity badge poll (extra.json mtime, every 2 s).
        try:
            self._activity_poll_after_id = self.root.after(
                2000, self._poll_activity_stamp
            )
        except tk.TclError:
            pass

    def _run_connectivity_check(self) -> None:
        """Launch a connectivity probe in a daemon thread."""
        def _probe() -> None:
            try:
                ok = not not socket.create_connection(("1.1.1.1", 53), timeout=5)
            except (OSError, socket.timeout):
                ok = False
            try:
                self.root.after(0, self._on_connectivity_result, ok)
            except Exception:
                pass  # App shutting down.

        threading.Thread(target=_probe, daemon=True).start()

    def _on_connectivity_result(self, ok: bool) -> None:
        self._set_warning(
            "connectivity",
            None if ok else "No internet connection",
        )
        # Schedule the next probe.
        try:
            self._connectivity_after_id = self.root.after(
                30000, self._run_connectivity_check,
            )
        except tk.TclError:
            pass

    # --- service health (every 5 min) --------------------------------

    def _run_service_health_check(self) -> None:
        """Launch a per-platform service-health probe in a daemon thread."""
        # Only probe platforms the user actually has playlists for AND
        # whose integration is loaded - with no integration there is
        # nothing to reach and nothing to warn about.
        available = set(self.integrations.get_all())
        platforms = {
            p.get("platform") or PLATFORM_YOUTUBE_MUSIC
            for p in PlaylistStore.load_playlists()
        } & available
        if platforms:
            threading.Thread(
                target=self._service_health_probe,
                args=(platforms,),
                daemon=True,
            ).start()
        else:
            # No playlists yet - just reschedule.
            self._reschedule_service_check()

    def _service_health_probe(self, platforms: set[str]) -> None:
        """Probe each *platform*'s API endpoint (runs in a daemon thread)."""
        results: dict[str, bool] = {}
        for platform in platforms:
            target = _PLATFORM_API_TARGETS.get(platform)
            if target is None:
                continue
            host, port = target
            try:
                socket.create_connection((host, port), timeout=10)
                results[platform] = True
            except (OSError, socket.timeout):
                results[platform] = False
        try:
            self.root.after(0, self._on_service_health_result, results)
        except Exception:
            # App shut down while the probe was in flight.
            pass

    def _on_service_health_result(self, results: dict[str, bool]) -> None:
        for platform, ok in results.items():
            key = f"service:{platform}"
            if ok:
                self._set_warning(key, None)
            else:
                integration = self.integrations.get(platform)
                name = integration.display_name if integration else platform
                self._set_warning(key, f"{name} service is unreachable")
        self._reschedule_service_check()

    def _reschedule_service_check(self) -> None:
        try:
            self._service_after_id = self.root.after(
                300000, self._run_service_health_check,
            )
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        self.search.dismiss()
        self.showcase.frame_img_refs.clear()
        self.card_grid.cleanup()
        # Cancel pending connectivity / service-health / activity-poll timers.
        for aid in (
            self._connectivity_after_id,
            self._service_after_id,
            self._activity_poll_after_id,
        ):
            if aid is not None:
                try:
                    self.root.after_cancel(aid)
                except tk.TclError:
                    pass
        self._connectivity_after_id = None
        self._service_after_id = None
        self._activity_poll_after_id = None
        # Release cached per-thread SQLite connections held by the UI thread.
        try:
            DatabaseManager.close_thread_connections()
        except Exception as e:
            logger.warning("Error closing database connections: %s", e)
