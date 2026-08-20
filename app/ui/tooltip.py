"""Hover tooltips (tkinter).

Theme- and scale-aware: colors come from the live palette
(``utils.theme.C``) and the font from ``utils.scaling.ui_font``, read at
*show* time - a theme change applies to the next hover without
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
    child of the *root window* (not the widget) so that ``wm_geometry``
    screen coordinates are not distorted by the widget's parent chain
    (e.g. a Canvas scroll offset).  Cleanup is explicit: the ``<Destroy>``
    binding on the widget tears down the tip if the widget is removed.
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
        # If the widget is destroyed (card close, dialog close) while the
        # tip is still visible or the timer is pending, tear it down.
        widget.bind("<Destroy>", self._on_widget_destroy, add="+")

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
        # the widget, horizontally centred; flipped above when near the
        # bottom edge of the screen.
        try:
            w_x = self.widget.winfo_rootx()
            w_y = self.widget.winfo_rooty()
            w_w = self.widget.winfo_width()
            w_h = self.widget.winfo_height()
            screen_h = self.widget.winfo_screenheight()
            screen_w = self.widget.winfo_screenwidth()
        except tk.TclError:
            return

        # Parent the Toplevel to the root window so that screen-
        # absolute wm_geometry coordinates are not shifted by the
        # widget's parent chain (Canvas scroll, nested frames, etc.).
        tip = tk.Toplevel(self.widget.winfo_toplevel())

        tip.withdraw()

        tip.wm_overrideredirect(True)
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass  # some WMs/compositors reject -topmost; tip still shows

        # Set a matching background on the toplevel to avoid "white cube".
        tip.configure(background=C["frame_main_bg"])

        # Cap the label width at 60 % of the screen so long text wraps
        # rather than running off the right edge.
        wrap_len = max(screen_w * 3 // 5, px(200))

        tk.Label(
            tip,
            text=text,
            background=C["frame_main_bg"],
            foreground=C["label_def_fg"],
            font=ui_font(9),
            padx=px(6),
            pady=px(3),
            justify="left",
            wraplength=wrap_len,
        ).pack()

        # Measure the tip height to decide whether to flip above the
        # widget.  update_idletasks() is needed while still withdrawn so
        # geometry is computed but nothing flickers on screen.
        tip.update_idletasks()
        tip_h = tip.winfo_reqheight()
        tip_w = tip.winfo_reqwidth()

        # Centre horizontally on the widget.
        x = w_x + (w_w - tip_w) // 2

        y = w_y + w_h + 4  # default: below the widget
        if y + tip_h > screen_h:
            y = w_y - tip_h - 4  # flip above

        # Clamp so the tip stays on-screen.
        x = max(0, min(x, screen_w - tip_w - px(4)))

        tip.wm_geometry(f"+{x}+{y}")
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

    def _on_widget_destroy(self, _event=None) -> None:
        """Tear down when the attached widget is destroyed."""
        self._cancel_timer()
        self._hide()

    def _cancel_timer(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
