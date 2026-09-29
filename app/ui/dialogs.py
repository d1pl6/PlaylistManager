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
from utils.window import center_window

logger = logging.getLogger(__name__)

# Outcomes of :func:`confirm_quit`.  Plain strings (not an enum) so the
# caller compares them without importing anything from the UI layer.
QUIT_CANCELLED = "cancel"
QUIT_HIDE = "hide"
QUIT_CONFIRMED = "quit"


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
        # Stay unmapped until it is positioned - see confirm_quit.
        dialog.withdraw()
        dialog.title(tr("common.error"))
        dialog.configure(background=C["frame_main_bg"])
        dialog.transient(root)
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
            foreground=C["status_error_fg"],
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

        # Center on screen like every other dialog in the app (the WM
        # would otherwise place a fresh Toplevel wherever it likes, which
        # for a tray-triggered dialog means near the tray icon), then map
        # it and take the grab once the window is where it belongs.
        center_window(dialog)
        dialog.deiconify()
        dialog.grab_set()
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


def confirm_quit(root) -> str:
    """Ask Cancel / Hide to tray / Quit; return one of the ``QUIT_*`` values.

    Shown only while hide-to-tray is actually available (the caller
    decides, see ``controllers.app_controller.should_confirm_quit``), so
    every path through the dialog has a meaningful outcome:

    - ``QUIT_CANCELLED`` - nothing happens (also Escape / window close).
    - ``QUIT_HIDE`` - the window goes back to the tray.
    - ``QUIT_CONFIRMED`` - proceed with the real quit.

    The window is deiconified first when it is not ``normal`` (hidden in
    the tray), otherwise a tray-triggered dialog would open off-screen
    behind the tray icon.  That visibility change is reverted on every
    non-quit outcome, so "Cancel" while hidden leaves the app exactly as
    invisible as it was.

    A dialog that fails to build returns ``QUIT_CONFIRMED``: the ask is
    an opt-in convenience, and a build failure must not strand the app
    in a state where it can never be quit (same reasoning as
    :func:`confirm_force_quit`).
    """
    try:
        if not root.winfo_exists():
            return QUIT_CONFIRMED
    except tk.TclError:
        return QUIT_CONFIRMED

    was_hidden = False
    try:
        if root.state() != "normal":
            was_hidden = True
            root.deiconify()
            # Let the WM map it before the modal is grabbed, otherwise the
            # dialog can flash in the corner or land behind the icon.
            root.update_idletasks()
    except tk.TclError:
        pass

    result = {"choice": QUIT_CANCELLED}

    try:
        dialog = tk.Toplevel(root)
        # Keep the dialog unmapped while it is being built and positioned
        # (the close_playlist_dialog pattern).  A Toplevel maps as soon as
        # it is created, and KWin drops a post-map move request - the
        # window would stay wherever the WM first put it, i.e. the
        # top-left corner.  Withdrawn, center_window's geometry call
        # becomes a pre-map hint the WM always honors.
        dialog.withdraw()
        dialog.title(tr("quit.confirm_title"))
        dialog.configure(background=C["frame_main_bg"])
        dialog.transient(root)
        dialog.resizable(False, False)

        tk.Label(
            dialog,
            text=tr("quit.confirm_message"),
            background=C["frame_main_bg"],
            foreground=C["label_def_fg"],
            font=ui_font(10),
            wraplength=px(300),
            justify="left",
        ).pack(padx=16, pady=(12, 14))

        def _choose(choice: str) -> None:
            result["choice"] = choice
            dialog.destroy()
            if was_hidden and choice != QUIT_CONFIRMED:
                try:
                    root.withdraw()
                except tk.TclError:
                    pass

        # Escape and the WM close button are Cancel - the dialog must not
        # be dismissible into a real quit.
        dialog.bind("<Escape>", lambda _e: _choose(QUIT_CANCELLED))
        dialog.protocol("WM_DELETE_WINDOW", lambda: _choose(QUIT_CANCELLED))

        def _button(text_key: str, choice: str, bg: str, fg: str) -> None:
            tk.Button(
                dialog,
                text=tr(text_key),
                command=lambda: _choose(choice),
                **btn_colors(bg, fg),
                bd=0,
            ).pack(side="left", padx=6, pady=(0, 12))

        _button(
            "common.cancel",
            QUIT_CANCELLED,
            C["button_head_bg"],
            C["button_head_fg"],
        )
        _button(
            "quit.confirm_hide_to_tray",
            QUIT_HIDE,
            C["button_save_bg"],
            C["button_save_fg"],
        )
        _button(
            "quit.confirm_quit",
            QUIT_CONFIRMED,
            C["button_close_bg"],
            C["button_close_fg"],
        )

        # Cancel holds the initial focus so a stray Enter cancels rather
        # than quitting.  Center on screen like every other dialog in the
        # app (see the withdraw() above), then map and grab in that order.
        center_window(dialog)
        dialog.deiconify()
        dialog.grab_set()
        dialog.focus_set()
        root.wait_window(dialog)
        return result["choice"]
    except Exception:
        if was_hidden:
            try:
                root.withdraw()
            except tk.TclError:
                pass
        logger.exception("Failed to show the quit confirmation dialog")
        return QUIT_CONFIRMED
