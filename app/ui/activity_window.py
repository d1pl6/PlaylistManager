"""
Non-modal Activity window - persistent error log + duplicate songs.

Two tabs, plain-tk toggle buttons instead of ttk.Notebook (repo style):

``Errors``
    Persistent log fed by keybind flow failures, import failures and
    failed activity actions.  Today those errors only paint a card red
    and vanish at the next keybind; here they survive until cleared.

``Duplicates``
    Three sections rendered from :meth:`MainWindow._load_activity_data`:
    pending queued adds ("Similar song" cards with Don't add / Add),
    scan results ("Not duplicates" / "Remove newer" pair cards), and the
    marked-pairs manager (every recorded pair song with an Undo).

Lifecycle: closing HIDES the window (withdraw) so hotkey adds keep
working while it is open; a repeated open re-lifts and re-renders fresh
data.  All callbacks arrive on the UI thread; every mutation funnels
through the injected ``on_song(record, action)`` so this module
stays free of business logic.
"""

from __future__ import annotations

import logging
import tkinter as tk
import webbrowser

from utils.scaling import px, ui_font
from utils.theme import C, btn_colors
from ui.scrollable import ScrollableFrame

logger = logging.getLogger(__name__)

WINDOW_TITLE = "PlaylistManager \u2014 Activity"

# Actions reported through on_song(record, action):
#   pending cards: "add" | "dismiss"
#   pair cards:    "not_duplicate" | "remove_newer"
#   undo rows:     "undo"      (record {"kind": "pair", "pair_key": ...})

# Module-level ref: repeated opens re-lift the same window instead of
# stacking copies (hotkey adds keep working while it stays open).
_active_window: "ActivityWindow | None" = None

_SONG_LINK_PREFIX = {
    # Plain URL transform of odesli's pages - no API, no key (the public
    # API itself is closed/alpha); used only as an advisory link.
    "youtube_music": "https://song.link/y/",
    "spotify": "https://song.link/s/",
}


def show_activity_window(parent, *, load_data, on_song, on_close=None):
    """Open (or re-lift) the activity window.

    Args:
        parent: tkinter parent window.
        load_data: callable returning a fresh snapshot dict with keys
            ``pending``, ``errors``, ``songs``, ``pairs``.
        on_song: callable(record, action) - the single mutation hook.
        on_close: optional callable fired when the user hides the window
            (used by MainWindow to refresh the badge).
    """
    global _active_window
    if _active_window is not None and _active_window.winfo_exists():
        win = _active_window
    else:
        win = ActivityWindow(
            parent,
            load_data=load_data,
            on_song=on_song,
            on_close=on_close,
        )
        _active_window = win
    win.refresh()
    try:
        win.deiconify()
        win.lift()
        win.focus_set()
    except tk.TclError:
        pass
    return win


def _fmt_duration(seconds) -> str:
    """m:ss rendering; unknown lengths render as '?'."""
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "?"
    if total <= 0:
        return "?"
    return f"{total // 60}:{total % 60:02d}"


def _fmt_artists(artists) -> str:
    if isinstance(artists, list):
        return ", ".join(a for a in artists if a) or "Unknown Artist"
    return str(artists or "Unknown Artist")


