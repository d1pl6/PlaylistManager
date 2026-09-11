"""CardGridManager - owns playlist card creation, layout, and lifecycle.

Extracted from MainWindow to reduce its size.  Manages the card widgets
inside the scrollable content frame, including creation, deletion,
layout, empty-state, height calculation, and card-specific theming.
"""

from __future__ import annotations

import logging
import tkinter as tk
import webbrowser
from typing import TYPE_CHECKING, Callable

from controllers.keybind_registry import KeybindCallbacks
from services.database import DatabaseManager
from services.playlist_store import PlaylistStore
from services.playlist_url import build_playlist_url
from services import duplicate_queue, scrobble_log
from ui.card import PlaylistCard
from ui.close_playlist_dialog import show_close_playlist_dialog
from ui.tooltip import ToolTip
from utils.config import (
    REMOVE_PLAYLIST_DEFAULT_MODE,
    REMOVE_PLAYLIST_MODE_LABELS,
    get_setting,
    get_setting_value,
)
from utils.scaling import px, ui_font
from utils.theme import C, btn_colors

if TYPE_CHECKING:
    from ui.scrollable import ScrollableFrame

logger = logging.getLogger(__name__)

# Base design sizes of the playlist card, in unscaled pixels; every value
# is multiplied by the UI scale (utils/scaling).  Card *width* is no
# longer fixed — each card fills its grid column (column width is driven
# by the window width and the column count).
CARD_H_BASE = 96
LOG_ROW_H_BASE = 23
STATS_ROW_H_BASE = 20


# Mode choices for removing a playlist card (config.REMOVE_PLAYLIST_*).
def resolve_playlist_removal_mode(confirm: bool, default: str) -> Optional[str]:
    """Resolve the playlist-removal action for a card ✕.

    Returns "remove" (registry entry + local DB deleted), "keep_db"
    (registry entry deleted, per-playlist SQLite file kept), or None when
    confirmation is on -- the caller must then show the close dialog,
    whose choice is one of the two modes or None (cancelled).  Unknown
    ``default`` values fall back to "remove" (today's behaviour).
    """
    if confirm:
        return None
    return default if default in REMOVE_PLAYLIST_MODE_LABELS else REMOVE_PLAYLIST_DEFAULT_MODE


