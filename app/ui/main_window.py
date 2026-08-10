"""Main application window.

Responsible for widget creation, layout, theme application, frame
management, and thin hook methods that delegate business logic to
controllers and services.
"""

import logging
import threading
import tkinter as tk
from configparser import ConfigParser
from pathlib import Path
from tkinter import ttk, messagebox

from constants import PLATFORM_YOUTUBE_MUSIC
from controllers.keybind_registry import KeybindCallbacks
from controllers.playlist_controller import PlaylistController
from services.database import DatabaseManager
from services.playlist_store import PlaylistStore
from services.playlist_sync import PlaylistSyncService
from services.song_manager import SongManager
from utils.thumbnail import ThumbnailService
from ui.login_ui import show_login_dialog
from ui.playlist_dialog import PlaylistDialog
from ui.settings_ui import show_settings_dialog
from utils.window import center_window, resize_window
from utils.config import (
    ensure_settings_file,
    SETTINGS_PATH as _settings_path,
)
from utils.theme import C, load_theme
from utils.platform import is_wayland_session

logger = logging.getLogger(__name__)

INTEGRATION_ERROR_MSG = (
    "Add integrations following INTEGRATIONS.MD. "
    "Check your internet connection and check if the API is down."
)

assets_dir = Path(__file__).resolve().parents[2] / "assets"
playlist_cover_img_path = assets_dir / "playlist_image.png"
close_playlist_img_path = assets_dir / "close_playlist.png"
reload_database_img_path = assets_dir / "reloadCache.png"
loading_img_path = assets_dir / "hourglass.png"