class ActivityWindow(tk.Toplevel):
    def __init__(self, parent, *, load_data, on_song, on_close=None):
        super().__init__(parent)
        self.title(WINDOW_TITLE)
        self.configure(background=C["root_bg"])
        self.minsize(px(520), px(380))
        self.geometry(f"{px(600)}x{px(520)}")
        try:
            self.transient(parent)
        except tk.TclError:
            pass

        self._load_data = load_data
        self._on_song = on_song
        self._on_close_cb = on_close
        self._show_marked = False  # collapsed by default

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._hide)

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------

    def _build(self) -> None:
        tab_bar = tk.Frame(self, background=C["frame_head_bg"])
        tab_bar.pack(side="top", fill="x")

        self._tab_buttons = {}
        for name in ("Errors", "Duplicates"):
            btn = tk.Button(
                tab_bar,
                text=name,
                cursor="hand2",
                font=ui_font(12),
                highlightthickness=0,
                relief="flat",
                command=lambda n=name: self._select_tab(n),
            )
            btn.pack(side="left", padx=(8, 0), pady=4)
            self._tab_buttons[name] = btn

        self._body = tk.Frame(self, background=C["root_bg"])
        self._body.pack(side="top", fill="both", expand=True)

        bg = C["scrollable_frame_bg"]
        self._errors_view = ScrollableFrame(self._body, bg=bg)
        self._dups_view = ScrollableFrame(self._body, bg=bg)
        self._current_tab = "Duplicates"
        self._select_tab("Duplicates")

    def _select_tab(self, name: str) -> None:
        self._current_tab = name
        active_bg, inactive_bg = (
            C["label_playlist_warn_bg"],
            C["button_head_bg"],
        )
        fg_active, fg_inactive = (
            C["label_playlist_warn_fg"],
            C["button_head_fg"],
        )
        for label, btn in self._tab_buttons.items():
            selected = label == name
            try:
                btn.configure(
                    background=active_bg if selected else inactive_bg,
                    foreground=fg_active if selected else fg_inactive,
                    activebackground=active_bg if selected else inactive_bg,
                    activeforeground=fg_active if selected else fg_inactive,
                )
            except tk.TclError:
                pass
        self._errors_view.pack_forget()
        self._dups_view.pack_forget()
        view = self._errors_view if name == "Errors" else self._dups_view
        view.pack(side="top", fill="both", expand=True)

    def _hide(self) -> None:
        """Hide instead of destroy - the window survives all session."""
        self.withdraw()
        if callable(self._on_close_cb):
            try:
                self._on_close_cb()
            except Exception:
                logger.debug("activity on_close callback failed", exc_info=True)

    def refresh(self) -> None:
        """Re-render both tabs from a fresh snapshot."""
        data = self._load_data() or {}
        self._render_errors(data)
        self._render_duplicates(data)
        self._select_tab(self._current_tab)

    # ------------------------------------------------------------------
    # Small builders
    # ------------------------------------------------------------------

    def _act(self, record: dict, action: str) -> None:
        """Funnel every button through the injected dispatcher."""
        try:
            self._on_song(record, action)
        except Exception:
            logger.error("on_song(%r) failed", action, exc_info=True)
        self.refresh()

    def _section_header(self, parent, text: str) -> tk.Label:
        label = tk.Label(
            parent,
            text=text,
            background=C["scrollable_frame_bg"],
            foreground=C["label_def_fg"],
            font=ui_font(13, "bold"),
            anchor="w",
        )
        label.pack(fill="x", padx=10, pady=(10, 4))
        return label

    def _empty_label(self, parent, text: str) -> None:
        tk.Label(
            parent,
            text=text,
            background=C["scrollable_frame_bg"],
            foreground=C["label_def_fg"],
            font=ui_font(11),
        ).pack(fill="x", padx=10, pady=(6, 10))

    def _song_line(self, parent, prefix: str, song: dict) -> None:
        """'prefix: author - name (m:ss)' row."""
        text = (
            f"{prefix} {_fmt_artists(song.get('artists'))} - "
            f"{song.get('title', 'Unknown')} "
            f"({_fmt_duration(song.get('duration'))})"
        )
        tk.Label(
            parent,
            text=text,
            background=C["frame_playlist_bg"],
            foreground=C["label_playlist_fg"],
            font=ui_font(11),
            anchor="w",
        ).pack(fill="x", padx=10, pady=1)

    def _link_row(self, parent, platform: str, track_id, label: str) -> None:
        url = _SONG_LINK_PREFIX.get(platform)
        if not url or not track_id:
            return
        link = tk.Label(
            parent,
            text=label,
            background=C["frame_playlist_bg"],
            foreground="#4da6ff",
            cursor="hand2",
            font=ui_font(9, "underline"),
            anchor="w",
        )
        link.pack(fill="x", padx=10)
        link.bind(
            "<Button-1>",
            lambda _e, u=f"{url}{track_id}": webbrowser.open(u),
        )

    def _action_button(self, parent, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            cursor="hand2",
            **btn_colors(C["button_main_bg"], C["button_main_fg"]),
            font=ui_font(11),
            highlightthickness=0,
            relief="raised",
            command=command,
        )

    # ------------------------------------------------------------------
    # Errors tab
    # ------------------------------------------------------------------

    def _render_errors(self, data: dict) -> None:
        content = self._errors_view.content
        for child in content.winfo_children():
            child.destroy()
        bg = C["scrollable_frame_bg"]
        content.configure(background=bg)

        header = tk.Frame(content, background=bg)
        header.pack(fill="x", padx=10, pady=(8, 0))
        errors = data.get("errors") or []
        tk.Label(
            header,
            text=f"Errors ({len(errors)})",
            background=bg,
            foreground=C["label_def_fg"],
            font=ui_font(13, "bold"),
        ).pack(side="left")
        if errors:
            self._action_button(header, "Clear", self._clear_errors).pack(
                side="right"
            )

        if not errors:
            self._empty_label(content, "No errors logged.")
            return
        for err in errors:
            card = tk.Frame(
                content,
                background=C["label_playlist_error_bg"],
                padx=0,
                pady=4,
            )
            card.pack(fill="x", padx=10, pady=4)
            tk.Label(
                card,
                text=str(err.get("message", "")),
                background=C["label_playlist_error_bg"],
                foreground=C["label_playlist_error_fg"],
                font=ui_font(11),
                anchor="w",
                wraplength=px(520),
                justify="left",
            ).pack(fill="x", padx=10)
            where = " \u00b7 ".join(
                part
                for part in (
                    err.get("playlist_name", ""),
                    err.get("platform", ""),
                    str(err.get("created_at", "")),
                )
                if part
            )
            if where:
                tk.Label(
                    card,
                    text=where,
                    background=C["label_playlist_error_bg"],
                    foreground=C["label_playlist_error_fg"],
                    font=ui_font(9),
                    anchor="w",
                ).pack(fill="x", padx=10)

    def _clear_errors(self) -> None:
        self._act({"kind": "clear_errors"}, "clear")

    # ------------------------------------------------------------------
    # Duplicates tab
    # ------------------------------------------------------------------

    def _render_duplicates(self, data: dict) -> None:
        content = self._dups_view.content
        for child in content.winfo_children():
            child.destroy()
        bg = C["scrollable_frame_bg"]
        content.configure(background=bg)

        pending = data.get("pending") or []
        pairs = data.get("pairs") or []
        songs = data.get("songs") or {}

        self._section_header(content, f"Pending songs ({len(pending)})")
        if not pending:
            self._empty_label(
                content, "Nothing waiting - hotkey adds resolved on their own."
            )
        for record in pending:
            self._pending_card(content, record)

        self._section_header(content, f"Scan results ({len(pairs)})")
        if not pairs:
            self._empty_label(
                content,
                'Run Settings \u2192 "Check for duplicates now" to search.',
            )
        for record in pairs:
            self._pair_card(content, record)

        self._marked_pairs_section(content, songs)

    def _pending_card(self, parent, record: dict) -> None:
        card = tk.Frame(
            parent, background=C["frame_playlist_bg"], padx=0, pady=6
        )
        card.pack(fill="x", padx=10, pady=4)

        head = tk.Frame(card, background=C["frame_playlist_bg"])
        head.pack(fill="x")
        similarity = record.get("similarity")
        pct = f" \u00b7 {round(similarity * 100)}% match" if similarity else ""
        tk.Label(
            head,
            text="Similar song",
            background=C["frame_playlist_bg"],
            foreground=C["label_playlist_warn_fg"],
            font=ui_font(12, "bold"),
        ).pack(side="left", padx=10)
        tk.Label(
            head,
            text=f"{record.get('playlist_name', '')}{pct}",
            background=C["frame_playlist_bg"],
            foreground=C["label_playlist_fg"],
            font=ui_font(9),
        ).pack(side="right", padx=10)

        self._song_line(card, "already in playlist:", record.get("existing") or {})
        self._link_row(
            card,
            record.get("platform", ""),
            (record.get("existing") or {}).get("track_id"),
            "existing on song.link \u2197",
        )
        self._song_line(card, "trying to add:      ", record)
        self._link_row(
            card,
            record.get("platform", ""),
            record.get("track_id"),
            "new on song.link \u2197",
        )

        buttons = tk.Frame(card, background=C["frame_playlist_bg"])
        buttons.pack(fill="x", pady=(4, 0))
        self._action_button(
            buttons, "Don't add", lambda r=dict(record): self._act(r, "dismiss")
        ).pack(side="right", padx=10, pady=2)
        self._action_button(
            buttons, "Add", lambda r=dict(record): self._act(r, "add")
        ).pack(side="right", pady=2)

    def _pair_card(self, parent, record: dict) -> None:
        card = tk.Frame(
            parent, background=C["frame_playlist_bg"], padx=0, pady=6
        )
        card.pack(fill="x", padx=10, pady=4)

        similarity = record.get("similarity")
        pct = f" \u00b7 {round(similarity * 100)}% match" if similarity else ""
        tk.Label(
            card,
            text=(
                "Both variants already in playlist \u2014 "
                f"{record.get('playlist_name', '')}{pct}"
            ),
            background=C["frame_playlist_bg"],
            foreground=C["label_playlist_fg"],
            font=ui_font(12, "bold"),
            anchor="w",
        ).pack(fill="x", padx=10)

        self._song_line(card, "older:  ", record.get("older") or {})
        self._song_line(card, "newer:  ", record.get("newer") or {})

        buttons = tk.Frame(card, background=C["frame_playlist_bg"])
        buttons.pack(fill="x", pady=(4, 0))
        self._action_button(
            buttons,
            "Remove newer",
            lambda r=dict(record): self._act(r, "remove_newer"),
        ).pack(side="right", padx=10, pady=2)
        self._action_button(
            buttons,
            "Not duplicates",
            lambda r=dict(record): self._act(r, "not_duplicate"),
        ).pack(side="right", pady=2)

    def _marked_pairs_section(self, parent, songs: dict) -> None:
        bg = C["scrollable_frame_bg"]
        arrow = "\u25be" if self._show_marked else "\u25b8"

        def _toggle() -> None:
            self._show_marked = not self._show_marked
            self.refresh()

        tk.Button(
            parent,
            text=f"Marked / decided pairs ({len(songs)}) {arrow}",
            cursor="hand2",
            **btn_colors(C["button_main_bg"], C["button_main_fg"]),
            font=ui_font(11),
            highlightthickness=0,
            relief="flat",
            anchor="w",
            command=_toggle,
        ).pack(fill="x", padx=10, pady=(12, 2))

        if not self._show_marked or not songs:
            return
        for pair_key, info in sorted(
            songs.items(),
            key=lambda kv: kv[1].get("at", ""),
            reverse=True,
        ):
            row = tk.Frame(parent, background=bg)
            row.pack(fill="x", padx=16, pady=1)
            tk.Label(
                row,
                text=f"{pair_key}  \u2014  {info.get('song', '?')}",
                background=bg,
                foreground=C["label_def_fg"],
                font=ui_font(9),
                anchor="w",
            ).pack(side="left")
            self._action_button(
                row,
                "Undo",
                lambda k=pair_key: self._act(
                    {"kind": "pair", "pair_key": k}, "undo"
                ),
            ).pack(side="right")
