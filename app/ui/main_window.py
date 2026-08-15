"""Main application window.

Responsible for widget creation, layout, theme application, frame
management, and thin hook methods that delegate business logic to
controllers and services.
"""

import logging
import threading
import tkinter as tk
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
from utils.icons import IconService
from utils.scaling import px, ui_font
from ui.login_ui import show_login_dialog
from ui.playlist_dialog import PlaylistDialog
from ui.settings_ui import show_settings_dialog
from utils.window import center_window, resize_window
from utils.config import get_setting, get_setting_value
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

# Base design sizes of the playlist card, in unscaled pixels; every value
# is multiplied by the UI scale (utils/scaling).  The card is a fixed-size
# box (grid_propagate(False)) so text never makes it grow — long names and
# track titles clip inside instead of ballooning the window at high scale.
CARD_W_BASE = 320
CARD_H_BASE = 96
# Showcase geometry: each song block is two font-12 lines (name + artists,
# ~46 px) plus the thumbnail's 2 px top padding = 48 px; a 40 px thumbnail
# fits in that block.  The log row is one font-12 line (~23 px), so a card
# with the log hidden shrinks by that much.  (46 would clip the last row's
# bottom edge once the card grows beyond a couple of songs.)
SONG_UNIT_H_BASE = 48
LOG_ROW_H_BASE = 23


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
        self._recording_frame_idx: int | None = None

        self._auto_resize_enabled = self._read_auto_resize_setting()
        self._hide_to_tray = self._read_hide_to_tray_setting()
        self._showcase_count = self._read_showcase_count_setting()
        self._show_log = self._read_show_log_setting()

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
        self.song_placeholder_img = self._load_song_placeholder()

        self.root.grid_rowconfigure(0, weight=0)
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
        """Re-apply the palette from cfg/theme.ini to every existing widget.

        Called after a theme change (theme picker).  The status labels are
        left untouched - their colour is dynamic (good/warn/error) and is
        refreshed by the status callbacks, which read ``C`` at update time.
        """
        load_theme()
        self.root.configure(background=C["root_bg"])

        header_bg = C["frame_head_bg"]
        self.header_frame.configure(background=header_bg)
        for widget in self.header_frame.winfo_children():
            if isinstance(widget, tk.Button):
                widget.configure(
                    background=C["button_head_bg"],
                    activebackground=C["button_head_a_bg"],
                )

        frame_playlist_bg = C["frame_playlist_bg"]
        for frame_idx, frame in enumerate(self.frames):
            frame.configure(background=frame_playlist_bg)
            labels = self.active_log_labels.get(frame_idx)
            if labels is None:
                continue

            name_label = self.playlist_name_labels[frame_idx]
            name_label.configure(
                background=C["label_playlist_name_bg"],
                foreground=C["label_playlist_name_fg"],
            )
            labels["cover"].configure(background=C["label_playlist_bg"])

            for key in ("artist", "name"):
                labels[key].configure(
                    background=C["label_playlist_log_bg"],
                    foreground=C["label_playlist_log_fg"],
                )

            entry = labels["keybind_entry"]
            if self._recording_frame_idx != frame_idx:
                entry.configure(
                    background=C["entry_playlist_bg"],
                    foreground=C["entry_playlist_fg"],
                    readonlybackground=C["entry_playlist_ro_bg"],
                )

            # Remaining descendants: the two log separator labels and the
            # close/reload buttons.  The status label is deliberately
            # skipped (dynamic colour, see docstring).
            known_labels = {
                labels["cover"],
                labels["status"],
                labels["artist"],
                labels["name"],
                name_label,
            }
            for child in frame.winfo_children():
                for widget in child.winfo_children():
                    if isinstance(widget, tk.Label) and widget not in known_labels:
                        widget.configure(
                            background=C["label_playlist_log_bg"],
                            foreground=C["label_playlist_log_fg"],
                        )
                    elif isinstance(widget, tk.Button):
                        widget.configure(
                            background=C["button_playlist_bg"],
                            foreground=C["button_playlist_fg"],
                            activebackground=C["button_playlist_a_bg"],
                            activeforeground=C["button_playlist_a_fg"],
                        )

    # ------------------------------------------------------------------
    # Widget creation
    # ------------------------------------------------------------------

    def _create_widgets(self) -> None:
        btn_header_bg = C["button_head_bg"]
        btn_header_abg = C["button_head_a_bg"]

        login_img_path = assets_dir / "login.png"
        self.login_img = IconService.get(login_img_path, 32)
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
        self.add_playlist_img = IconService.get(add_playlist_img_path, 32)
        self.btn_add_playlist = tk.Button(
            self.header_frame,
            image=self.add_playlist_img,
            cursor="hand2",
            background=btn_header_bg,
            activebackground=btn_header_abg,
            command=self._open_playlist_dialog,
        )

        open_settings_img_path = assets_dir / "settings.png"
        self.open_settings_img = IconService.get(open_settings_img_path, 32)
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
                on_auto_resize_toggle=self.set_auto_resize,
                on_showcase_count_change=self.set_showcase_count,
                on_showcase_log_change=self.set_showcase_log,
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

    def _show_platform_picker(self, platforms, callback, on_cancel=None) -> None:
        """Create a Toplevel to pick a platform.

        ``on_cancel`` (the controller's cancel path) fires when the user
        dismisses the picker without choosing, so the controller can
        release its re-entrancy guard.
        """
        win_bg = C["frame_main_bg"]
        label_fg = C["label_def_fg"]
        btn_bg = C["button_main_bg"]
        btn_fg = C["button_main_fg"]
        btn_a_bg = C["button_main_a_bg"]
        btn_a_fg = C["button_main_a_fg"]
        cancel_bg = C["button_head_bg"]
        cancel_fg = C["button_head_fg"]
        cancel_a_bg = C["button_head_a_bg"]

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
                background=btn_bg,
                foreground=btn_fg,
                activebackground=btn_a_bg,
                activeforeground=btn_a_fg,
                font=ui_font(11),
                width=30,
                command=lambda i=integration: (win.destroy(), callback(i)),
            ).pack(pady=4, padx=20)

        tk.Button(
            win,
            text="Cancel",
            background=cancel_bg,
            foreground=cancel_fg,
            activebackground=cancel_a_bg,
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
                cover_label = self.active_log_labels[frame_idx].get("cover")
                if cover_label:
                    self._set_playlist_cover(cover_label, thumb_url)

            self._import_playlist_tracks(playlist_name, platform, playlist_id, frame_idx)

    # ------------------------------------------------------------------
    # Thumbnail management
    # ------------------------------------------------------------------

    def _set_playlist_cover(self, cover_label: tk.Label, thumb_url: str) -> None:
        """Download a playlist thumbnail in a background thread.

        Only the download + resize run off-thread (thread-safe); the
        PhotoImage is created on the main thread by :meth:`_apply_cover`
        because tkinter is not thread-safe.

        The cover *widget* is captured, not a frame index: after
        close_main_frame() renumbers ``self.frames``/``active_log_labels``,
        a captured index could silently resolve to a different frame (or
        out of range).  A widget reference stays unambiguous - the apply
        side just checks it still exists.
        """
        def fetch() -> None:
            img = ThumbnailService.fetch_image(thumb_url, size=(px(64), px(64)))
            if img is not None:
                try:
                    self.root.after(0, lambda: self._apply_cover(cover_label, img))
                except Exception:
                    logger.debug("Window closed during cover download", exc_info=True)

        threading.Thread(target=fetch, daemon=True).start()

    def _apply_cover(self, cover_label: tk.Label, img) -> None:
        try:
            if not cover_label.winfo_exists():
                return
            tk_img = ThumbnailService.to_photoimage(img)
        except Exception as e:
            logger.error(f"Failed to create cover PhotoImage: {e}")
            return
        try:
            cover_label.configure(image=tk_img)
        except tk.TclError:
            # Widget destroyed between winfo_exists() and configure().
            return
        # Replace, not append: every reload otherwise piles a new
        # PhotoImage reference onto the same label, leaking them for the
        # life of the frame.  The label now displays the new image, so the
        # old one can be released immediately.
        self.frame_img_refs[cover_label] = [tk_img]

    # ------------------------------------------------------------------
    # Showcase (last-N-songs section of a playlist card)
    # ------------------------------------------------------------------

    def _load_song_placeholder(self):
        """Load the song-thumbnail placeholder, falling back defensively.

        ``assets/album_img.png`` is gitignored, so a fresh clone does not
        have it - never let its absence crash startup.
        """
        album_path = assets_dir / "album_img.png"
        try:
            return IconService.get(album_path, 40)
        except FileNotFoundError:
            logger.debug(
                "album_img.png placeholder missing; falling back to playlist_image.png"
            )
            # 40 px, not the 64 px cover: a 64 px image in the 40 px slot
            # would inflate the thumbnail column on a fresh clone.
            return IconService.get(playlist_cover_img_path, 40)

    def _prune_frame_imgs(self, container) -> None:
        """Release every PhotoImage ref held for labels inside *container*.

        The cover and showcase-thumbnail refs are keyed by their Label
        widgets in ``frame_img_refs``; before a frame (or its showcase
        section) is destroyed the refs must be popped and cleared or the
        PhotoImages stay alive for the process lifetime.
        """
        for child in container.winfo_children():
            if isinstance(child, tk.Label):
                refs = self.frame_img_refs.pop(child, None)
                if refs:
                    refs.clear()
            elif isinstance(child, tk.Frame):
                self._prune_frame_imgs(child)

    def _fetch_song_thumb(self, thumb_label: tk.Label, thumb_url: str) -> None:
        """Download a song thumbnail in a background thread (showcase rows).

        Same pattern as :meth:`_set_playlist_cover`: fetch on a worker,
        marshal the plain PIL image to the main thread where
        :meth:`_apply_cover` builds the PhotoImage (tkinter is not
        thread-safe).
        """
        def fetch() -> None:
            img = ThumbnailService.fetch_image(thumb_url, size=(px(40), px(40)))
            if img is not None:
                try:
                    self.root.after(0, lambda: self._apply_cover(thumb_label, img))
                except Exception:
                    logger.debug(
                        "Window closed during song thumb download", exc_info=True
                    )

        threading.Thread(target=fetch, daemon=True).start()

    def _refresh_showcase(
        self, frame_idx: int, playlist_name: str, platform: str
    ) -> None:
        """Rebuild the last-N-songs showcase section of a playlist card.

        DB-driven and cheap: re-reads the newest songs whenever song data
        changes (startup, import/reload, hotkey add, removal, settings).
        The section is destroyed and rebuilt so rows always match the
        database and no stale widgets linger.
        """
        try:
            main_frame = self.frames[frame_idx]
        except IndexError:
            return

        # Drop the previous showcase section (and its thumbnails).
        old_showcase = getattr(main_frame, "showcase_frame", None)
        if old_showcase is not None:
            self._prune_frame_imgs(old_showcase)
            old_showcase.destroy()
            main_frame.showcase_frame = None

        rows = []
        if self._showcase_count > 0:
            rows = SongManager().get_latest_songs(
                playlist_name, self._showcase_count, platform=platform
            )

        main_frame.showcase_rows = len(rows)
        if rows:
            showcase_frame = self._build_showcase_frame(main_frame, rows)
            showcase_frame.grid(row=2, column=0, sticky="nsew")
            main_frame.showcase_frame = showcase_frame
            for thumb_label, url in showcase_frame._thumb_jobs:
                self._fetch_song_thumb(thumb_label, url)

        self._update_card_height(frame_idx)

    def _build_showcase_frame(self, main_frame: tk.Frame, songs: list) -> tk.Frame:
        """Create the song rows inside a fresh showcase frame.

        Returns the frame; the caller grids it.  Each song is a two-row
        block: thumbnail (rowspan 2) | name / artists | remove button -
        mirroring the header's cover/name/button layout.  Pending
        thumbnail fetches are stashed as ``(label, url)`` tuples on
        ``_thumb_jobs`` for the caller to start.
        """
        frame_playlist_bg = C["frame_playlist_bg"]
        label_playlist_log_bg = C["label_playlist_log_bg"]
        label_playlist_log_fg = C["label_playlist_log_fg"]
        button_playlist_bg = C["button_playlist_bg"]
        button_playlist_fg = C["button_playlist_fg"]
        button_playlist_a_bg = C["button_playlist_a_bg"]
        button_playlist_a_fg = C["button_playlist_a_fg"]

        showcase = tk.Frame(main_frame, background=frame_playlist_bg, padx=2, borderwidth=2, relief="solid")
        showcase.grid_columnconfigure(1, weight=1)

        jobs = []
        for row_idx, song in enumerate(songs):
            grid_row = row_idx * 2

            thumb = tk.Label(
                showcase,
                image=self.song_placeholder_img,
                background=label_playlist_log_bg,
            )
            song_name = tk.Label(
                showcase,
                text=song.get("title", ""),
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                anchor="w",
            )
            artists = song.get("artists", [])
            artists_str = (
                ", ".join(artists[:2]) if isinstance(artists, list) else str(artists)
            )
            song_artists = tk.Label(
                showcase,
                text=artists_str,
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                anchor="w",
            )
            remove_btn = tk.Button(
                showcase,
                image=self.close_playlist_img,
                cursor="hand2",
                background=button_playlist_bg,
                foreground=button_playlist_fg,
                activebackground=button_playlist_a_bg,
                activeforeground=button_playlist_a_fg,
                command=lambda f=main_frame, sid=song.get("id"), tid=song.get("track_id"): (
                    self._on_remove_song(f, sid, tid)
                ),
            )

            thumb.grid(
                row=grid_row, column=0, rowspan=2, sticky="nsew",
                padx=(0, 2), pady=(2, 0),
            )
            song_name.grid(row=grid_row, column=1, sticky="nsew")
            remove_btn.grid(row=grid_row, column=2, rowspan=2, sticky="ne")
            song_artists.grid(row=grid_row + 1, column=1, sticky="nsew")

            url = song.get("thumbnail_url") or ""
            if url:
                jobs.append((thumb, url))

        showcase._thumb_jobs = jobs
        return showcase

    def _on_remove_song(
        self, main_frame: tk.Frame, song_id: int, track_id: str
    ) -> None:
        """Remove one song: platform API first, then the local DB.

        Mirrors the add-flow invariant: a platform failure must not leave
        a local-only "success" - the track is still in the platform
        playlist and the next reload would re-import it anyway.  Runs on
        a worker thread (memory: platform round trips must never block
        the tkinter main thread).
        """
        try:
            frame_idx = self.frames.index(main_frame)
        except ValueError:
            return
        if frame_idx >= len(self.playlist_name_labels):
            return
        playlist_name = self.playlist_name_labels[frame_idx].cget("text")
        platform = self.frame_platforms[frame_idx]
        labels = self.active_log_labels.get(frame_idx)
        if labels is None:
            return
        status_label = labels["status"]

        # One removal in flight per frame; also refuse while a reload is
        # running (a reload starting mid-remove could re-import the track).
        if getattr(main_frame, "_removing", False) or getattr(
            main_frame, "_syncing", False
        ):
            return
        if not track_id or not song_id:
            # Legacy DB row without a platform id - nothing to remove
            # platform-side, so nothing may be deleted locally either.
            status_label.config(
                text="Error", background=C["label_playlist_error_bg"]
            )
            return
        main_frame._removing = True

        status_label.config(text="Removing", background=C["label_playlist_warn_bg"])

        buttons = self._frame_buttons(main_frame)
        for btn in buttons:
            try:
                btn.config(state="disabled")
            except tk.TclError:
                pass

        playlist_data = PlaylistStore.find_playlist(playlist_name, platform)
        playlist_id = playlist_data.get("playlist_id", "") if playlist_data else ""
        integration = self.integrations.get(platform) if self.integrations else None

        def work() -> None:
            ok = False
            if not playlist_id:
                logger.error(
                    "No playlist_id for '%s' (%s); cannot remove track %s",
                    playlist_name, platform, track_id,
                )
            elif integration is None or not integration.is_authenticated():
                logger.error(
                    "Integration %s not authenticated; cannot remove track %s",
                    platform, track_id,
                )
            else:
                ok = integration.remove_track(playlist_id, track_id)
                if ok:
                    SongManager().delete_song(
                        playlist_name, song_id, platform=platform
                    )

            def done() -> None:
                main_frame._removing = False
                try:
                    if not main_frame.winfo_exists():
                        return
                except tk.TclError:
                    return
                for btn in buttons:
                    try:
                        btn.config(state="normal")
                    except tk.TclError:
                        pass
                if ok:
                    status_label.config(
                        text="Removed", background=C["label_playlist_good_bg"]
                    )
                    try:
                        cur_idx = self.frames.index(main_frame)
                        pname = self.playlist_name_labels[cur_idx].cget("text")
                        self._refresh_showcase(
                            cur_idx, pname, self.frame_platforms[cur_idx]
                        )
                    except (ValueError, IndexError):
                        pass
                else:
                    status_label.config(
                        text="Error", background=C["label_playlist_error_bg"]
                    )

            try:
                self.root.after(0, done)
            except Exception:
                logger.debug(
                    "App is shutting down; dropped remove-done update",
                    exc_info=True,
                )

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _frame_buttons(main_frame: tk.Frame) -> list:
        """All Button widgets inside a card (close/reload + showcase removes)."""
        buttons = []
        for child in main_frame.winfo_children():
            if isinstance(child, tk.Frame):
                for widget in child.winfo_children():
                    if isinstance(widget, tk.Button):
                        buttons.append(widget)
        return buttons

    def _update_card_height(self, frame_idx: int) -> None:
        """Recompute a card's fixed height from its current showcase state.

        ``card_height = px(CARD_H_BASE) + rows * px(SONG_UNIT_H_BASE)``,
        minus one log row when the log row is hidden.  A card with no
        rows reserves no space - it grows as songs are added and shrinks
        when they are removed.
        """
        try:
            main_frame = self.frames[frame_idx]
        except IndexError:
            return
        rows = getattr(main_frame, "showcase_rows", 0)
        height = px(CARD_H_BASE) + rows * px(SONG_UNIT_H_BASE)
        if not self._show_log:
            height -= px(LOG_ROW_H_BASE)
        main_frame.config(height=max(px(CARD_H_BASE) - px(LOG_ROW_H_BASE), height))

    def _apply_log_visibility(self, frame_idx: int) -> None:
        """Show or hide the artist/name/status log row of a card."""
        try:
            main_frame = self.frames[frame_idx]
        except IndexError:
            return
        log_frame = getattr(main_frame, "main_log_frame", None)
        if log_frame is None:
            return
        if self._show_log:
            log_frame.grid()
        else:
            log_frame.grid_remove()
        self._update_card_height(frame_idx)

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
    ) -> int | None:
        """Handle a finished track import; returns the resolved frame index.

        The captured ``frame_idx`` can be stale after close_main_frame()
        renumbered the frames - accept it only if it still belongs to
        *playlist_name*, otherwise fall back to resolving by name so a
        finished import never updates a different frame's labels.
        """
        if frame_idx is not None:
            in_range = frame_idx < len(self.playlist_name_labels)
            matches_name = (
                in_range
                and self.playlist_name_labels[frame_idx].cget("text")
                == playlist_name
            )
            if not (matches_name and frame_idx in self.active_log_labels):
                frame_idx = self._find_frame_index_by_name(playlist_name)
        else:
            frame_idx = self._find_frame_index_by_name(playlist_name)
        if frame_idx is None or frame_idx not in self.active_log_labels:
            return None
        # The frame's reload sync finished (import or reload completion) -
        # allow remove clicks again.  Guarded: the frame may have been
        # closed and recreated, in which case the flag is already unset.
        try:
            main_frame = self.frames[frame_idx]
            main_frame._syncing = False
        except IndexError:
            pass
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
        # Import/reload changed the song set - rebuild the showcase rows
        # (this also covers the _on_reload_done path, which routes here).
        self._refresh_showcase(
            frame_idx, playlist_name, self.frame_platforms[frame_idx]
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
        if frame_idx is not None and thumb_url:
            cover_label = self.active_log_labels[frame_idx].get("cover")
            if cover_label:
                self._set_playlist_cover(cover_label, thumb_url)

    # ------------------------------------------------------------------
    # Keybind setup (called once after __init__)
    # ------------------------------------------------------------------

    def _make_keybind_callbacks(self, frame_idx: int) -> KeybindCallbacks:
        """Build a :class:`KeybindCallbacks` bound to *frame_idx* widgets.

        All callbacks are scheduled on the main thread (tkinter must be
        accessed from the main thread).
        """
        labels = self.active_log_labels[frame_idx]
        # Capture the frame widget, not the index: the callbacks object can
        # outlive close_main_frame() renumbering, so on_song_added resolves
        # the live index at callback time (memory: never capture an index).
        main_frame = self.frames[frame_idx]

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
            _set(labels["status"], text=text, background=background)

        def on_song_info(artist: str, name: str) -> None:
            _set(labels["artist"], text=artist)
            _set(labels["name"], text=name)

        def on_entry_state(state: str) -> None:
            _set(labels["keybind_entry"], state=state)

        def on_reset(entry_state: str) -> None:
            _set(labels["keybind_entry"], state=entry_state)
            _set(labels["status"], text="", background=C["frame_playlist_bg"])
            _set(labels["artist"], text="")
            _set(labels["name"], text="")

        def on_song_added() -> None:
            # Runs on the main thread (the flow schedules _apply there).
            # Resolve the frame's live index - the frame may have been
            # closed or renumbered since the callbacks were created.
            try:
                cur_idx = self.frames.index(main_frame)
            except ValueError:
                return
            try:
                playlist_name = self.playlist_name_labels[cur_idx].cget("text")
            except (IndexError, tk.TclError):
                return
            self._refresh_showcase(
                cur_idx, playlist_name, self.frame_platforms[cur_idx]
            )

        return KeybindCallbacks(
            on_status=on_status,
            on_song_info=on_song_info,
            on_entry_state=on_entry_state,
            on_reset=on_reset,
            on_song_added=on_song_added,
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
                        displaced = self.kc.register_hotkey(
                            name,
                            hotkey,
                            self._make_keybind_callbacks(i),
                            platform=platform,
                        )
                        if displaced:
                            # Self-heal stale stores: a displaced playlist's
                            # persisted hotkey no longer fires anything.
                            self._clear_displaced_keybind(displaced)

                    self._update_log_labels_from_db(i, name, platform)
                    self._refresh_showcase(i, name, platform)

                    thumb_url = playlist.get("thumbnail_url", "")
                    if thumb_url:
                        cover_label = self.active_log_labels[i].get("cover")
                        if cover_label:
                            self._set_playlist_cover(cover_label, thumb_url)

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
    # Frame creation / layout
    # ------------------------------------------------------------------

    def create_main_frame(self, num: int) -> None:
        start_index = len(self.frames)
        for i in range(start_index, start_index + num):
            col = i % 2
            row = (i // 2) + 1

            frame_playlist_bg = C["frame_playlist_bg"]
            label_playlist_bg = C["label_playlist_bg"]
            label_playlist_name_bg = C["label_playlist_name_bg"]
            label_playlist_name_fg = C["label_playlist_name_fg"]
            label_playlist_log_bg = C["label_playlist_log_bg"]
            label_playlist_log_fg = C["label_playlist_log_fg"]
            label_playlist_good_bg = C["label_playlist_good_bg"]
            label_playlist_good_fg = C["label_playlist_good_fg"]
            button_playlist_bg = C["button_playlist_bg"]
            button_playlist_fg = C["button_playlist_fg"]
            button_playlist_a_bg = C["button_playlist_a_bg"]
            button_playlist_a_fg = C["button_playlist_a_fg"]
            entry_playlist_bg = C["entry_playlist_bg"]
            entry_playlist_fg = C["entry_playlist_fg"]
            entry_playlist_ro_bg = C["entry_playlist_ro_bg"]

            # Fixed-size card: grid_propagate(False) + explicit scaled size
            # means text never makes the card grow (long names clip inside
            # instead).  Rows/columns use weights so inner frames stretch
            # and the weighted column absorbs/clips overflow text.
            main_frame = tk.Frame(
                self.root,
                width=px(CARD_W_BASE),
                height=px(CARD_H_BASE),
                background=frame_playlist_bg,
            )
            main_frame.grid_propagate(False)
            main_frame.grid_rowconfigure(0, weight=1)
            main_frame.grid_rowconfigure(1, weight=1)
            main_frame.grid_rowconfigure(2, weight=0)
            main_frame.grid_columnconfigure(0, weight=1)
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
                font=ui_font(12),
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
                font=ui_font(12),
                justify="center",
                background=entry_playlist_bg,
                foreground=entry_playlist_fg,
                readonlybackground=entry_playlist_ro_bg,
                state="readonly",
            )
            # Capture the frame widget, not its index: close_main_frame()
            # renumbers self.frames after deleting a frame, and a captured
            # index would then point at the wrong playlist (or out of range,
            # silently disabling the reload button).
            playlist_keybind.bind(
                "<Button-1>",
                lambda e, f=main_frame: self._start_recording(self.frames.index(f)),
            )

            reload_database = tk.Button(
                main_header_frame,
                image=self.reload_database_img,
                cursor="hand2",
                background=button_playlist_bg,
                foreground=button_playlist_fg,
                activebackground=button_playlist_a_bg,
                activeforeground=button_playlist_a_fg,
                command=lambda f=main_frame: self._on_reload_requested(
                    self.frames.index(f)
                ),
            )

            log_artist = tk.Label(
                main_log_frame,
                text="log_artist placeholder",
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                width=8,
                anchor="w",
            )
            log_helper_1 = tk.Label(
                main_log_frame,
                text="-",
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                anchor="w",
            )
            log_name = tk.Label(
                main_log_frame,
                text="log_name placeholder",
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                width=18,
                anchor="w",
            )
            log_helper_2 = tk.Label(
                main_log_frame,
                text="|",
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                anchor="w",
            )
            log_log = tk.Label(
                main_log_frame,
                text="Waiting",
                font=ui_font(12),
                background=label_playlist_good_bg,
                foreground=label_playlist_good_fg,
                width=5,
                anchor="w",
            )

            main_frame.grid(row=row, column=col, sticky="ne" if col == 1 else "nw", pady=(5,0), padx=2.5)
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

            # Weighted name column: absorbs/clips long text instead of
            # letting the card grow (the card itself never grows - see the
            # grid_propagate(False) above).
            main_header_frame.grid_columnconfigure(1, weight=1)
            main_log_frame.grid_columnconfigure(2, weight=1)

            main_header_frame.grid(row=0, column=0, sticky="nsew")
            main_log_frame.grid(row=1, column=0, sticky="nsew")

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

            # Per-frame state the showcase helpers read.  Widgets, not
            # indices: close_main_frame() renumbers the frame lists, but
            # attributes travel with the widget itself.
            main_frame.showcase_rows = 0
            main_frame.showcase_frame = None
            main_frame.main_log_frame = main_log_frame
            if not self._show_log:
                main_log_frame.grid_remove()
            self._update_card_height(i)

        self._auto_resize()

    def _hide_main_content(self) -> None:
        for frame in self.frames:
            frame.grid_forget()

    def _show_main_content(self) -> None:
        for frame, (row, col) in zip(self.frames, self.frame_positions):
            frame.grid(row=row, column=col, sticky="ne" if col == 1 else "nw", pady=(5,0), padx=2.5)

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

            # Release the PhotoImage references for the closing frame's
            # labels (cover + showcase thumbnails - keyed by widget in
            # _apply_cover) so they can be garbage-collected before the
            # widgets are destroyed.
            self._prune_frame_imgs(frame)

            closing_labels = self.active_log_labels.get(index)

            if index in self.active_log_labels:
                del self.active_log_labels[index]

            new_active_log_labels = {}
            for old_idx, labels_dict in self.active_log_labels.items():
                if old_idx > index:
                    new_active_log_labels[old_idx - 1] = labels_dict
                else:
                    new_active_log_labels[old_idx] = labels_dict
            self.active_log_labels = new_active_log_labels

            # The recording target's frame is still open, just shifted down
            # by one - keep the state index in sync with the renumbering.
            if (
                self._recording_frame_idx is not None
                and self._recording_frame_idx > index
            ):
                self._recording_frame_idx -= 1

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
        return get_setting("auto_resize", False)

    @staticmethod
    def _read_hide_to_tray_setting() -> bool:
        """Read the hide-to-tray setting once."""
        return get_setting("hide_to_tray", False)

    @staticmethod
    def _read_showcase_count_setting() -> int:
        """Read the showcase count once; clamp to [0, 20].

        0 (the default) turns the showcase off entirely.
        """
        try:
            raw = get_setting_value("showcase", "count", "0")
            return min(20, max(0, int(str(raw).strip())))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _read_show_log_setting() -> bool:
        """Read the show-log-row setting once."""
        return get_setting("showcase_log", True)

    def set_showcase_count(self, count: int) -> None:
        """Live-apply the showcase count (called from the Settings dialog).

        Rebuilds every card's showcase section in place - no window
        rebuild.  Falls back to a full rebuild when no frames exist yet.
        """
        try:
            self._showcase_count = min(20, max(0, int(count)))
        except (ValueError, TypeError):
            return
        for frame_idx in list(self.active_log_labels):
            try:
                playlist_name = self.playlist_name_labels[frame_idx].cget("text")
                platform = self.frame_platforms[frame_idx]
            except (IndexError, tk.TclError):
                continue
            self._refresh_showcase(frame_idx, playlist_name, platform)
        self._auto_resize()

    def set_showcase_log(self, show: bool) -> None:
        """Live-apply the show-log-row setting (called from Settings)."""
        self._show_log = bool(show)
        for frame_idx in list(self.active_log_labels):
            self._apply_log_visibility(frame_idx)
        self._auto_resize()

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
            frame.grid(row=row, column=col, sticky="ne" if col == 1 else "nw", pady=(5,0), padx=2.5)
        logger.debug("Reordered frames after deletion")

    # ------------------------------------------------------------------
    # Keybind recording
    # ------------------------------------------------------------------

    def _clear_displaced_keybind(self, displaced: dict) -> None:
        """Clear a hotkey that was just taken over by another playlist.

        Recording the same combo on playlist B silently displaces playlist
        A's binding (``KeybindRegistry.register`` returns the displaced
        info).  A's entry must stop showing the stolen combo and its
        persisted keybind must be cleared - otherwise the app would
        display a hotkey that fires B, and a restart would resurrect the
        collision (leaving the combo bound to nothing once B's frame is
        closed).
        """
        name = displaced.get("playlist_name")
        platform = displaced.get("platform", PLATFORM_YOUTUBE_MUSIC)
        if not name:
            return
        for i, label in enumerate(self.playlist_name_labels):
            if label.cget("text") == name and self.frame_platforms[i] == platform:
                entry = self.active_log_labels.get(i, {}).get("keybind_entry")
                if entry is not None:
                    try:
                        entry.config(state="normal")
                        entry.delete(0, tk.END)
                        entry.config(state="readonly")
                    except tk.TclError:
                        pass
                break
        PlaylistStore.update_keybind(name, platform, "")

    def _start_recording(self, frame_idx: int) -> str:
        if frame_idx >= len(self.playlist_name_labels):
            return "break"
        if frame_idx not in self.active_log_labels:
            return "break"
        if self._recording_frame_idx is not None:
            self._stop_recording(self._recording_frame_idx)

        self._recording_frame_idx = frame_idx
        recording_frame = self.frames[frame_idx]
        entry = self.active_log_labels[frame_idx]["keybind_entry"]
        entry.config(
            state="normal",
            readonlybackground=C["label_playlist_error_bg"],
            background=C["entry_playlist_bg"],
        )
        entry.delete(0, tk.END)

        def _live_index() -> int | None:
            # close_main_frame() renumbers self.frames after deleting a
            # frame; resolve the recording frame's current index at
            # callback time so a mid-recording close can't commit against
            # the wrong playlist (or IndexError when out of range).
            try:
                return self.frames.index(recording_frame)
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
            # the previously registered hotkey is removed and the store
            # matches what the entry now shows - a stale hotkey firing with
            # a blank entry is confusing.
            playlist_name = self.playlist_name_labels[cur_idx].cget("text")
            platform = self.frame_platforms[cur_idx]
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
            displaced = self.kc.register_hotkey(
                playlist_name,
                combo,
                self._make_keybind_callbacks(frame_idx),
                platform=platform,
            )
            if displaced:
                self._clear_displaced_keybind(displaced)
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

        # Prevent a remove click mid-reload from racing the re-import
        # (_on_remove_song checks this flag; the reload button is not
        # disabled - the flag is the guard).
        main_frame = self.frames[frame_idx]
        main_frame._syncing = True

        status_label = self.active_log_labels[frame_idx]["status"]
        status_label.config(text="Sync", background=C["label_playlist_warn_bg"])

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