class MainWindow:
    def __init__(
        self,
        root,
        *,
        integrations,
        keybind_controller,
        app_controller,
    ) -> None:
        self.root = root
        self.integrations = integrations
        self.kc = keybind_controller
        self.ac = app_controller

        self.frames: list[tk.Frame] = []
        self.frame_positions: list[tuple[int, int]] = []
        self.playlist_name_labels: list[tk.Label] = []
        self.frame_platforms: list[str] = []
        self.active_log_labels: dict[int, dict] = {}
        self.img_refs: list = []
        self.frame_img_refs: dict = {}
        self._choose_open = False
        self._recording_frame_idx: int | None = None

        self._auto_resize_enabled = self._read_auto_resize_setting()
        self._hide_to_tray = self._read_hide_to_tray_setting()

        # Set by App._start_tray() once the window exists; None when no
        # tray backend is available.
        self.tray_service = None
        # Pending hide-to-tray after() id (cancelled if the window is
        # mapped again before the WM settles the minimize).
        self._hide_after_id: str | None = None

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
        )

        # ----- theme & layout ------------------------------------------
        style = ttk.Style(self.root)
        style.theme_use("clam")

        self.root.title("PlaylistManager")
        self.root.configure(background=C["root_bg"])
        self.root.geometry("650x460")
        self.root.minsize(325, 150)
        self.root.maxsize(999999, 999999)

        icon_path = assets_dir / "app_image.png"
        self.icon = tk.PhotoImage(file=str(icon_path))
        self.root.iconphoto(False, self.icon)

        self.playlist_cover_img = tk.PhotoImage(file=str(playlist_cover_img_path))
        self.close_playlist_img = tk.PhotoImage(file=str(close_playlist_img_path))
        self.reload_database_img = tk.PhotoImage(file=str(reload_database_img_path))
        self.loading_img = tk.PhotoImage(file=str(loading_img_path))

        self.root.grid_rowconfigure(0, weight=0)
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

        header_bg = C["frame_head_bg"]
        self.header_frame = tk.Frame(self.root, background=header_bg, pady=5, padx=5)
        self.header_frame.bind("<B1-Motion>", self.on_drag)

        self._create_widgets()
        self.header_frame.bind("<Button-1>", self.start_drag)
        self.root.bind("<Button-1>", self._on_root_click, add="+")
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.bind("<Map>", self._on_map)
        self.root.protocol("WM_DELETE_WINDOW", self.ac.quit_app)

    # ------------------------------------------------------------------
    # Theme helpers
    # ------------------------------------------------------------------

    def apply_theme(self) -> None:
        load_theme()
        header_bg = C["frame_head_bg"]
        self.header_frame.configure(background=header_bg)
        for widget in self.header_frame.winfo_children():
            if isinstance(widget, tk.Button):
                widget.configure(
                    background=C["button_head_bg"],
                    activebackground=C["button_head_a_bg"],
                )

        frame_playlist_bg = C["frame_playlist_bg"]
        for frame in self.frames:
            frame.configure(background=frame_playlist_bg)
            for child in frame.winfo_children():
                try:
                    child.configure(background=frame_playlist_bg)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Widget creation
    # ------------------------------------------------------------------

    def _create_widgets(self) -> None:
        btn_header_bg = C["button_head_bg"]
        btn_header_abg = C["button_head_a_bg"]

        login_img_path = assets_dir / "login.png"
        self.login_img = tk.PhotoImage(file=str(login_img_path))
        self.btn_login = tk.Button(
            self.header_frame,
            image=self.login_img,
            cursor="hand2",
            background=btn_header_bg,
            activebackground=btn_header_abg,
            command=lambda: show_login_dialog(
                self.root, on_success=self.ac.refresh_auth
            ),
        )

        add_playlist_img_path = assets_dir / "addPlaylist.png"
        self.add_playlist_img = tk.PhotoImage(file=str(add_playlist_img_path))
        self.btn_add_playlist = tk.Button(
            self.header_frame,
            image=self.add_playlist_img,
            cursor="hand2",
            background=btn_header_bg,
            activebackground=btn_header_abg,
            command=self._open_playlist_dialog,
        )

        open_settings_img_path = assets_dir / "settings.png"
        self.open_settings_img = tk.PhotoImage(file=str(open_settings_img_path))
        self.btn_open_settings = tk.Button(
            self.header_frame,
            image=self.open_settings_img,
            cursor="hand2",
            background=btn_header_bg,
            activebackground=btn_header_abg,
            command=lambda: show_settings_dialog(
                self.root,
                keybind_controller=self.kc,
                on_theme_change=self.apply_theme,
                tray_available=self.tray_service,
                on_tray_toggle=self.set_hide_to_tray,
            ),
        )

        self.header_frame.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.header_frame.grid_columnconfigure(0, weight=1)
        self.header_frame.grid_columnconfigure(1, weight=0)
        self.header_frame.grid_columnconfigure(2, weight=1)

        self.btn_login.grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.btn_add_playlist.grid(row=0, column=1, padx=4, pady=4)
        self.btn_open_settings.grid(row=0, column=3, sticky="e", padx=4, pady=4)

    # ------------------------------------------------------------------
    # Playlist dialog workflow (delegates to PlaylistController)
    # ------------------------------------------------------------------

    def _open_playlist_dialog(self) -> None:
        self._playlist_controller.open_playlist_dialog()

    def _show_platform_picker(self, platforms, callback) -> None:
        """Create a Toplevel to pick a platform."""
        win_bg = C["frame_main_bg"]
        label_fg = C["label_def_fg"]
        btn_bg = C["button_main_bg"]
        btn_fg = C["button_main_fg"]
        btn_a_bg = C["button_main_a_bg"]
        btn_a_fg = C["button_main_a_fg"]
        cancel_bg = C["button_head_bg"]
        cancel_fg = C["button_head_fg"]
        cancel_a_bg = C["button_head_a_bg"]

        win = tk.Toplevel(self.root)
        win.title("Choose Platform")
        win.configure(background=win_bg)
        win.transient(self.root)
        center_window(win)
        win.grab_set()

        tk.Label(
            win,
            text="Select platform to fetch playlists from:",
            background=win_bg,
            foreground=label_fg,
            font="Noto, 11",
        ).pack(pady=10, padx=20)

        for integration in platforms:
            tk.Button(
                win,
                text=integration.display_name,
                background=btn_bg,
                foreground=btn_fg,
                activebackground=btn_a_bg,
                activeforeground=btn_a_fg,
                font="Noto, 11",
                width=30,
                command=lambda i=integration: (win.destroy(), callback(i)),
            ).pack(pady=4, padx=20)

        tk.Button(
            win,
            text="Cancel",
            background=cancel_bg,
            foreground=cancel_fg,
            activebackground=cancel_a_bg,
            font="Noto, 10",
            command=win.destroy,
        ).pack(pady=10)

    def _show_playlist_dialog(self, playlists, integration, on_select, on_cancel) -> None:
        """Create the playlist selection dialog."""
        self._choose_open = True
        self.btn_add_playlist.configure(state="disabled", image=self.loading_img)
        self._hide_main_content()

        dialog = PlaylistDialog(
            self.root,
            lambda name, pid, thumb_url: on_select(
                name, integration.id, pid, thumb_url
            ),
            on_cancel=on_cancel,
        )
        dialog.show(playlists)

    def _show_integration_error(self) -> None:
        messagebox.showerror("Integration Error", INTEGRATION_ERROR_MSG)

    def _on_dialog_cancel(self) -> None:
        """Restore UI after playlist dialog is cancelled."""
        self._choose_open = False
        self.btn_add_playlist.configure(state="normal", image=self.add_playlist_img)
        self._show_main_content()

    def _on_add_playlist_frame(
        self, playlist_name: str, platform: str, playlist_id: str, thumb_url: str | None
    ) -> None:
        """Create a new frame for the selected playlist."""
        self.btn_add_playlist.configure(state="normal", image=self.add_playlist_img)
        self._show_main_content()
        self.create_main_frame(1)

        if self.playlist_name_labels:
            self.playlist_name_labels[-1].config(text=playlist_name)
            self.frame_platforms[-1] = platform

            frame_idx = len(self.frames) - 1
            status_label = self.active_log_labels[frame_idx]["status"]
            status_label.config(text="Sync", background=C["label_playlist_warn_bg"])

            if thumb_url:
                self._set_playlist_cover(frame_idx, thumb_url)

            self._import_playlist_tracks(playlist_name, platform, playlist_id, frame_idx)

    # ------------------------------------------------------------------
    # Thumbnail management
    # ------------------------------------------------------------------

    def _set_playlist_cover(self, frame_idx: int, thumb_url: str) -> None:
        """Download a playlist thumbnail in a background thread.

        Only the download + resize run off-thread (thread-safe); the
        PhotoImage is created on the main thread by :meth:`_apply_cover`
        because tkinter is not thread-safe.
        """
        if frame_idx not in self.active_log_labels:
            return
        cover_label = self.active_log_labels[frame_idx].get("cover")
        if not cover_label:
            return

        def fetch() -> None:
            img = ThumbnailService.fetch_image(thumb_url, size=(64, 64))
            if img is not None:
                self.root.after(0, lambda: self._apply_cover(frame_idx, img))

        threading.Thread(target=fetch, daemon=True).start()

    def _apply_cover(self, frame_idx: int, img) -> None:
        if frame_idx not in self.active_log_labels:
            return
        cover_label = self.active_log_labels[frame_idx].get("cover")
        if not cover_label:
            return
        try:
            tk_img = ThumbnailService.to_photoimage(img)
        except Exception as e:
            logger.error(f"Failed to create cover PhotoImage: {e}")
            return
        cover_label.configure(image=tk_img)
        # Keep a reference so the PhotoImage isn't garbage-collected.
        # Keyed by the widget itself (not id()) so the close path can
        # release the refs reliably - id() values are reused after widget
        # destruction and the old close-path check never matched.
        self.frame_img_refs.setdefault(cover_label, []).append(tk_img)

    # ------------------------------------------------------------------
    # Database / log label helpers
    # ------------------------------------------------------------------

    def _update_log_labels_from_db(
        self, frame_idx: int, playlist_name: str, platform: str
    ) -> None:
        """Refresh the artist / song-name labels from the playlist DB.

        Reads the most recently added song so the frame shows real data
        as soon as an import or reload has populated the database.
        """
        sm = SongManager()
        latest = sm.get_latest_song(playlist_name, platform=platform)
        if not latest:
            return
        labels = self.active_log_labels.get(frame_idx)
        if not labels:
            return
        artists = latest.get("artists", [])
        artists_str = ", ".join(artists[:2]) if isinstance(artists, list) else str(artists)
        labels["artist"].config(text=artists_str[:8])
        labels["name"].config(text=latest.get("title", "")[:18])

    # ------------------------------------------------------------------
    # Track import / reload (delegates to PlaylistSyncService)
    # ------------------------------------------------------------------

    def _import_playlist_tracks(
        self, playlist_name: str, platform: str, playlist_id: str, frame_idx: int
    ) -> None:
        """Start importing tracks in a background thread."""
        def on_done(name: str, count: int, status_text: str) -> None:
            self.root.after(
                0, self._on_import_done, name, count, status_text, frame_idx
            )

        self._sync_service.import_tracks(
            playlist_name, platform, playlist_id, on_done
        )

    def _find_frame_index_by_name(self, playlist_name: str) -> int | None:
        for i, label in enumerate(self.playlist_name_labels):
            if label.cget("text") == playlist_name:
                return i
        return None

    def _on_import_done(
        self,
        playlist_name: str,
        count: int,
        status_text: str,
        frame_idx: int | None = None,
    ) -> None:
        if frame_idx is None:
            frame_idx = self._find_frame_index_by_name(playlist_name)
        if frame_idx is None or frame_idx not in self.active_log_labels:
            return
        status_label = self.active_log_labels[frame_idx]["status"]
        if count > 0:
            status_label.config(text="OK", background=C["label_playlist_good_bg"])
        elif status_text == "Error":
            status_label.config(text=status_text, background=C["label_playlist_error_bg"])
        else:
            # Nothing imported - "No tracks" (empty playlist or a swallowed
            # platform error) and "0 new" (all duplicates) are not successes.
            status_label.config(text=status_text, background=C["label_playlist_warn_bg"])
        self._update_log_labels_from_db(
            frame_idx, playlist_name, self.frame_platforms[frame_idx]
        )
        logger.info("Import finished for '%s': %s", playlist_name, status_text)

    def _on_reload_done(
        self,
        playlist_name: str,
        count: int,
        status_text: str,
        thumb_url: str | None,
        frame_idx: int | None = None,
    ) -> None:
        self._on_import_done(playlist_name, count, status_text, frame_idx)
        if frame_idx is None:
            frame_idx = self._find_frame_index_by_name(playlist_name)
        if frame_idx is not None and thumb_url:
            self._set_playlist_cover(frame_idx, thumb_url)

    # ------------------------------------------------------------------
    # Keybind setup (called once after __init__)
    # ------------------------------------------------------------------

    def _make_keybind_callbacks(self, frame_idx: int) -> KeybindCallbacks:
        """Build a :class:`KeybindCallbacks` bound to *frame_idx* widgets.

        All callbacks are scheduled on the main thread (tkinter must be
        accessed from the main thread).
        """
        labels = self.active_log_labels[frame_idx]

        def on_status(text: str, background: str) -> None:
            labels["status"].config(text=text, background=background)

        def on_song_info(artist: str, name: str) -> None:
            labels["artist"].config(text=artist)
            labels["name"].config(text=name)

        def on_entry_state(state: str) -> None:
            labels["keybind_entry"].config(state=state)

        def on_reset(entry_state: str) -> None:
            labels["keybind_entry"].config(state=entry_state)
            labels["status"].config(text="", background=C["frame_playlist_bg"])
            labels["artist"].config(text="")
            labels["name"].config(text="")

        return KeybindCallbacks(
            on_status=on_status,
            on_song_info=on_song_info,
            on_entry_state=on_entry_state,
            on_reset=on_reset,
        )

    def setup(self) -> None:
        self.kc.set_root(self.root)
        playlists = PlaylistStore.load_playlists()
        if playlists:
            self.create_main_frame(len(playlists))
            for i, playlist in enumerate(playlists):
                if i < len(self.playlist_name_labels):
                    name = playlist.get("name", f"Playlist {i + 1}")
                    platform = playlist.get("platform", PLATFORM_YOUTUBE_MUSIC)
                    self.playlist_name_labels[i].config(text=name)
                    self.frame_platforms[i] = platform

                    hotkey = playlist.get("hotkey", "")
                    if hotkey:
                        entry = self.active_log_labels[i]["keybind_entry"]
                        entry.config(state="normal")
                        entry.insert(0, hotkey)
                        entry.config(state="readonly")
                        self.kc.register_hotkey(
                            name,
                            hotkey,
                            self._make_keybind_callbacks(i),
                            platform=platform,
                        )

                    self._update_log_labels_from_db(i, name, platform)

                    thumb_url = playlist.get("thumbnail_url", "")
                    if thumb_url:
                        self._set_playlist_cover(i, thumb_url)

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
        ``withdraw()``, WM restarts), so the decision is deferred and
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
                self.root.withdraw()
            elif attempts > 0:
                # WM hasn't settled the minimize yet - try again shortly.
                self._hide_after_id = self.root.after(
                    50, lambda: _maybe_hide(attempts - 1)
                )

        # Let the WM settle the state before deciding (X11 race).
        self._hide_after_id = self.root.after(0, _maybe_hide)

    def _on_map(self, event) -> None:
        """Cancel a pending hide-to-tray decision once the window maps.

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

    def set_hide_to_tray(self, enabled: bool) -> None:
        """Live-apply the hide-to-tray setting (called from Settings)."""
        self._hide_to_tray = enabled

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
    # Frame creation / layout
    # ------------------------------------------------------------------

    def create_main_frame(self, num: int) -> None:
        start_index = len(self.frames)
        for i in range(start_index, start_index + num):
            col = i % 2
            row = (i // 2) + 1

            main_bg = C["frame_main_bg"]
            frame_playlist_bg = C["frame_playlist_bg"]
            label_playlist_bg = C["label_playlist_bg"]
            label_playlist_fg = C["label_playlist_fg"]
            label_playlist_name_bg = C["label_playlist_name_bg"]
            label_playlist_name_fg = C["label_playlist_name_fg"]
            label_playlist_log_bg = C["label_playlist_log_bg"]
            label_playlist_log_fg = C["label_playlist_log_fg"]
            label_playlist_good_bg = C["label_playlist_good_bg"]
            label_playlist_good_fg = C["label_playlist_good_fg"]
            label_playlist_warn_bg = C["label_playlist_warn_bg"]
            label_playlist_warn_fg = C["label_playlist_warn_fg"]
            label_playlist_error_bg = C["label_playlist_error_bg"]
            label_playlist_error_fg = C["label_playlist_error_fg"]
            button_playlist_bg = C["button_playlist_bg"]
            button_playlist_fg = C["button_playlist_fg"]
            button_playlist_a_bg = C["button_playlist_a_bg"]
            button_playlist_a_fg = C["button_playlist_a_fg"]
            entry_playlist_bg = C["entry_playlist_bg"]
            entry_playlist_fg = C["entry_playlist_fg"]
            entry_playlist_ro_bg = C["entry_playlist_ro_bg"]
            btn_close_bg = C["button_close_bg"]
            btn_close_abg = C["button_close_a_bg"]
            btn_close_fg = C["button_close_fg"]
            btn_close_a_fg = C["button_close_a_fg"]

            main_frame = tk.Frame(self.root, width=320, background=frame_playlist_bg)
            main_header_frame = tk.Frame(main_frame, background=frame_playlist_bg)
            main_log_frame = tk.Frame(main_frame, background=frame_playlist_bg)

            playlist_cover = tk.Label(
                main_header_frame,
                image=self.playlist_cover_img,
                background=label_playlist_bg,
            )
            playlist_name = tk.Label(
                main_header_frame,
                text=f"row:{row} col:{col}",
                font="Noto, 12",
                background=label_playlist_name_bg,
                foreground=label_playlist_name_fg,
                width=25,
            )

            close_playlist = tk.Button(
                main_header_frame,
                image=self.close_playlist_img,
                cursor="hand2",
                background=button_playlist_bg,
                foreground=button_playlist_fg,
                activebackground=button_playlist_a_bg,
                activeforeground=button_playlist_a_fg,
                command=lambda f=main_frame: self.close_main_frame(f),
            )

            playlist_keybind = tk.Entry(
                main_header_frame,
                font="Noto, 12",
                justify="center",
                background=entry_playlist_bg,
                foreground=entry_playlist_fg,
                readonlybackground=entry_playlist_ro_bg,
                state="readonly",
            )
            playlist_keybind.bind(
                "<Button-1>",
                lambda e, frame_idx=len(self.frames): self._start_recording(frame_idx),
            )

            reload_database = tk.Button(
                main_header_frame,
                image=self.reload_database_img,
                cursor="hand2",
                background=button_playlist_bg,
                foreground=button_playlist_fg,
                activebackground=button_playlist_a_bg,
                activeforeground=button_playlist_a_fg,
                command=lambda idx=len(self.frames): self._on_reload_requested(idx),
            )

            log_artist = tk.Label(
                main_log_frame,
                text="log_artist placeholder",
                font="Noto, 12",
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                width=8,
                anchor="w",
            )
            log_helper_1 = tk.Label(
                main_log_frame,
                text="-",
                font="Noto, 12",
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                anchor="w",
            )
            log_name = tk.Label(
                main_log_frame,
                text="log_name placeholder",
                font="Noto, 12",
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                width=18,
                anchor="w",
            )
            log_helper_2 = tk.Label(
                main_log_frame,
                text="|",
                font="Noto, 12",
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                anchor="w",
            )
            log_log = tk.Label(
                main_log_frame,
                text="Waiting",
                font="Noto, 12",
                background=label_playlist_good_bg,
                foreground=label_playlist_good_fg,
                width=5,
                anchor="w",
            )

            main_frame.grid(row=row, column=col)
            self.frames.append(main_frame)
            self.frame_positions.append((row, col))
            self.playlist_name_labels.append(playlist_name)
            self.frame_platforms.append(PLATFORM_YOUTUBE_MUSIC)

            frame_idx = len(self.frames) - 1
            self.active_log_labels[frame_idx] = {
                "artist": log_artist,
                "name": log_name,
                "status": log_log,
                "keybind_entry": playlist_keybind,
                "cover": playlist_cover,
            }

            main_header_frame.grid(row=0, column=0, columnspan=2)
            main_log_frame.grid(row=1, column=0)

            playlist_cover.grid(row=0, column=0, sticky="ne", rowspan=2)
            playlist_name.grid(row=0, column=1, sticky="nswe")
            close_playlist.grid(row=0, column=2, sticky="ne")
            playlist_keybind.grid(row=1, column=1, sticky="nswe")
            reload_database.grid(row=1, column=2, sticky="ne")

            log_artist.grid(row=0, column=0, padx=(0, 2))
            log_helper_1.grid(row=0, column=1)
            log_name.grid(row=0, column=2)
            log_helper_2.grid(row=0, column=3)
            log_log.grid(row=0, column=4, padx=(0, 2))

        self._auto_resize()

    def _hide_main_content(self) -> None:
        for frame in self.frames:
            frame.grid_forget()

    def _show_main_content(self) -> None:
        for frame, (row, col) in zip(self.frames, self.frame_positions):
            frame.grid(row=row, column=col)

    def close_main_frame(self, frame) -> None:
        try:
            index = self.frames.index(frame)
            playlist_name = self.playlist_name_labels[index].cget("text")
            platform = self.frame_platforms[index]

            self.kc.unregister_hotkey(playlist_name, platform=platform)
            if self._recording_frame_idx == index:
                self.kc.stop_recording()
                self._recording_frame_idx = None

            self.frames.pop(index)
            self.frame_positions.pop(index)
            self.playlist_name_labels.pop(index)
            self.frame_platforms.pop(index)

            # Release the cover-image references for the closing frame so the
            # PhotoImages can be garbage-collected (they were keyed by the
            # cover label widget in _apply_cover).
            closing_labels = self.active_log_labels.get(index)
            if closing_labels is not None:
                cover = closing_labels.get("cover")
                if cover is not None:
                    refs = self.frame_img_refs.pop(cover, None)
                    if refs:
                        refs.clear()

            if index in self.active_log_labels:
                del self.active_log_labels[index]

            new_active_log_labels = {}
            for old_idx, labels_dict in self.active_log_labels.items():
                if old_idx > index:
                    new_active_log_labels[old_idx - 1] = labels_dict
                else:
                    new_active_log_labels[old_idx] = labels_dict
            self.active_log_labels = new_active_log_labels

            PlaylistStore.delete_playlist(playlist_name, platform=platform)
            DatabaseManager.delete_playlist_db(playlist_name, platform)

            frame.grid_forget()
            frame.destroy()
            self._reorder_frames()
            logger.debug("Closed frame at index %d", index)
            self._auto_resize()
        except (ValueError, IndexError) as e:
            logger.error("Error closing frame: %s", e)

    # ------------------------------------------------------------------
    # Auto-resize
    # ------------------------------------------------------------------

    @staticmethod
    def _read_auto_resize_setting() -> bool:
        """Read the auto-resize setting once."""
        try:
            ensure_settings_file()
            cfg = ConfigParser()
            cfg.read(str(_settings_path))
            return cfg.getboolean("auto_resize", "is_true", fallback=False)
        except Exception:
            return False

    @staticmethod
    def _read_hide_to_tray_setting() -> bool:
        """Read the hide-to-tray setting once."""
        try:
            ensure_settings_file()
            cfg = ConfigParser()
            cfg.read(str(_settings_path))
            return cfg.getboolean("hide_to_tray", "is_true", fallback=False)
        except Exception:
            return False

    def _auto_resize(self) -> None:
        """Resize the window to fit playlist frames."""
        if not self._auto_resize_enabled:
            return
        try:
            resize_window(self.root)
        except Exception as e:
            logger.debug("Auto-resize failed: %s", e)

    def _reorder_frames(self) -> None:
        self.frame_positions.clear()
        for i, frame in enumerate(self.frames):
            col = i % 2
            row = (i // 2) + 1
            self.frame_positions.append((row, col))
            frame.grid(row=row, column=col)
        logger.debug("Reordered frames after deletion")

    # ------------------------------------------------------------------
    # Keybind recording
    # ------------------------------------------------------------------

    def _start_recording(self, frame_idx: int) -> str:
        if frame_idx >= len(self.playlist_name_labels):
            return "break"
        if frame_idx not in self.active_log_labels:
            return "break"
        if self._recording_frame_idx is not None:
            self._stop_recording(self._recording_frame_idx)

        self._recording_frame_idx = frame_idx
        entry = self.active_log_labels[frame_idx]["keybind_entry"]
        entry.config(
            state="normal",
            readonlybackground=C["label_playlist_error_bg"],
            background=C["entry_playlist_bg"],
        )
        entry.delete(0, tk.END)

        def on_combo(combo: str) -> None:
            entry.config(state="normal")
            entry.delete(0, tk.END)
            entry.insert(0, combo)

        def on_stop() -> None:
            # Only commit the empty keybind if this frame was still the
            # active recording target when the stop fired.
            was_recording_here = self._recording_frame_idx == frame_idx
            self._recording_frame_idx = None
            entry.config(
                state="readonly", readonlybackground=C["entry_playlist_ro_bg"]
            )
            entry.delete(0, tk.END)
            if not was_recording_here:
                return
            # Escape / focus-out during recording: commit the empty combo so
            # the previously registered hotkey is removed and the store
            # matches what the entry now shows - a stale hotkey firing with
            # a blank entry is confusing.
            playlist_name = self.playlist_name_labels[frame_idx].cget("text")
            platform = self.frame_platforms[frame_idx]
            PlaylistStore.update_keybind(playlist_name, platform, "")
            self.kc.unregister_hotkey(playlist_name, platform=platform)

        self.kc.start_recording(on_combo, on_stop=on_stop)
        return "break"

    def _stop_recording(self, frame_idx: int) -> None:
        if self._recording_frame_idx != frame_idx:
            return
        self._recording_frame_idx = None
        combo = self.kc.stop_recording()

        entry = self.active_log_labels[frame_idx]["keybind_entry"]
        entry.config(
            state="readonly", readonlybackground=C["entry_playlist_ro_bg"]
        )
        entry.delete(0, tk.END)

        playlist_name = self.playlist_name_labels[frame_idx].cget("text")
        platform = self.frame_platforms[frame_idx]

        if combo:
            entry.insert(0, combo)
            PlaylistStore.update_keybind(playlist_name, platform, combo)
            self.kc.register_hotkey(
                playlist_name,
                combo,
                self._make_keybind_callbacks(frame_idx),
                platform=platform,
            )
        else:
            PlaylistStore.update_keybind(playlist_name, platform, "")
            self.kc.unregister_hotkey(playlist_name, platform=platform)

    def _on_root_click(self, event) -> None:
        if self._recording_frame_idx is not None:
            entry = self.active_log_labels[self._recording_frame_idx]["keybind_entry"]
            if event.widget != entry:
                self.root.after(1, self._stop_recording, self._recording_frame_idx)

    # ------------------------------------------------------------------
    # Reload database (delegates to PlaylistSyncService)
    # ------------------------------------------------------------------

    def _on_reload_requested(self, frame_idx: int) -> None:
        """User clicked the reload button for a playlist frame."""
        if frame_idx >= len(self.playlist_name_labels):
            return
        playlist_name = self.playlist_name_labels[frame_idx].cget("text")
        platform = self.frame_platforms[frame_idx]
        playlist_data = PlaylistStore.find_playlist(playlist_name, platform)
        playlist_id = playlist_data.get("playlist_id", "") if playlist_data else ""

        if not playlist_id:
            logger.warning("No playlist_id for '%s', cannot reload", playlist_name)
            return

        status_label = self.active_log_labels[frame_idx]["status"]
        status_label.config(text="Sync", background=C["label_playlist_warn_bg"])

        def on_done(
            name: str, count: int, status_text: str, thumb_url: str | None
        ) -> None:
            self.root.after(
                0,
                self._on_reload_done,
                name,
                count,
                status_text,
                thumb_url,
                frame_idx,
            )
        try:
            DatabaseManager.close_thread_connections()
        except Exception as e:
            logger.warning("Failed to close DB connections before reload: %s", e)

        self._sync_service.reload_database(
            playlist_name, platform, playlist_id, on_done
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        self.img_refs.clear()
        self.frame_img_refs.clear()
        self.active_log_labels.clear()
        for frame in self.frames:
            try:
                frame.grid_forget()
                frame.destroy()
            except Exception as e:
                logger.warning("Error destroying frame: %s", e)
        self.frames.clear()
        self.frame_positions.clear()
        self.playlist_name_labels.clear()
        self.frame_platforms.clear()
        # Release cached per-thread SQLite connections held by the UI thread.
        try:
            DatabaseManager.close_thread_connections()
        except Exception as e:
            logger.warning("Error closing database connections: %s", e)