class CardGridManager:
    """Manages the grid of playlist cards inside a scrollable content frame."""

    def __init__(
        self,
        main_area: tk.Frame,
        content_frame: tk.Frame,
        sf: ScrollableFrame,
        root: tk.Tk,
        *,
        columns: int,
        song_manager,
        keybind_controller,
        show_log: bool,
        show_stats: bool,
        showcase_count: int,
        playlist_cover_img,
        close_playlist_img,
        reload_database_img,
        # Pin/unpin (optional - when pin images are None the buttons are off)
        pin_img=None,
        pin_active_img=None,
        show_pin_buttons=True,
        on_pin_toggle: Callable[[int], None] | None = None,
        # Callbacks into MainWindow
        make_keybind_callbacks: Callable[[int], KeybindCallbacks],
        on_reload_requested: Callable[[int], None],
        start_recording: Callable[[int], str],
        auto_resize: Callable[[], None],
        before_card_close: Callable[[int], None],
        after_card_close: Callable[[int], None],
        prune_frame_imgs: Callable[[tk.Frame], None],
        get_search_results_height: Callable[[int], int],
        is_recording: Callable[[int], bool],
        open_playlist_dialog: Callable[[], None],
    ) -> None:
        self.root = root
        self.parent_frame = main_area
        self._content_frame = content_frame
        self._sf = sf
        self._columns = columns
        self._song_manager = song_manager
        self.kc = keybind_controller
        self._show_log = show_log
        self._show_stats = show_stats
        self._showcase_count = showcase_count

        # Image assets (owned by MainWindow, shared here)
        self._playlist_cover_img = playlist_cover_img
        self._close_playlist_img = close_playlist_img
        self._reload_database_img = reload_database_img
        self._pin_img = pin_img
        self._pin_active_img = pin_active_img
        self._show_pin_buttons = show_pin_buttons
        self._on_pin_toggle_cb = on_pin_toggle
        self._pin_buttons: list[tk.Button | None] = []
        self._close_buttons: list[tk.Button] = []

        # Callbacks
        self._make_kc_callbacks = make_keybind_callbacks
        self._on_reload_requested_cb = on_reload_requested
        self._start_recording_cb = start_recording
        self._auto_resize_cb = auto_resize
        self._before_card_close_cb = before_card_close
        self._after_card_close_cb = after_card_close
        self._prune_frame_imgs_cb = prune_frame_imgs
        self._get_search_results_height_cb = get_search_results_height
        self._is_recording_cb = is_recording
        self._open_playlist_dialog_cb = open_playlist_dialog

        self.cards: list[PlaylistCard] = []

        for c in range(self._columns):
            self._content_frame.grid_columnconfigure(c, weight=1)

        self.empty_state_btn = tk.Button(
            self._content_frame,
            text="Click '+' to add a playlist",
            cursor="hand2",
            **btn_colors(C["button_main_bg"], C["button_main_fg"]),
            font=ui_font(12),
            highlightthickness=0,
            relief="raised",
            command=self._open_playlist_dialog_cb,
        )

    @property
    def content_frame(self) -> tk.Frame:
        return self._content_frame

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        for card in self.cards:
            try:
                card.frame.grid_forget()
                card.frame.destroy()
            except Exception as e:
                logger.warning("Error destroying frame: %s", e)
        self.cards.clear()
        self._pin_buttons.clear()
        self._close_buttons.clear()

    # ------------------------------------------------------------------
    # Pin/unpin
    # ------------------------------------------------------------------

    def set_card_pin_state(self, frame_idx: int, pinned: bool) -> None:
        """Swap the card's pin button to the active/inactive icon."""
        try:
            btn = self._pin_buttons[frame_idx]
        except IndexError:
            return
        if btn is None:
            return
        try:
            btn.config(image=self._pin_active_img if pinned else self._pin_img)
        except tk.TclError:
            return
        btn._pm_pinned = pinned  # read by the tooltip lambda

    def reset_state(self) -> None:
        """Drop all grid bookkeeping (cards + per-card button lists).

        Called before an in-place rebuild (sort/pin changes) — the
        parallel lists must reset together or stale destroyed widgets
        linger and later visibility calls hit bad window paths.
        """
        self.cards.clear()
        self._pin_buttons.clear()
        self._close_buttons.clear()

    def _apply_pin_visibility(self, enabled: bool) -> None:
        """Show/hide every pin button without touching the stored flags."""
        self._show_pin_buttons = enabled
        for i, btn in enumerate(self._pin_buttons):
            if btn is None:
                continue
            try:
                if enabled:
                    btn.pack(side="left", before=self._close_buttons[i])
                else:
                    btn.pack_forget()
            except (tk.TclError, IndexError):
                continue

    # ------------------------------------------------------------------
    # Card index helper
    # ------------------------------------------------------------------

    def _card_index(self, card) -> int | None:
        try:
            return self.cards.index(card)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Card creation
    # ------------------------------------------------------------------

    def create_main_frame(self, num: int) -> None:
        start_card_idx = len(self.cards)
        for j in range(num):
            i = start_card_idx + j
            col = i % self._columns
            row = (i // self._columns) + 1

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
            button_playlist_btn = btn_colors(button_playlist_bg, button_playlist_fg)
            entry_playlist_bg = C["entry_playlist_bg"]
            entry_playlist_fg = C["entry_playlist_fg"]
            entry_playlist_ro_bg = C["entry_playlist_ro_bg"]

            main_frame = tk.Frame(
                self._content_frame,
                height=px(CARD_H_BASE),
                background=frame_playlist_bg,
                borderwidth=2,
                relief="solid",
            )
            main_frame.grid_propagate(False)
            main_frame.grid_rowconfigure(0, weight=1)
            main_frame.grid_rowconfigure(1, weight=0)
            main_frame.grid_rowconfigure(2, weight=1)
            main_frame.grid_rowconfigure(3, weight=0)
            main_frame.grid_columnconfigure(0, weight=1)
            main_header_frame = tk.Frame(main_frame, background=frame_playlist_bg)
            main_stats_frame = tk.Frame(main_frame, background=frame_playlist_bg)
            main_log_frame = tk.Frame(main_frame, background=frame_playlist_bg)

            stats_bg = C["label_playlist_stats_bg"]
            stats_fg = C["label_playlist_stats_fg"]
            stats_songs = tk.Label(
                main_stats_frame,
                text="",
                font=ui_font(10),
                background=stats_bg,
                foreground=stats_fg,
                anchor="w",
            )
            stats_duration = tk.Label(
                main_stats_frame,
                text="",
                font=ui_font(10),
                background=stats_bg,
                foreground=stats_fg,
                anchor="center",
            )
            stats_followers = tk.Label(
                main_stats_frame,
                text="",
                font=ui_font(10),
                background=stats_bg,
                foreground=stats_fg,
                anchor="e",
            )

            playlist_cover = tk.Label(
                main_header_frame,
                image=self._playlist_cover_img,
                background=label_playlist_bg,
            )
            playlist_name = tk.Label(
                main_header_frame,
                text="",
                font=ui_font(12),
                background=label_playlist_name_bg,
                foreground=label_playlist_name_fg,
            )

            # Header action cluster (pin | close) - the buttons are real
            # children of this frame, not `pack(in_=...)` guests from the
            # card header, so they render under any font/scaling setup.
            header_actions = tk.Frame(
                main_header_frame, background=label_playlist_name_bg
            )
            close_playlist = tk.Button(
                header_actions,
                image=self._close_playlist_img,
                cursor="hand2",
                **button_playlist_btn,
                highlightthickness=0,
                relief="raised",
            )
            ToolTip(close_playlist, "Close playlist")
            self._close_buttons.append(close_playlist)

            # Pin/unpin toggle - sits immediately LEFT of the close button.
            pin_btn = None
            if self._pin_img is not None and self._show_pin_buttons:
                pin_btn = tk.Button(
                    header_actions,
                    image=self._pin_img,
                    cursor="hand2",
                    **button_playlist_btn,
                    highlightthickness=0,
                    relief="raised",
                )
                pin_btn._pm_pinned = False
                ToolTip(
                    pin_btn,
                    lambda b=pin_btn: (
                        "Unpin playlist" if getattr(b, "_pm_pinned", False)
                        else "Pin playlist"
                    ),
                )
            self._pin_buttons.append(pin_btn)

            playlist_keybind = tk.Entry(
                main_header_frame,
                font=ui_font(12),
                justify="center",
                background=entry_playlist_bg,
                foreground=entry_playlist_fg,
                readonlybackground=entry_playlist_ro_bg,
                state="readonly",
            )
            ToolTip(playlist_keybind, "Click to record a keybind")

            reload_database = tk.Button(
                main_header_frame,
                image=self._reload_database_img,
                cursor="hand2",
                **button_playlist_btn,
                highlightthickness=0,
                relief="raised",
            )
            ToolTip(reload_database, "Reload from platform")

            log_artist = tk.Label(
                main_log_frame,
                text="",
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
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
                text="",
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                anchor="w",
            )
            log_status = tk.Label(
                main_log_frame,
                text="Waiting",
                font=ui_font(12),
                background=label_playlist_good_bg,
                foreground=label_playlist_good_fg,
                anchor="w",
            )

            card = PlaylistCard(
                frame=main_frame,
                header_frame=main_header_frame,
                name_label=playlist_name,
                cover_label=playlist_cover,
                keybind_entry=playlist_keybind,
                reload_btn=reload_database,
                stats_frame=main_stats_frame,
                stats_songs=stats_songs,
                stats_duration=stats_duration,
                stats_followers=stats_followers,
                log_frame=main_log_frame,
                log_artist=log_artist,
                log_name=log_name,
                log_status=log_status,
                platform="",
            )
            card.position = (row, col)

            def _open_card_playlist(_event, c=card):
                try:
                    pid = c.playlist_id or ""
                    url = build_playlist_url(c.platform, pid)
                    if not url:
                        return
                    webbrowser.open(url)
                except Exception:
                    logger.debug("Failed to open playlist URL", exc_info=True)

            playlist_name.bind("<Button-1>", _open_card_playlist)
            playlist_name.configure(cursor="hand2")

            close_playlist["command"] = lambda c=card: self._confirm_close_playlist(c)
            if pin_btn is not None:
                pin_btn["command"] = (
                    lambda c=card: self._on_pin_toggle_cb(c)
                    if self._on_pin_toggle_cb
                    else None
                )
            playlist_keybind.bind(
                "<Button-1>",
                lambda e, c=card: self._start_recording_cb(self._card_index(c)),
            )
            reload_database["command"] = lambda c=card: self._on_reload_requested_cb(
                self._card_index(c)
            )

            main_header_frame.grid_columnconfigure(1, weight=1)
            main_log_frame.grid_columnconfigure(2, weight=1)

            main_frame.grid(
                row=row, column=col, sticky=self._column_sticky(col), pady=(5, 0), padx=2.5
            )
            main_header_frame.grid(row=0, column=0, sticky="nsew")
            main_stats_frame.grid(row=1, column=0, sticky="nsew")
            main_log_frame.grid(row=2, column=0, sticky="nsew")

            playlist_cover.grid(row=0, column=0, sticky="ne", rowspan=2)
            playlist_name.grid(row=0, column=1, sticky="nswe")
            # Close always lives in the actions frame; the pin joins it on
            # the left when enabled.  One code path, no None branches.
            if pin_btn is not None:
                pin_btn.pack(side="left")
            close_playlist.pack(side="left")
            header_actions.grid(row=0, column=2, sticky="ne")
            playlist_keybind.grid(row=1, column=1, sticky="nswe")
            reload_database.grid(row=1, column=2, sticky="ne")

            stats_songs.grid(row=0, column=0, padx=4, sticky="nswe")
            stats_duration.grid(row=0, column=1, padx=4, sticky="nswe")
            stats_followers.grid(row=0, column=2, padx=4, sticky="nswe")
            main_stats_frame.grid_columnconfigure(0, weight=1)
            main_stats_frame.grid_columnconfigure(1, weight=0)
            main_stats_frame.grid_columnconfigure(2, weight=1)

            log_artist.grid(row=0, column=0, padx=(0, 2), sticky="nswe")
            log_helper_1.grid(row=0, column=1, sticky="nswe")
            log_name.grid(row=0, column=2, sticky="nswe")
            log_status.grid(row=0, column=3, padx=(0, 2), sticky="nswe")

            self.cards.append(card)
            if not self._show_stats:
                main_stats_frame.grid_remove()
            if not self._show_log:
                main_log_frame.grid_remove()
            self._update_card_height(i, layout=True)

        self._sf.update_scrollregion()
        self._auto_resize_cb()
        self._sync_empty_state()

    # ------------------------------------------------------------------
    # Card deletion
    # ------------------------------------------------------------------

    def _confirm_close_playlist(self, card) -> None:
        try:
            # Membership check: card must still be in the grid.
            self.cards.index(card)
            playlist_name = card.name_label.cget("text")
        except (ValueError, tk.TclError):
            logger.error("Close confirmation: frame not found")
            return
        # Confirmation gate: with "Ask before removing the playlist" ON
        # the close dialog decides (Cancel / Keep DB / Remove); with it
        # OFF the [remove_playlist] default applies without a dialog.
        mode = resolve_playlist_removal_mode(
            get_setting("confirm_on_playlist_remove", True),
            get_setting_value("remove_playlist", "default", REMOVE_PLAYLIST_DEFAULT_MODE),
        )
        if mode is None:
            show_close_playlist_dialog(
                self.root,
                playlist_name,
                on_cancel=None,
                on_keep_db=lambda: self.close_main_frame(card, delete_db=False),
                on_confirm=lambda: self.close_main_frame(card, delete_db=True),
            )
        else:
            self.close_main_frame(card, delete_db=(mode != "keep_db"))

    def close_main_frame(self, card, delete_db: bool = True) -> None:
        try:
            index = self.cards.index(card)
            playlist_name = card.name_label.cget("text")
            platform = card.platform
            playlist_id = card.playlist_id

            self.kc.unregister_keybind(
                playlist_name, platform=platform, playlist_id=playlist_id
            )

            self._before_card_close_cb(index)

            self.cards.pop(index)

            self._prune_frame_imgs_cb(card.frame)

            self._after_card_close_cb(index)

            PlaylistStore.delete_playlist(
                playlist_name, platform=platform, playlist_id=playlist_id
            )
            if delete_db:
                DatabaseManager.delete_playlist_db(
                    playlist_name, platform, playlist_id
                )

            # Scrobble-ledger records are pinned to this playlist's rows -
            # worthless once the playlist + DB are gone.
            scrobble_log.remove_playlist_entries(platform, playlist_id)

            # Pending duplicate decisions, pair-memory and error-log lines
            # reference this playlist - purge them too, or they linger in
            # db/extra.json and resurface if the playlist is re-added.
            duplicate_queue.purge_playlist(
                platform, playlist_id, playlist_name
            )

            card.frame.grid_forget()
            card.frame.destroy()
            self._reorder_frames()
            self._sync_empty_state()
            logger.debug("Closed frame at index %d", index)
            self._auto_resize_cb()
        except (ValueError, IndexError) as e:
            logger.error("Error closing frame: %s", e)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _layout_frames(self) -> None:
        for i, card in enumerate(self.cards):
            col = i % self._columns
            row = (i // self._columns) + 1
            card.position = (row, col)
            self._restore_frame_grid(card.frame, i)
        self._sf.update_scrollregion()

    def _reorder_frames(self) -> None:
        self._layout_frames()
        logger.debug("Reordered frames after deletion")

    def _column_sticky(self, col: int) -> str:
        return "nsew"

    def _restore_frame_grid(self, frame: tk.Frame, idx: int) -> None:
        pos = self.cards[idx].position
        frame.grid(
            row=pos[0], column=pos[1],
            sticky=self._column_sticky(pos[1]),
            pady=(5, 0), padx=2.5,
        )
        frame.grid_propagate(False)

    # ------------------------------------------------------------------
    # Empty state
    # ------------------------------------------------------------------

    def _sync_empty_state(self) -> None:
        try:
            if not self.cards:
                self.empty_state_btn.grid(
                    row=1,
                    column=0,
                    columnspan=self._columns,
                    sticky="n",
                    pady=px(48),
                )
            elif self.empty_state_btn.winfo_ismapped():
                self.empty_state_btn.grid_remove()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------

    def _hide_main_content(self) -> None:
        self.parent_frame.grid_forget()
        for card in self.cards:
            card.frame.grid_forget()

    def _show_main_content(self) -> None:
        self.parent_frame.grid(
            row=2, column=0, columnspan=self._columns, sticky="nsew"
        )
        for i, card in enumerate(self.cards):
            self._restore_frame_grid(card.frame, i)
        self._sf.update_scrollregion()
        self._sync_empty_state()

    # ------------------------------------------------------------------
    # Card height
    # ------------------------------------------------------------------

    def _update_card_height(self, frame_idx: int, *, layout: bool = False) -> None:
        try:
            card = self.cards[frame_idx]
        except IndexError:
            return
        if layout:
            try:
                self.root.update_idletasks()
            except Exception:
                logger.debug("update_idletasks failed (teardown?)", exc_info=True)
        showcase = card.showcase_frame
        showcase_h = showcase.winfo_reqheight() if showcase is not None else 0
        height = px(CARD_H_BASE) + showcase_h
        if not self._show_log:
            height -= px(LOG_ROW_H_BASE)
        if self._show_stats:
            height += px(STATS_ROW_H_BASE)
        search_results_h = self._get_search_results_height_cb(frame_idx)
        if search_results_h is not None:
            height += search_results_h
        card.frame.config(height=max(px(CARD_H_BASE) - px(LOG_ROW_H_BASE), height))
        self._sf.update_scrollregion()

        if self._showcase_count > 0:
            self._auto_resize_cb()

    # ------------------------------------------------------------------
    # Log / stats visibility
    # ------------------------------------------------------------------

    def _apply_log_visibility(self, frame_idx: int) -> None:
        try:
            card = self.cards[frame_idx]
        except IndexError:
            return
        log_frame = card.log_frame
        if log_frame is None:
            return
        if self._show_log:
            log_frame.grid()
        else:
            log_frame.grid_remove()
        self._update_card_height(frame_idx)

    def _apply_stats_visibility(self, frame_idx: int) -> None:
        try:
            card = self.cards[frame_idx]
        except IndexError:
            return
        stats_frame = card.stats_frame
        if stats_frame is None:
            return
        if self._show_stats:
            stats_frame.grid()
        else:
            stats_frame.grid_remove()
        self._update_card_height(frame_idx)

    # ------------------------------------------------------------------
    # DB log labels
    # ------------------------------------------------------------------

    def _update_log_labels_from_db(
        self, frame_idx: int, playlist_name: str, platform: str
    ) -> None:
        sm = self._song_manager
        try:
            card = self.cards[frame_idx]
        except IndexError:
            return
        latest = sm.get_latest_song(
            playlist_name,
            platform=platform,
            playlist_id=card.playlist_id or "",
        )
        if not latest:
            return
        artists = latest.get("artists", [])
        artists_str = ", ".join(artists[:2]) if isinstance(artists, list) else str(artists)
        card.log_artist.config(text=artists_str[:8])
        card.log_name.config(text=latest.get("title", "")[:18])

    # ------------------------------------------------------------------
    # Theme (card-specific parts only)
    # ------------------------------------------------------------------

    def apply_theme(self) -> None:
        try:
            self.empty_state_btn.configure(
                **btn_colors(C["button_main_bg"], C["button_main_fg"])
            )
        except tk.TclError:
            pass

        frame_playlist_bg = C["frame_playlist_bg"]
        for frame_idx, card in enumerate(self.cards):
            card.frame.configure(background=frame_playlist_bg)
            for container in (card.header_frame, card.stats_frame,
                              card.log_frame, card.showcase_frame):
                if container is not None:
                    container.configure(background=frame_playlist_bg)

            card.name_label.configure(
                background=C["label_playlist_name_bg"],
                foreground=C["label_playlist_name_fg"],
            )
            card.cover_label.configure(background=C["label_playlist_bg"])

            for lbl in (card.log_artist, card.log_name):
                lbl.configure(
                    background=C["label_playlist_log_bg"],
                    foreground=C["label_playlist_log_fg"],
                )

            for lbl in (card.stats_songs, card.stats_duration, card.stats_followers):
                lbl.configure(
                    background=C["label_playlist_stats_bg"],
                    foreground=C["label_playlist_stats_fg"],
                )

            if not self._is_recording_cb(frame_idx):
                card.keybind_entry.configure(
                    background=C["entry_playlist_bg"],
                    foreground=C["entry_playlist_fg"],
                    readonlybackground=C["entry_playlist_ro_bg"],
                )

            known_labels = {
                card.cover_label,
                card.log_status,
                card.log_artist,
                card.log_name,
                card.name_label,
                card.stats_songs,
                card.stats_duration,
                card.stats_followers,
            }
            for child in card.frame.winfo_children():
                for widget in child.winfo_children():
                    if isinstance(widget, tk.Label) and widget not in known_labels:
                        widget.configure(
                            background=C["label_playlist_log_bg"],
                            foreground=C["label_playlist_log_fg"],
                        )
                    elif isinstance(widget, tk.Button):
                        widget.configure(
                            **btn_colors(
                                C["button_playlist_bg"], C["button_playlist_fg"]
                            )
                        )
