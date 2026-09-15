"""
Shared dialog helpers for the UI layer.

Keeps controller- and window-level code from building widget-heavy
dialogs directly - the controller delegates to these functions so the
"no tkinter" boundary is respected.
"""

import logging
import tkinter as tk

from utils.i18n import tr
from utils.scaling import px, ui_font
from utils.theme import C, btn_colors

logger = logging.getLogger(__name__)


def confirm_force_quit(root, error: BaseException) -> bool:
    """Show a Force-quit / Cancel dialog; return True to force-quit.

    If the root window is already destroyed there is nothing left to
    cancel (and nowhere to host a dialog), so force-quit is the only
    meaningful outcome.
    """
    try:
        if not root.winfo_exists():
            return True
    except tk.TclError:
        return True

    result = {"force": False}

    # If the main window is withdrawn (hidden to tray) the error
    # dialog, being transient to it, can hide behind the tray icon -
    # make the root visible again so the dialog has a parent on screen.
    try:
        if root.state() != "normal":
            root.deiconify()
    except tk.TclError:
        pass

    try:
        dialog = tk.Toplevel(root)
        dialog.title(tr("common.error"))
        dialog.configure(background=C["frame_main_bg"])
        dialog.transient(root)
        dialog.grab_set()
        dialog.resizable(False, False)

        tk.Label(
            dialog,
            text=tr("quit.error_while_closing"),
            background=C["frame_main_bg"],
            foreground=C["label_def_fg"],
            font=ui_font(10),
        ).pack(padx=16, pady=(12, 0))

        tk.Label(
            dialog,
            text=str(error),
            background=C["frame_main_bg"],
            foreground=C["label_playlist_error_fg"],
            font=ui_font(9),
            wraplength=px(360),
            justify="left",
        ).pack(padx=16, pady=(4, 12))

        def _choose(force: bool) -> None:
            result["force"] = force
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", lambda: _choose(False))

        tk.Button(
            dialog,
            text=tr("quit.force_quit"),
            command=lambda: _choose(True),
            **btn_colors(C["button_main_bg"], C["button_main_fg"]),
            bd=0,
        ).pack(side="left", padx=(16, 4), pady=(0, 12))

        tk.Button(
            dialog,
            text=tr("common.cancel"),
            command=lambda: _choose(False),
            **btn_colors(C["button_head_bg"], C["button_head_fg"]),
            bd=0,
        ).pack(side="left", padx=(4, 16), pady=(0, 12))

        root.wait_window(dialog)
        return result["force"]
    except Exception:
        # The dialog itself failed to build (grab conflict, a root
        # destroyed between the checks above, ...) - there is no
        # place left to host it and nothing to cancel, so force-quit
        # is the only meaningful outcome.  Returning False here would
        # let the exception escape quit_app's handler with _quitting
        # still True, permanently blocking every later quit request.
        logger.exception("Failed to show the force-quit dialog")
        return True
