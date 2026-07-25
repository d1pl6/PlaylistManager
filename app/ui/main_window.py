import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import logging
import threading

from ui.playlist_dialog import PlaylistDialog
from ui.login_ui import show_login_dialog
from ui.settings_ui import show_settings_dialog
from services.song_manager import SongManager
from services.database import DatabaseManager

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
        playlist_service,
        playlist_store,
        keybind_controller,
        app_controller,
    ):
        self.root = root
        self.integrations = integrations
        self.ps = playlist_service
        self.store = playlist_store
        self.kc = keybind_controller
        self.ac = app_controller

        self.frames = []
        self.frame_positions = []
        self.playlist_name_labels = []
        self.frame_platforms = []
        self.active_log_labels = {}
        self.img_refs = []
        self.frame_img_refs = {}
        self._choose_open = False
        self._recording_frame_idx = None

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

        self.header_frame = tk.Frame(self.root, background="#181818")
        self.header_frame.bind("<B1-Motion>", self.on_drag)

        self._create_widgets()
        self.header_frame.bind("<Button-1>", self.start_drag)
        self.root.bind("<Button-1>", self._on_root_click, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.ac.quit_app)

    def _create_widgets(self):
        login_img_path = assets_dir / "login.png"
        self.login_img = tk.PhotoImage(file=str(login_img_path))
        self.btn_login = tk.Button(
            self.header_frame,
            image=self.login_img,
            cursor="hand2",
            background="#9A9A9A",
            activebackground="#868686",
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
            background="#9A9A9A",
            activebackground="#868686",
            command=self._open_playlist_dialog,
        )

        self.close_btn = tk.Button(
            self.header_frame,
            text="✕",
            command=self.ac.quit_app,
            background="#0A0000",
            activebackground="#320000",
            activeforeground="#ff0000",
            fg="white",
            bd=0,
        )

        open_settings_img_path = assets_dir / "settings.png"
        self.open_settings_img = tk.PhotoImage(file=str(open_settings_img_path))
        self.btn_open_settings = tk.Button(
            self.header_frame,
            image=self.open_settings_img,
            cursor="hand2",
            background="#9A9A9A",
            activebackground="#868686",
            command=lambda: show_settings_dialog(self.root, keybind_controller=self.kc),
        )

        self.header_frame.grid(row=0, column=0, columnspan=2, sticky="nsew")

        self.header_frame.grid_columnconfigure(0, weight=1)
        self.header_frame.grid_columnconfigure(1, weight=0)
        self.header_frame.grid_columnconfigure(2, weight=1)

        self.btn_login.grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.btn_add_playlist.grid(row=0, column=1, padx=4, pady=4)
        self.close_btn.grid(row=0, column=2, sticky="e")
        self.btn_open_settings.grid(row=0, column=3, sticky="e", padx=4, pady=4)

    def _open_playlist_dialog(self):
        if self._choose_open:
            return

        active = self.integrations.get_active()
        if not active:
            self._show_integration_error()
            return

        if len(active) == 1:
            platform = next(iter(active))
            self._fetch_and_show_playlists(active[platform])
        else:
            self._choose_platform(active)

    def _choose_platform(self, active_integrations):
        platforms = list(active_integrations.values())
        win = tk.Toplevel(self.root)
        win.title("Choose Platform")
        win.configure(background="#2A2A2A")
        win.transient(self.root)
        win.grab_set()

        tk.Label(
            win,
            text="Select platform to fetch playlists from:",
            background="#2A2A2A",
            foreground="white",
            font="Noto, 11",
        ).pack(pady=10, padx=20)

        def pick(integration):
            win.destroy()
            self._fetch_and_show_playlists(integration)

        for integration in platforms:
            tk.Button(
                win,
                text=integration.display_name,
                background="#404040",
                foreground="white",
                font="Noto, 11",
                width=30,
                command=lambda i=integration: pick(i),
            ).pack(pady=4, padx=20)

        tk.Button(
            win,
            text="Cancel",
            background="#0A0000",
            foreground="white",
            font="Noto, 10",
            command=win.destroy,
        ).pack(pady=10)

    def _fetch_and_show_playlists(self, integration):
        self._choose_open = True
        self.btn_add_playlist.configure(state="disabled", image=self.loading_img)
        self._hide_main_content()

        playlists = integration.get_library_playlists()
        if not playlists:
            self._on_dialog_cancel()
            self._show_integration_error()
            return

        existing_names = self.store.get_existing_names(platform=integration.id)
        available = [p for p in playlists if p.get("title") not in existing_names]

        dialog = PlaylistDialog(
            self.root,
            lambda name, pid, thumb_url: self._on_playlist_selected(name, integration.id, pid, thumb_url),
            on_cancel=self._on_dialog_cancel,
        )
        dialog.show(available, integration)

    def _show_integration_error(self):
        messagebox.showerror(
            "Integration Error",
            INTEGRATION_ERROR_MSG,
        )

    def _on_dialog_cancel(self):
        self._choose_open = False
        self.btn_add_playlist.configure(state="normal", image=self.add_playlist_img)
        self._show_main_content()

    def _on_playlist_selected(
        self, playlist_name, platform="youtube_music", playlist_id="", thumb_url=None
    ):
        self.store.add_playlist(
            playlist_name,
            platform=platform,
            playlist_id=playlist_id,
            thumbnail_url=thumb_url or "",
        )
        self._choose_open = False
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

            self._import_playlist_tracks(
                playlist_name, platform, playlist_id, frame_idx
            )

    def _set_playlist_cover(self, frame_idx, thumb_url):
        if frame_idx not in self.active_log_labels:
            return
        cover_label = self.active_log_labels[frame_idx].get("cover")
        if not cover_label:
            return
        integration = self.integrations.get(self.frame_platforms[frame_idx])
        if not integration:
            return

        def fetch():
            try:
                tk_img = integration.fetch_thumbnail(thumb_url, size=(64, 64))
                if tk_img:
                    self.root.after(0, lambda: self._apply_cover(frame_idx, tk_img))
            except Exception as e:
                logger.error(f"Failed to set playlist cover: {e}")

        threading.Thread(target=fetch, daemon=True).start()

    def _apply_cover(self, frame_idx, tk_img):
        if frame_idx not in self.active_log_labels:
            return
        cover_label = self.active_log_labels[frame_idx].get("cover")
        if not cover_label:
            return
        cover_label.configure(image=tk_img)
        self.frame_img_refs.setdefault(id(cover_label), []).append(tk_img)

    def _update_log_labels_from_db(self, frame_idx, playlist_name):
        sm = SongManager()
        latest = sm.get_latest_song(playlist_name)
        if not latest:
            return
        labels = self.active_log_labels.get(frame_idx)
        if not labels:
            return
        artists = latest.get("artists", [])
        artists_str = ", ".join(artists[:2]) if isinstance(artists, list) else str(artists)
        labels["artist"].config(text=artists_str[:8])
        labels["name"].config(text=latest.get("title", "")[:18])

    def _import_playlist_tracks(self, playlist_name, platform, playlist_id, frame_idx):
        if not playlist_id:
            logger.warning(f"No playlist_id for '{playlist_name}', skipping import")
            return

        integration = self.integrations.get(platform)
        if integration is None:
            return

        def run_import():
            try:
                tracks = integration.get_playlist_tracks(playlist_id)
                if not tracks:
                    self.root.after(0, self._on_import_done, frame_idx, 0, "No tracks")
                    return

                sm = SongManager()
                if platform == "spotify":
                    inserted = sm.add_songs_bulk_spotify(playlist_name, tracks)
                else:
                    inserted = sm.add_songs_bulk(playlist_name, tracks)
                self.root.after(
                    0,
                    self._on_import_done,
                    frame_idx,
                    inserted,
                    f"{inserted} new",
                )
            except Exception as e:
                logger.error(f"Import failed for '{playlist_name}': {e}")
                self.root.after(0, self._on_import_done, frame_idx, 0, "Error")

        threading.Thread(target=run_import, daemon=True).start()

    def _on_import_done(self, frame_idx, count, status_text):
        if frame_idx >= len(self.active_log_labels):
            return
        status_label = self.active_log_labels[frame_idx]["status"]
        if count > 0:
            status_label.config(text="OK", background="#006713")
        elif status_text == "Error":
            status_label.config(text=status_text, background="#A00000")
        else:
            status_label.config(text=status_text, background="#006713")
        playlist_name = self.playlist_name_labels[frame_idx].cget("text")
        self._update_log_labels_from_db(frame_idx, playlist_name)
        logger.info(f"Import finished for frame {frame_idx}: {status_text}")

    def _on_reload_done(self, frame_idx, count, status_text, thumb_url):
        self._on_import_done(frame_idx, count, status_text)
        if thumb_url:
            self._set_playlist_cover(frame_idx, thumb_url)

    def setup(self):
        self.kc.set_root(self.root)
        playlists = self.store.load_playlists()
        if playlists:
            self.create_main_frame(len(playlists))
            for i, playlist in enumerate(playlists):
                if i < len(self.playlist_name_labels):
                    name = playlist.get("name", f"Playlist {i+1}")
                    platform = playlist.get("platform", "youtube_music")
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
                            self.active_log_labels[i],
                            platform=platform,
                        )

                    self._update_log_labels_from_db(i, name)

                    thumb_url = playlist.get("thumbnail_url", "")
                    if thumb_url:
                        self._set_playlist_cover(i, thumb_url)

    def start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def on_drag(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def create_main_frame(self, num):
        start_index = len(self.frames)
        for i in range(start_index, start_index + num):
            col = i % 2
            row = (i // 2) + 1

            main_frame = tk.Frame(self.root, width=320)
            main_header_frame = tk.Frame(main_frame, background="#404040")
            main_log_frame = tk.Frame(main_frame, background="#404040")

            playlist_cover = tk.Label(
                main_header_frame,
                image=self.playlist_cover_img,
                background="#404040",
            )
            playlist_name = tk.Label(
                main_header_frame,
                text=f"row:{row} col:{col}",
                font="Noto, 12",
                background="#404040",
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
                background="#404040",
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
                command=lambda idx=len(self.frames): self._reload_database(idx),
            )

            log_artist = tk.Label(
                main_log_frame,
                text="log_artist placeholder",
                font="Noto, 12",
                background="#404040",
                width=8,
                anchor="w",
            )
            log_helper_1 = tk.Label(
                main_log_frame,
                text="-",
                font="Noto, 12",
                background="#404040",
                anchor="w",
            )
            log_name = tk.Label(
                main_log_frame,
                text="log_name placeholder",
                font="Noto, 12",
                background="#404040",
                width=18,
                anchor="w",
            )
            log_helper_2 = tk.Label(
                main_log_frame,
                text="|",
                font="Noto, 12",
                background="#404040",
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
            self.frame_platforms.append("youtube_music")

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

    def _hide_main_content(self):
        for frame in self.frames:
            frame.grid_forget()

    def _show_main_content(self):
        for frame, (row, col) in zip(self.frames, self.frame_positions):
            frame.grid(row=row, column=col)

    def close_main_frame(self, frame):
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

            self.store.delete_playlist(playlist_name, platform=platform)

            frame.grid_forget()
            frame.destroy()
            self._reorder_frames()
            logger.debug(f"Closed frame at index {index}")
        except (ValueError, IndexError) as e:
            logger.error(f"Error closing frame: {e}")

    def _reorder_frames(self):
        self.frame_positions.clear()
        for i, frame in enumerate(self.frames):
            col = i % 2
            row = (i // 2) + 1
            self.frame_positions.append((row, col))
            frame.grid(row=row, column=col)
        logger.debug("Reordered frames after deletion")

    def _start_recording(self, frame_idx):
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

        def on_combo(combo):
            entry.config(state="normal")
            entry.delete(0, tk.END)
            entry.insert(0, combo)

        self.kc.start_recording(on_combo)
        return "break"

    def _stop_recording(self, frame_idx):
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
            self.store.update_keybind(playlist_name, platform, combo)
            self.kc.register_hotkey(
                playlist_name,
                combo,
                self.active_log_labels[frame_idx],
                platform=platform,
            )
        else:
            self.store.update_keybind(playlist_name, platform, "")
            self.kc.unregister_hotkey(playlist_name)

    def _on_root_click(self, event):
        if self._recording_frame_idx is not None:
            entry = self.active_log_labels[self._recording_frame_idx]["keybind_entry"]
            if event.widget != entry:
                self.root.after(1, self._stop_recording, self._recording_frame_idx)

    def _reload_database(self, frame_idx):
        if frame_idx >= len(self.playlist_name_labels):
            return
        playlist_name = self.playlist_name_labels[frame_idx].cget("text")
        platform = self.frame_platforms[frame_idx]
        playlist_data = self.store.find_playlist(playlist_name, platform)
        playlist_id = playlist_data.get("playlist_id", "") if playlist_data else ""

        if not playlist_id:
            logger.warning(f"No playlist_id for '{playlist_name}', cannot reload")
            return

        status_label = self.active_log_labels[frame_idx]["status"]
        status_label.config(text="Sync", background="#5A4A00")

        def run_reload():
            try:
                db_path = DatabaseManager.get_playlist_db_path_static(playlist_name)
                if db_path.exists():
                    db_path.unlink()
                    logger.info(f"Deleted database for '{playlist_name}'")

                integration = self.integrations.get(platform)
                if not integration:
                    self.root.after(0, self._on_import_done, frame_idx, 0, "Error")
                    return

                details = integration.get_playlist_details(playlist_id)
                thumbnails = details.get("thumbnails") or details.get("thumbnail")
                thumb_url = None
                if isinstance(thumbnails, list):
                    thumb_url = integration.get_smallest_thumbnail(thumbnails)
                elif isinstance(thumbnails, str):
                    thumb_url = thumbnails

                tracks = integration.get_playlist_tracks(playlist_id)
                if not tracks:
                    self.root.after(0, self._on_import_done, frame_idx, 0, "No tracks")
                    return

                sm = SongManager()
                if platform == "spotify":
                    inserted = sm.add_songs_bulk_spotify(playlist_name, tracks)
                else:
                    inserted = sm.add_songs_bulk(playlist_name, tracks)

                if thumb_url:
                    self.store.update_thumbnail(playlist_name, platform, thumb_url)
                self.root.after(
                    0, self._on_reload_done, frame_idx, inserted, f"{inserted} new", thumb_url
                )
            except Exception as e:
                logger.error(f"Reload failed for '{playlist_name}': {e}")
                self.root.after(0, self._on_import_done, frame_idx, 0, "Error")

        threading.Thread(target=run_reload, daemon=True).start()

    def cleanup(self):
        self.img_refs.clear()
        self.frame_img_refs.clear()
        self.active_log_labels.clear()
        for frame in self.frames:
            try:
                frame.grid_forget()
                frame.destroy()
            except Exception as e:
                logger.warning(f"Error destroying frame: {e}")
        self.frames.clear()
        self.frame_positions.clear()
        self.playlist_name_labels.clear()
        self.frame_platforms.clear()
