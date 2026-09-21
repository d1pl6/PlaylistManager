"""NotificationBar - the fixed status row showing the last flow event.

A single row (default: directly under the header) rendering the last
activity as ``Playlist | Artist - Song | Status``.  Status runs through
:func:`utils.i18n.tr_status` (known card codes translated, opaque plugin
messages passed through) and is tinted by the event's background colour,
reusing the card status palette.

When a near-duplicate is queued in ``db/extra.json`` (via
``services.duplicate_check.resolve_near_duplicate``, no UI hooks), the
status segment swaps for an inline **Add** / **Don't add** button pair.
The bar itself is UI-only: MainWindow feeds events, runs the 2 s
activity poll, and dispatches the buttons through the same
``on_song(record, action)`` contract the activity window uses.

``show_event()`` is the single choke point every notification funnels
through - the future desktop-notification sink will hook in here too
(same events, different sink), which is why the kind is an explicit
argument rather than inferred.
"""

import tkinter as tk

from utils.i18n import tr, tr_status
from utils.scaling import ui_font
from utils.theme import C, btn_colors, readable_fg

#: Event kinds rendered by :meth:`NotificationBar.show_event` (see the
#: module docstring for the full vocabulary): the two the prompt poll
#: may take over - a queued ``duplicate`` and a finished ``scan`` -
#: plus idle (None).  Everything else ("added"/"exists"/"error"/opaque
#: "status" messages) keeps the bar until the next event.
_KIND_DUPLICATE = "duplicate"
_KIND_SCAN = "scan"
_PROMPT_TAKEOVER_KINDS = (None, _KIND_DUPLICATE, _KIND_SCAN)


def prompt_may_takeover(current_kind: str | None, prompt_shown: bool) -> bool:
    """Pure decision: may :meth:`NotificationBar.on_store_changed` swap in
    the duplicate prompt for the shown content?

    True when the bar already shows a prompt or when its current event
    kind is idle/duplicate/scan; False when an added/exists/error event
    is on display (those keep the bar until the next event).  Extracted
    so the rule is headless-testable (importing this module creates no
    tkinter widgets).
    """
    return prompt_shown or current_kind in _PROMPT_TAKEOVER_KINDS


