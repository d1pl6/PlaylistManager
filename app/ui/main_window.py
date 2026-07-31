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
    get_theme_value,
    SETTINGS_PATH as _settings_path,
)

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

        # ----- state ---------------------------------------------------
        self.frames: list[tk.Frame] = []
        self.frame_positions: list[tuple[int, int]] = []
        self.playlist_name_labels: list[tk.Label] = []
        self.frame_platforms: list[str] = []
        self.active_log_labels: dict[int, dict] = {}
        self.img_refs: list = []
        self.frame_img_refs: dict = {}
        self._choose_open = False
        self._recording_frame_idx: int | None = None

        # Cache the auto-resize setting at startup (Issue #11).
        self._auto_resize_enabled = self._read_auto_resize_setting()

        # ----- services ------------------------------------------------
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
        self.root.configure(background="#1A1A1A", pady=5, padx=5)
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

        header_bg = self._theme_bg("frame_header", "background", "#181818")
        self.header_frame = tk.Frame(self.root, background=header_bg)
        self.header_frame.bind("<B1-Motion>", self.on_drag)

        self._create_widgets()
        self.header_frame.bind("<Button-1>", self.start_drag)
        self.root.bind("<Button-1>", self._on_root_click, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.ac.quit_app)

    # ------------------------------------------------------------------
    # Theme helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _theme_bg(section: str, option: str, default: str) -> str:
        try:
            return get_theme_value(section, option, default)
        except Exception:
            return default

    def apply_theme(self) -> None:
        header_bg = self._theme_bg("frame_header", "background", "#181818")
        self.header_frame.configure(background=header_bg)
        for widget in self.header_frame.winfo_children():
            if isinstance(widget, tk.Frame):
                widget.configure(background=header_bg)

        for frame in self.frames:
            main_bg = self._theme_bg("frame_main", "background", "#404040")
            frame.configure(background=main_bg)
            for child in frame.winfo_children():
                try:
                    child.configure(background=main_bg)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Widget creation
    # ------------------------------------------------------------------

    def _create_widgets(self) -> None:
        btn_header_bg = self._theme_bg("button_header", "background", "#6c6c6c")
        btn_header_abg = self._theme_bg("button_header", "activebackground", "#868686")
        btn_close_bg = self._theme_bg("button_close", "background", "#160000")
        btn_close_abg = self._theme_bg("button_close", "activebackground", "#390000")
        btn_close_fg = self._theme_bg("button_close", "foreground", "#FFFFFF")
        btn_close_afg = self._theme_bg("button_close", "activeforeground", "#ffffff")

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

        self.close_btn = tk.Button(
            self.header_frame,
            text="\u2715",
            cursor="hand2",
            command=self.ac.quit_app,
            background=btn_close_bg,
            foreground=btn_close_fg,
            activebackground=btn_close_abg,
            activeforeground=btn_close_afg,
            fg="white",
            bd=0,
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
                self.root, keybind_controller=self.kc, on_theme_change=self.apply_theme
            ),
        )

        self.header_frame.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.header_frame.grid_columnconfigure(0, weight=1)
        self.header_frame.grid_columnconfigure(1, weight=0)
        self.header_frame.grid_columnconfigure(2, weight=1)

        self.btn_login.grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.btn_add_playlist.grid(row=0, column=1, padx=4, pady=4)
        self.close_btn.grid(row=0, column=2, sticky="e")
        self.btn_open_settings.grid(row=0, column=3, sticky="e", padx=4, pady=4)

    # ------------------------------------------------------------------
    # Playlist dialog workflow (delegates to PlaylistController)
    # ------------------------------------------------------------------

    def _open_playlist_dialog(self) -> None:
        self._playlist_controller.open_playlist_dialog()

    def _show_platform_picker(self, platforms, callback) -> None:
        """Create a Toplevel to pick a platform (Issue #1, #12)."""
        win = tk.Toplevel(self.root)
        win.title("Choose Platform")
        win.configure(background="#2A2A2A")
        win.transient(self.root)
        center_window(win)  # Issue #12 — was never centred
        win.grab_set()

        tk.Label(
            win,
            text="Select platform to fetch playlists from:",
            background="#2A2A2A",
            foreground="white",
            font="Noto, 11",
        ).pack(pady=10, padx=20)

        for integration in platforms:
            tk.Button(
                win,
                text=integration.display_name,
                background="#404040",
                foreground="white",
                font="Noto, 11",
                width=30,
                command=lambda i=integration: (win.destroy(), callback(i)),
            ).pack(pady=4, padx=20)

        tk.Button(
            win,
            text="Cancel",
            background="#0A0000",
            foreground="white",
            font="Noto, 10",
            command=win.destroy,
        ).pack(pady=10)

    def _show_playlist_dialog(self, playlists, integration, on_select, on_cancel) -> None:
        """Create the playlist selection dialog (Issue #1)."""
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
        dialog.show(playlists, integration)

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
        """Create a new frame for the selected playlist (Issue #1)."""
        self.btn_add_playlist.configure(state="normal", image=self.add_playlist_img)
        self._show_main_content()
        self.create_main_frame(1)

        if self.playlist_name_labels:
            self.playlist_name_labels[-1].config(text=playlist_name)
            self.frame_platforms[-1] = platform

            frame_idx = len(self.frames) - 1
            status_label = self.active_log_labels[frame_idx]["status"]
            status_label.config(text="Sync", background="#5A4A00")

            if thumb_url:
                self._set_playlist_cover(frame_idx, thumb_url)

            self._import_playlist_tracks(playlist_name, platform, playlist_id, frame_idx)

    # ------------------------------------------------------------------
    # Thumbnail management
    # ------------------------------------------------------------------

    def _set_playlist_cover(self, frame_idx: int, thumb_url: str) -> None:
        """Fetch and apply a playlist thumbnail in a background thread."""
        if frame_idx not in self.active_log_labels:
            return
        cover_label = self.active_log_labels[frame_idx].get("cover")
        if not cover_label:
            return

        def fetch() -> None:
            tk_img = ThumbnailService.fetch_thumbnail(thumb_url, size=(64, 64))
            if tk_img:
                self.root.after(0, lambda: self._apply_cover(frame_idx, tk_img))

        threading.Thread(target=fetch, daemon=True).start()

    def _apply_cover(self, frame_idx: int, tk_img) -> None:
        if frame_idx not in self.active_log_labels:
            return
        cover_label = self.active_log_labels[frame_idx].get("cover")
        if not cover_label:
            return
        cover_label.configure(image=tk_img)
        self.frame_img_refs.setdefault(id(cover_label), []).append(tk_img)

    # ------------------------------------------------------------------
    # Database / log label helpers
    # ------------------------------------------------------------------

    def _update_log_labels_from_db(
        self, frame_idx: int, playlist_name: str, platform: str
    ) -> None:
        """Refresh the artist / song-name labels from the playlist DB.

        Reads the most recently added song so the frame shows real data
        (instead of the initial placeholders) as soon as an import or
        reload has populated the database.
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
        """Start importing tracks in a background thread (Issue #1)."""
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
            status_label.config(text="OK", background="#006713")
        elif status_text == "Error":
            status_label.config(text=status_text, background="#A00000")
        else:
            status_label.config(text=status_text, background="#006713")
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
    # Setup (called once after __init__)
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
            labels["status"].config(text="", background="SystemButtonFace")
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
    # Frame creation / layout
    # ------------------------------------------------------------------

    def create_main_frame(self, num: int) -> None:
        start_index = len(self.frames)
        for i in range(start_index, start_index + num):
            col = i % 2
            row = (i // 2) + 1

            main_bg = self._theme_bg("frame_main", "background", "#404040")
            main_frame = tk.Frame(self.root, width=320, background=main_bg)
            main_header_frame = tk.Frame(main_frame, background=main_bg)
            main_log_frame = tk.Frame(main_frame, background=main_bg)

            playlist_cover = tk.Label(
                main_header_frame,
                image=self.playlist_cover_img,
                background=main_bg,
            )
            playlist_name = tk.Label(
                main_header_frame,
                text=f"row:{row} col:{col}",
                font="Noto, 12",
                background=main_bg,
                width=25,
            )

            close_playlist = tk.Button(
                main_header_frame,
                image=self.close_playlist_img,
                background="#404040",
                command=lambda f=main_frame: self.close_main_frame(f),
            )

            playlist_keybind = tk.Entry(
                main_header_frame,
                font="Noto, 12",
                justify="center",
                background=main_bg,
                readonlybackground="#2A2A2A",
                foreground="white",
                state="readonly",
            )
            playlist_keybind.bind(
                "<Button-1>",
                lambda e, frame_idx=len(self.frames): self._start_recording(frame_idx),
            )

            reload_database = tk.Button(
                main_header_frame,
                image=self.reload_database_img,
                background="#404040",
                command=lambda idx=len(self.frames): self._on_reload_requested(idx),
            )

            log_artist = tk.Label(
                main_log_frame,
                text="log_artist placeholder",
                font="Noto, 12",
                background=main_bg,
                width=8,
                anchor="w",
            )
            log_helper_1 = tk.Label(
                main_log_frame,
                text="-",
                font="Noto, 12",
                background=main_bg,
                anchor="w",
            )
            log_name = tk.Label(
                main_log_frame,
                text="log_name placeholder",
                font="Noto, 12",
                background=main_bg,
                width=18,
                anchor="w",
            )
            log_helper_2 = tk.Label(
                main_log_frame,
                text="|",
                font="Noto, 12",
                background=main_bg,
                anchor="w",
            )
            log_log = tk.Label(
                main_log_frame,
                text="Waiting",
                font="Noto, 12",
                background="#006713",
                foreground="white",
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

            self.kc.unregister_hotkey(playlist_name)
            if self._recording_frame_idx == index:
                self.kc.stop_recording()
                self._recording_frame_idx = None

            self.frames.pop(index)
            self.frame_positions.pop(index)
            self.playlist_name_labels.pop(index)
            self.frame_platforms.pop(index)

            if index in self.active_log_labels:
                del self.active_log_labels[index]

            new_active_log_labels = {}
            for old_idx, labels_dict in self.active_log_labels.items():
                if old_idx > index:
                    new_active_log_labels[old_idx - 1] = labels_dict
                else:
                    new_active_log_labels[old_idx] = labels_dict
            self.active_log_labels = new_active_log_labels

            if frame in self.frame_img_refs:
                self.frame_img_refs[frame].clear()
                del self.frame_img_refs[frame]

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
        """Read the auto-resize setting once (Issue #11)."""
        try:
            ensure_settings_file()
            cfg = ConfigParser()
            cfg.read(str(_settings_path))
            return cfg.getboolean("auto_resize", "is_true", fallback=False)
        except Exception:
            return False

    def _auto_resize(self) -> None:
        """Resize the window to fit playlist frames (uses cached setting)."""
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
    # Keybind recording (kept in MainWindow — tightly bound to widgets)
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
        entry.config(state="normal", readonlybackground="#A00000", background="#404040")
        entry.delete(0, tk.END)

        def on_combo(combo: str) -> None:
            entry.config(state="normal")
            entry.delete(0, tk.END)
            entry.insert(0, combo)

        def on_stop() -> None:
            self._recording_frame_idx = None
            entry.config(state="readonly", readonlybackground="#2A2A2A")
            entry.delete(0, tk.END)

        self.kc.start_recording(on_combo, on_stop=on_stop)
        return "break"

    def _stop_recording(self, frame_idx: int) -> None:
        if self._recording_frame_idx != frame_idx:
            return
        self._recording_frame_idx = None
        combo = self.kc.stop_recording()

        entry = self.active_log_labels[frame_idx]["keybind_entry"]
        entry.config(state="readonly", readonlybackground="#2A2A2A")
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
            self.kc.unregister_hotkey(playlist_name)

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
        status_label.config(text="Sync", background="#5A4A00")

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
