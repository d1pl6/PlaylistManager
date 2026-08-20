"""SearchManager - owns the search bar, filtering, and song-search UI.

Extracted from MainWindow to reduce its size.  Manages playlist-name
filtering and per-card song search with debounced async queries.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk

from utils.scaling import px, ui_font
from utils.theme import C, btn_colors, dimmed_fg

logger = logging.getLogger(__name__)


class SearchManager:
    def __init__(
        self,
        root,
        card_grid,
        song_manager,
        *,
        columns,
        close_img,
        show_main_content,
        sync_empty_state,
        update_card_height,
        update_scrollregion,
    ) -> None:
        self.root = root
        self._card_grid = card_grid
        self._song_manager = song_manager
        self._columns = columns
        self._close_img = close_img

        self._show_main_content = show_main_content
        self._sync_empty_state = sync_empty_state
        self._update_card_height = update_card_height
        self._update_scrollregion = update_scrollregion

        self._search_mode: str | None = None
        self._search_frame: tk.Frame | None = None
        self._search_entry: tk.Entry | None = None
        self._search_var: tk.StringVar | None = None
        self._search_results: dict[int, tk.Frame] = {}
        self._song_search_after_id: str | None = None
        self._song_search_token: int = 0

    def update_columns(self, columns: int) -> None:
        self._columns = columns
        if self._search_frame is not None and self._search_frame.winfo_ismapped():
            self._search_frame.grid_configure(columnspan=columns)

    def on_card_closed(self, index: int) -> None:
        """Renumber search results after a card is removed from the grid."""
        closing = self._search_results.pop(index, None)
        if closing is not None:
            try:
                closing.destroy()
            except tk.TclError:
                pass
        renum = {}
        for old_idx, rf in list(self._search_results.items()):
            if old_idx > index:
                renum[old_idx - 1] = self._search_results.pop(old_idx)
        self._search_results.update(renum)

    # ------------------------------------------------------------------
    # Toggle
    # ------------------------------------------------------------------

    def toggle_playlist_search(self, event=None) -> str:
        if self._search_mode == "playlist":
            self.dismiss()
        else:
            self._show_search_bar("playlist")
        return "break"

    def toggle_song_search(self, event=None) -> str:
        if self._search_mode == "song":
            self.dismiss()
        else:
            self._show_search_bar("song")
        return "break"

    # ------------------------------------------------------------------
    # Bar creation
    # ------------------------------------------------------------------

    def _show_search_bar(self, mode: str) -> None:
        if self._search_frame is not None:
            self.dismiss()

        self._search_mode = mode
        search_bg = C["search_bar_bg"]
        search_fg = C["search_bar_fg"]

        self._search_frame = tk.Frame(self.root, background=search_bg, pady=3, padx=6)
        self._search_frame.grid(
            row=1, column=0, columnspan=self._columns, sticky="ew"
        )

        placeholder = "Search playlists..." if mode == "playlist" else "Search songs..."
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_search_query)

        self._search_entry = tk.Entry(
            self._search_frame,
            textvariable=self._search_var,
            font=ui_font(12),
            background=search_bg,
            foreground=search_fg,
            insertbackground=search_fg,
            highlightthickness=0,
            relief="flat",
        )
        self._search_entry.insert(0, placeholder)
        self._search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self._search_entry.bind("<FocusOut>", self._on_search_focus_out)

        def _on_escape(event=None):
            self.dismiss()
            return "break"

        self._search_entry.bind("<Escape>", _on_escape)
        self._search_entry.pack(side="left", fill="x", expand=True)

        close_btn = tk.Button(
            self._search_frame,
            image=self._close_img,
            cursor="hand2",
            **btn_colors(C["button_playlist_bg"], C["button_playlist_fg"]),
            highlightthickness=0,
            relief="flat",
            command=self.dismiss,
        )
        close_btn.pack(side="right", padx=(6, 0))

        self._search_entry.focus_set()

    # ------------------------------------------------------------------
    # Dismiss
    # ------------------------------------------------------------------

    def dismiss(self) -> None:
        self._cancel_song_search_debounce()
        if self._search_frame is not None:
            try:
                self._search_frame.grid_forget()
                self._search_frame.destroy()
            except tk.TclError:
                pass
            self._search_frame = None
            self._search_entry = None
            self._search_var = None
        self._search_mode = None
        self._dismiss_all_search_results()
        self._show_main_content()
        self._sync_empty_state()
        self._update_scrollregion()

    # ------------------------------------------------------------------
    # Focus
    # ------------------------------------------------------------------

    def _on_search_focus_in(self, event=None) -> None:
        if self._search_entry is None:
            return
        text = self._search_entry.get()
        if text in ("Search playlists...", "Search songs..."):
            self._search_entry.delete(0, tk.END)
            self._search_entry.configure(foreground=C["search_bar_fg"])

    def _on_search_focus_out(self, event=None) -> None:
        if self._search_entry is None:
            return
        if not self._search_entry.get().strip():
            placeholder = "Search playlists..." if self._search_mode == "playlist" else "Search songs..."
            self._search_entry.delete(0, tk.END)
            self._search_entry.insert(0, placeholder)
            self._search_entry.configure(foreground=dimmed_fg(C["search_bar_fg"], C["search_bar_bg"]))

    # ------------------------------------------------------------------
    # Query dispatch
    # ------------------------------------------------------------------

    def _on_search_query(self, *args) -> None:
        if self._search_var is None or self._search_mode is None:
            return
        query = self._search_var.get()
        if query in ("Search playlists...", "Search songs..."):
            return
        if self._search_mode == "playlist":
            self._filter_playlists(query)
        elif self._search_mode == "song":
            self._cancel_song_search_debounce()
            self._song_search_after_id = self.root.after(
                200, self._filter_songs, query
            )

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _filter_playlists(self, query: str) -> None:
        q = query.strip().lower()
        for i, card in enumerate(self._card_grid.cards):
            try:
                name = card.name_label.cget("text").lower()
            except (IndexError, tk.TclError):
                continue
            if not q or q in name:
                try:
                    self._card_grid._restore_frame_grid(card.frame, i)
                except tk.TclError:
                    pass
            else:
                card.frame.grid_forget()
        self._sync_empty_state()
        self._update_scrollregion()

    def _filter_songs(self, query: str) -> None:
        q = query.strip()
        if not q:
            self._dismiss_all_search_results()
            return

        self._song_search_token += 1
        token = self._song_search_token

        sm = self._song_manager
        cards: list[tuple[int, str, str, str]] = []
        for i, card in enumerate(self._card_grid.cards):
            try:
                name = card.name_label.cget("text")
                platform = card.platform
            except (IndexError, tk.TclError):
                continue
            playlist_id = card.playlist_id
            cards.append((i, name, platform, playlist_id))

        def _search_worker() -> None:
            results: dict[int, list] = {}
            for i, name, platform, playlist_id in cards:
                results[i] = sm.search_songs(
                    name, q, platform=platform, playlist_id=playlist_id
                )
            try:
                self.root.after(
                    0, self._apply_song_search_results, q, token, results
                )
            except Exception:
                logger.debug("App shutting down; dropped song search results", exc_info=True)

        threading.Thread(target=_search_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Song search debounce
    # ------------------------------------------------------------------

    def _cancel_song_search_debounce(self) -> None:
        if self._song_search_after_id is not None:
            try:
                self.root.after_cancel(self._song_search_after_id)
            except (tk.TclError, ValueError):
                pass
            self._song_search_after_id = None

    # ------------------------------------------------------------------
    # Apply results
    # ------------------------------------------------------------------

    def _apply_song_search_results(
        self, query: str, token: int, results: dict[int, list]
    ) -> None:
        if token != self._song_search_token:
            return
        for i, card in enumerate(self._card_grid.cards):
            matches = results.get(i)
            old = self._search_results.pop(i, None)
            if old is not None:
                try:
                    old.grid_forget()
                    old.destroy()
                except tk.TclError:
                    pass
            if matches is None:
                continue
            results_frame = self._build_search_results_frame(card.frame, matches)
            self._search_results[i] = results_frame
            results_frame.grid(row=3, column=0, sticky="nsew", padx=2)
            self._update_card_height(i)
        self._update_scrollregion()

    def _build_search_results_frame(self, main_frame: tk.Frame, songs: list) -> tk.Frame:
        frame_playlist_bg = C["frame_playlist_bg"]
        result_bg = C["search_result_bg"]
        result_fg = C["search_result_fg"]

        frame = tk.Frame(main_frame, background=frame_playlist_bg)

        if not songs:
            no_match = tk.Label(
                frame,
                text="No matches",
                font=ui_font(10),
                background=frame_playlist_bg,
                foreground=dimmed_fg(result_fg, frame_playlist_bg),
                anchor="center",
            )
            no_match.pack(fill="x", pady=2)
            return frame

        for song in songs:
            row_frame = tk.Frame(frame, background=result_bg)
            row_frame.pack(fill="x", pady=1, padx=2)

            title = song.get("title", "")
            artists = song.get("artists", [])
            artists_str = ", ".join(artists[:2]) if isinstance(artists, list) else str(artists)
            label_text = f"{title} - {artists_str}" if artists_str else title

            lbl = tk.Label(
                row_frame,
                text=label_text,
                font=ui_font(10),
                background=result_bg,
                foreground=result_fg,
                anchor="w",
            )
            lbl.pack(fill="x", padx=4, pady=1)

        return frame

    def _dismiss_all_search_results(self) -> None:
        for idx, rf in self._search_results.items():
            try:
                rf.grid_forget()
                rf.destroy()
            except tk.TclError:
                pass
            try:
                self._update_card_height(idx)
            except (IndexError, tk.TclError):
                pass
        self._search_results.clear()

    # ------------------------------------------------------------------
    # Height query (called by CardGridManager during layout)
    # ------------------------------------------------------------------

    def get_search_results_height(self, frame_idx: int) -> int | None:
        search_results = self._search_results.get(frame_idx)
        if search_results is not None:
            try:
                return search_results.winfo_reqheight()
            except tk.TclError:
                pass
        return None

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def apply_theme(self) -> None:
        if self._search_frame is not None:
            self._search_frame.configure(background=C["search_bar_bg"])
        if self._search_entry is not None:
            self._search_entry.configure(
                background=C["search_bar_bg"],
                foreground=C["search_bar_fg"],
                insertbackground=C["search_bar_fg"],
            )
        for idx, rf in self._search_results.items():
            try:
                rf.configure(background=C["frame_playlist_bg"])
                for child in rf.winfo_children():
                    if isinstance(child, tk.Frame):
                        child.configure(background=C["search_result_bg"])
                        for lbl in child.winfo_children():
                            if isinstance(lbl, tk.Label):
                                lbl.configure(
                                    background=C["search_result_bg"],
                                    foreground=C["search_result_fg"],
                                )
                    elif isinstance(child, tk.Label):
                        child.configure(
                            background=C["frame_playlist_bg"],
                        )
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def search_mode(self) -> str | None:
        return self._search_mode