class NotificationBar(tk.Frame):
    """Fixed status row with an optional inline duplicate prompt.

    Args:
        parent: tkinter parent (the root window - MainWindow grids this
            frame itself per the ``[notification_bar] position`` setting).
        on_action: callable(record, action) invoked by the prompt
            buttons - same contract as the activity window's
            ``on_song``, so MainWindow reuses ``_handle_activity_song``.
    """

    def __init__(self, parent, *, on_action=None) -> None:
        super().__init__(parent)
        self._on_action = on_action
        self._enabled = True
        self._duplicates_enabled = True
        self._last: dict | None = None
        self._prompt_shown = False
        self._build()
        self.apply_theme()
        self._render_idle()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        # The middle (artist - song) cell expands so the right status
        # segment always hugs the window edge; the middle label's anchor
        # (left/center) follows the [notification_bar] song_align setting.
        self.grid_columnconfigure(1, weight=1)

        self._playlist_lbl = tk.Label(self, text="", font=ui_font(10), anchor="w")
        self._playlist_lbl.grid(row=0, column=0, sticky="w", padx=(6, 4), pady=2)

        self._song_lbl = tk.Label(self, text="", font=ui_font(10), anchor="w")
        self._song_lbl.grid(row=0, column=1, sticky="ew", pady=2)

        # Status segment - either the plain status label or the duplicate
        # prompt frame; exactly one is gridded at any time.
        self._status_lbl = tk.Label(self, text="", font=ui_font(10), anchor="e")
        self._status_lbl.grid(row=0, column=2, sticky="e", padx=(4, 6), pady=2)

        self._prompt_frame = tk.Frame(self)
        tk.Label(
            self._prompt_frame,
            text=tr("notif.duplicate_prompt"),
            font=ui_font(9),
        ).pack(side="left", padx=(0, 6))
        self._btn_add = tk.Button(
            self._prompt_frame,
            text=tr("common.add"),
            cursor="hand2",
            highlightthickness=0,
            relief="raised",
            font=ui_font(9),
        )
        self._btn_add.pack(side="left", padx=(0, 4))
        self._btn_dismiss = tk.Button(
            self._prompt_frame,
            text=tr("activity.dont_add"),
            cursor="hand2",
            highlightthickness=0,
            relief="raised",
            font=ui_font(9),
        )
        self._btn_dismiss.pack(side="left")

    def apply_theme(self) -> None:
        """Re-apply palette colours (MainWindow calls this on theme change)."""
        bg, fg = C["notif_bar_bg"], C["notif_bar_fg"]
        self.configure(background=bg)
        for lbl in (self._playlist_lbl, self._song_lbl, self._status_lbl):
            lbl.configure(background=bg, foreground=fg)
        self._prompt_frame.configure(background=bg)
        for child in self._prompt_frame.winfo_children():
            if isinstance(child, tk.Label):
                child.configure(background=bg, foreground=fg)
        self._btn_add.configure(
            **btn_colors(C["button_save_bg"], C["button_save_fg"])
        )
        self._btn_dismiss.configure(
            **btn_colors(C["button_main_bg"], C["button_main_fg"])
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def show_event(
        self,
        kind: str,
        *,
        playlist: str = "",
        artist: str = "",
        song: str = "",
        status: str = "",
        bg: str | None = None,
    ) -> None:
        """Render one notification event - the single choke point.

        *status* is a card-status code (``added``/``exists``/...)
        translated via :func:`tr_status`; opaque flow messages pass
        through.  *bg* tints the status segment (good/warn/error palette
        rows); None keeps the neutral bar colours.
        """
        self._last = {
            "kind": kind,
            "playlist": playlist,
            "artist": artist,
            "song": song,
            "status": status,
            "bg": bg,
        }
        self._prompt_shown = False
        self._render_event(self._last)

    def show_duplicate_prompt(self, record: dict) -> None:
        """Swap the status segment for the inline Approve / Don't-add pair.

        *record* is a pending record from ``duplicate_queue.list_pending``;
        its playlist/artist/title fill the left/middle segments.
        """
        self._prompt_shown = True
        self._set_segments(
            playlist=record.get("playlist_name", ""),
            artist=", ".join(record.get("artists") or []),
            song=record.get("title", ""),
        )
        self._btn_add.config(command=lambda r=record: self._fire(r, "add"))
        self._btn_dismiss.config(
            command=lambda r=record: self._fire(r, "dismiss")
        )
        self._status_lbl.grid_remove()
        self._prompt_frame.grid(row=0, column=2, sticky="e", padx=(4, 6), pady=2)

    def clear_prompt(self) -> None:
        """Return from the duplicate prompt to the last event (or idle)."""
        if not self._prompt_shown:
            return
        self._prompt_shown = False
        self._prompt_frame.grid_forget()
        if self._last is not None:
            self._render_event(self._last)
        else:
            self._render_idle()

    def on_store_changed(
        self, pending: list[dict], duplicates_enabled: bool
    ) -> None:
        """Poll hook: surface the newest pending duplicate when appropriate.

        Called from MainWindow's 2 s activity poll on every
        ``extra.json`` change (keybind flows, this UI, and the CLI
        sharing the file all land here).  Only takes over the bar when
        it is idle or already showing a duplicate/scan result - an
        "added"/"exists"/"error" event stays visible until the next
        event.
        """
        self._duplicates_enabled = duplicates_enabled
        if not self._enabled or not duplicates_enabled:
            return
        if pending:
            current_kind = (self._last or {}).get("kind")
            if prompt_may_takeover(current_kind, self._prompt_shown):
                self.show_duplicate_prompt(pending[0])
        elif self._prompt_shown:
            self.clear_prompt()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        """Remember the enabled state (grid show/hide is MainWindow's job)."""
        self._enabled = enabled

    def set_duplicates_enabled(self, enabled: bool) -> None:
        """Whether on_store_changed may show a prompt (Settings toggle)."""
        self._duplicates_enabled = enabled

    def set_song_align(self, align: str) -> None:
        """Re-anchor the Artist - Song segment ('left' | 'center')."""
        if align not in ("left", "center"):
            return
        self._song_lbl.configure(anchor="center" if align == "center" else "w")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fire(self, record: dict, action: str) -> None:
        if self._on_action is not None:
            self._on_action(record, action)

    def _set_segments(self, *, playlist: str, artist: str, song: str) -> None:
        text = f"{artist} - {song}" if (artist or song) else ""
        try:
            self._playlist_lbl.configure(text=playlist)
            self._song_lbl.configure(text=text)
        except tk.TclError:
            pass  # shutting down

    def _render_event(self, event: dict) -> None:
        self._prompt_frame.grid_forget()
        self._status_lbl.grid(row=0, column=2, sticky="e", padx=(4, 6), pady=2)
        self._set_segments(
            playlist=event.get("playlist", ""),
            artist=event.get("artist", ""),
            song=event.get("song", ""),
        )
        status = event.get("status", "")
        bg = event.get("bg")
        if status:
            self._status_lbl.configure(
                text=tr_status(status),
                background=bg or C["notif_bar_bg"],
                foreground=readable_fg(bg) if bg else C["notif_bar_fg"],
            )
        else:
            self._status_lbl.configure(
                text="",
                background=C["notif_bar_bg"],
                foreground=C["notif_bar_fg"],
            )

    def _render_idle(self) -> None:
        self._last = None
        self._prompt_shown = False
        self._prompt_frame.grid_forget()
        self._status_lbl.grid(row=0, column=2, sticky="e", padx=(4, 6), pady=2)
        self._set_segments(playlist="", artist="", song="")
        self._status_lbl.configure(
            text="", background=C["notif_bar_bg"], foreground=C["notif_bar_fg"]
        )