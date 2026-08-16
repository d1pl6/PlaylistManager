"""
Playlist selection dialog (scrollable list of playlists with covers).

Thumbnails are downloaded in background threads and applied on the main
thread via ``parent.after()`` - tkinter must never be touched from a
worker thread (and blocking network calls must never run on the UI
thread).
"""

import logging
import threading
import tkinter as tk
from queue import Empty, Queue
from tkinter import ttk

from utils.scaling import px, ui_font
from utils.theme import C, btn_colors
from utils.thumbnail import ThumbnailService

logger = logging.getLogger(__name__)

#: Bounded thumbnail fetch concurrency (the queue holds the backlog, so a
#: large library spawns 4 workers instead of one thread per playlist).
_THUMB_WORKERS = 4
#: Queue sentinel telling a worker to exit.
_WORKER_STOP = object()


class PlaylistDialog:
    def __init__(self, parent, on_select, on_cancel=None):
        self.parent = parent
        self.on_select = on_select
        self.on_cancel = on_cancel
        self.choose_frame = None
        self.canvas = None
        self.img_refs = []
        # Bounded thumbnail pipeline: jobs are queued and consumed by a few
        # daemon workers instead of one thread per playlist (a 100+ item
        # library would otherwise pile up 100 threads on the fetch semaphore).
        self._thumb_tasks: "Queue" = Queue()
        for _ in range(_THUMB_WORKERS):
            threading.Thread(target=self._thumb_worker, daemon=True).start()

    def show(self, playlists):
        dialog_bg = C["frame_main_bg"]
        label_fg = C["label_def_fg"]
        btn_bg = C["button_playlist_bg"]
        btn_fg = C["button_playlist_fg"]
        btn_btn = btn_colors(btn_bg, btn_fg)
        close_bg = C["button_head_bg"]
        close_fg = C["button_head_fg"]
        close_btn = btn_colors(close_bg, close_fg)

        self.choose_frame = tk.Frame(self.parent, background=dialog_bg)
        self.choose_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        try:
            self.parent.grid_rowconfigure(1, weight=1)
        except tk.TclError:
            pass

        title_frame = tk.Frame(self.choose_frame, background=dialog_bg)
        title_frame.pack(pady=5)

        tk.Label(
            title_frame,
            text="Select a Playlist below",
            background=dialog_bg,
            foreground=label_fg,
            font=ui_font(12),
        ).pack(side="left", anchor="w")

        tk.Button(
            title_frame,
            text="Close",
            cursor="hand2",
            **close_btn,
            font=ui_font(12),
            highlightthickness=0,
            relief="raised",
            command=self.cancel,
        ).pack(side="right", anchor="e")

        self.canvas = tk.Canvas(
            self.choose_frame, background=dialog_bg, highlightthickness=0
        )
        scrollbar = ttk.Scrollbar(
            self.choose_frame, orient="vertical", command=self.canvas.yview
        )
        scrollable_frame = tk.Frame(
            self.canvas,
            background=dialog_bg,
            border=1,
            relief="solid",
        )

        canvas = self.canvas
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )

        self._canvas_window = self.canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        for playlist in playlists:
            playlist_id = playlist.get("playlistId", "")
            thumb_url = ThumbnailService.from_data(playlist)
            playlist_name = playlist.get("title", "Unknown Playlist")

            btn = tk.Button(
                scrollable_frame,
                image="",
                cursor="hand2",
                **btn_btn,
                width=px(40),
                highlightthickness=0,
                relief="raised",
                command=lambda name=playlist_name, pid=playlist_id, tu=thumb_url: self._on_playlist_click(name, pid, tu),
            )
            btn.pack(pady=5, padx=5)

            tk.Button(
                scrollable_frame,
                text=playlist_name,
                cursor="hand2",
                **btn_btn,
                font=ui_font(11),
                width=40,
                highlightthickness=0,
                relief="raised",
                command=lambda name=playlist_name, pid=playlist_id, tu=thumb_url: self._on_playlist_click(name, pid, tu),
            ).pack(pady=5)

            if thumb_url:
                self._load_thumb_async(btn, thumb_url)

        self.canvas.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        scrollbar.pack(side="right", fill="y")

        scrollable_frame.bind("<MouseWheel>", self._on_mouse_wheel)
        scrollable_frame.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        scrollable_frame.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))
        for child in scrollable_frame.winfo_children():
            child.bind("<MouseWheel>", self._on_mouse_wheel)
            child.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
            child.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _load_thumb_async(self, button: tk.Button, thumb_url: str) -> None:
        """Queue a cover download; the PhotoImage is applied on the main thread."""
        self._thumb_tasks.put((button, thumb_url))

    def _thumb_worker(self) -> None:
        """Consume queued cover jobs (daemon).  Network + PIL run here; the
        resulting image is handed to the main thread via a guarded after()."""
        while True:
            item = self._thumb_tasks.get()
            if item is _WORKER_STOP:
                self._thumb_tasks.task_done()
                return
            button, thumb_url = item
            try:
                img = ThumbnailService.fetch_image(
                    thumb_url, size=(px(40), px(40))
                )
            except Exception as e:
                logger.debug("Thumbnail download failed: %s", e)
                img = None
            if img is not None:
                try:
                    self.parent.after(
                        0,
                        lambda b=button, im=img: self._apply_thumb(b, im),
                    )
                except Exception:
                    logger.debug("Dialog closed during thumbnail download", exc_info=True)
            self._thumb_tasks.task_done()

    def _apply_thumb(self, button: tk.Button, img) -> None:
        try:
            if not button.winfo_exists():
                # Dialog was closed before the download finished.
                return
            photo = ThumbnailService.to_photoimage(img)
        except Exception as e:
            logger.error(f"Failed to create dialog thumbnail: {e}")
            return
        try:
            button.configure(image=photo)
            self.img_refs.append(photo)
        except tk.TclError as e:
            # The dialog can be destroyed between the winfo_exists() check
            # and configure() - swallow the race, not a real failure.
            logger.debug("Dialog closed before thumbnail could be applied: %s", e)

    def _on_playlist_click(self, playlist_name, playlist_id, thumb_url=None):
        self.close()
        self.on_select(playlist_name, playlist_id, thumb_url)

    def cancel(self):
        if self.on_cancel:
            self.on_cancel()
        self.close()

    def close(self):
        try:
            while True:
                self._thumb_tasks.get_nowait()
                self._thumb_tasks.task_done()
        except Empty:
            pass
        for _ in range(_THUMB_WORKERS):
            self._thumb_tasks.put(_WORKER_STOP)
        try:
            self.parent.grid_rowconfigure(1, weight=0)
        except tk.TclError:
            pass
        if self.choose_frame:
            try:
                self.choose_frame.destroy()
            except Exception:
                logger.debug("Failed to destroy choose_frame", exc_info=True)
            self.choose_frame = None
        self.img_refs.clear()

    def _on_canvas_configure(self, event):
        if self._canvas_window:
            self.canvas.itemconfig(self._canvas_window, width=event.width)

    def _on_mouse_wheel(self, event):
        if self.canvas:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
