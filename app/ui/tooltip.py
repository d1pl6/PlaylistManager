"""Hover tooltips (tkinter).

Theme- and scale-aware: colors come from the live palette
(``utils.theme.C``) and the font from ``utils.scaling.ui_font``, read at
*show* time — a theme change applies to the next hover without
re-attaching.  Usage::

    from ui.tooltip import ToolTip

    ToolTip(btn_login, "Log in to YouTube Music")
"""

import tkinter as tk

from utils.scaling import px, ui_font
from utils.theme import C

#: Hover delay before the tip appears (ms).
_DELAY = 400


class ToolTip:
    """Show a small bubble on hover, hide on leave / click / widget destroy.

    The tip Toplevel is created lazily on first hover and destroyed on
    leave, so a tip only exists while it is visible.  The Toplevel is a
    child of the attached widget, so Tk destroys it automatically if the
    widget is destroyed (closed card, closed dialog) — no orphaned
    bubbles.
    """

    def __init__(self, widget: tk.Widget, text) -> None:
        self.widget = widget
        # Accept a plain string or a callable (re-read at show time, e.g.
        # a playlist name that is set after the widget is created).
        self._text = text
        self._tip: tk.Toplevel | None = None
        self._after_id: str | None = None

        # add="+" composes with existing bindings on the same widget.
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    # -- lifecycle ------------------------------------------------------

    def _schedule(self, _event=None) -> None:
        self._cancel_timer()
        try:
            self._after_id = self.widget.after(_DELAY, self._show)
        except tk.TclError:
            self._after_id = None  # widget/root already destroyed

    def _show(self) -> None:
        self._after_id = None
        if self._tip is not None:
            return
        text = self._text() if callable(self._text) else self._text
        if not text:
            return

        # Geometry is read at show time (not attach time) so the tip
        # stays put after window moves/resizes.  Positioned just below
        # the widget, left-aligned with it.
        try:
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except tk.TclError:
            return

        tip = tk.Toplevel(self.widget)

        tip.withdraw()

        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass  # some WMs/compositors reject -topmost; tip still shows

        # Set a matching background on the toplevel to avoid "white cube".
        tip.configure(background=C["frame_main_bg"])

        tk.Label(
            tip,
            text=text,
            background=C["frame_main_bg"],
            foreground=C["label_def_fg"],
            font=ui_font(9),
            padx=px(6),
            pady=px(3),
            justify="left",
        ).pack()

        tip.deiconify()

        self._tip = tip

    def _hide(self, _event=None) -> None:
        self._cancel_timer()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

    def _cancel_timer(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
