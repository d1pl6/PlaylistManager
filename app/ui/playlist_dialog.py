import logging
import tkinter as tk
from tkinter import ttk

from services.thumbnail import ThumbnailService

logger = logging.getLogger(__name__)


class PlaylistDialog:
    def __init__(self, parent, on_select, on_cancel=None):
        self.parent = parent
        self.on_select = on_select
        self.on_cancel = on_cancel
        self.choose_frame = None
        self.canvas = None
        self.img_refs = []

    def show(self, playlists, integration):
        self.choose_frame = tk.Frame(self.parent, background="#2A2A2A")
        self.choose_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")

        title_frame = tk.Frame(self.choose_frame, background="#2A2A2A")
        title_frame.pack(pady=5)

        tk.Label(
            title_frame,
            text="Select a Playlist below",
            background="#2A2A2A",
            foreground="white",
            font="Noto, 12",
        ).pack(side="left", anchor="w")

        tk.Button(
            title_frame,
            text="Close",
            background="#2A2A2A",
            foreground="white",
            font="Noto, 12",
            command=self.cancel,
        ).pack(side="right", anchor="e")

        self.canvas = tk.Canvas(
            self.choose_frame, background="#2A2A2A", highlightthickness=0
        )
        scrollbar = ttk.Scrollbar(
            self.choose_frame, orient="vertical", command=self.canvas.yview
        )
        scrollable_frame = tk.Frame(
            self.canvas,
            background="#2A2A2A",
            cursor="hand2",
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

        self._scrollable_frame = scrollable_frame

        for playlist in playlists:
            playlist_id = playlist.get("playlistId", "")
            thumb_url = playlist.get("thumbnail")
            if not thumb_url:
                try:
                    details = integration.get_playlist_details(playlist_id, limit=1)
                    thumbnails = details.get("thumbnails") or details.get("thumbnail")
                    if isinstance(thumbnails, list):
                        thumb_url = ThumbnailService.get_smallest_thumbnail(thumbnails)
                    elif isinstance(thumbnails, str):
                        thumb_url = thumbnails
                except Exception as e:
                    logger.error(f"Failed to fetch playlist details: {e}")

            tk_image = None
            if thumb_url:
                try:
                    tk_image = ThumbnailService.fetch_thumbnail(thumb_url, size=(40, 40))
                    if tk_image:
                        self.img_refs.append(tk_image)
                except Exception as e:
                    logger.error(f"Failed to fetch thumbnail: {e}")

            playlist_name = playlist.get("title", "Unknown Playlist")
            btn = tk.Button(
                scrollable_frame,
                image=tk_image or "",
                background="#404040",
                foreground="white",
                width=40,
                command=lambda name=playlist_name, pid=playlist_id, tu=thumb_url: self._on_playlist_click(name, pid, tu),
            )
            btn.pack(pady=5, padx=5)
            tk.Button(
                scrollable_frame,
                text=playlist_name,
                background="#404040",
                foreground="white",
                font="Noto, 11",
                width=40,
                command=lambda name=playlist_name, pid=playlist_id, tu=thumb_url: self._on_playlist_click(name, pid, tu),
            ).pack(pady=5)

        self.canvas.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        scrollbar.pack(side="right", fill="y")

        scrollable_frame.bind("<MouseWheel>", self._on_mouse_wheel)
        scrollable_frame.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        scrollable_frame.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))
        for child in scrollable_frame.winfo_children():
            child.bind("<MouseWheel>", self._on_mouse_wheel)
            child.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
            child.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _on_playlist_click(self, playlist_name, playlist_id, thumb_url=None):
        self.close()
        self.on_select(playlist_name, playlist_id, thumb_url)

    def cancel(self):
        if self.on_cancel:
            self.on_cancel()
        self.close()

    def close(self):
        if self.choose_frame:
            try:
                self.choose_frame.destroy()
            except Exception as e:
                logger.error(f"Failed to destroy choose_frame: {e}")
        self.img_refs.clear()

    def _on_canvas_configure(self, event):
        if self._canvas_window:
            self.canvas.itemconfig(self._canvas_window, width=event.width)

    def _on_mouse_wheel(self, event):
        if self.canvas:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
