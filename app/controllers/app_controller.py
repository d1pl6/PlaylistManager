"""
App-level controller — quit and auth-refresh entry points for the UI.

Quit orchestration lives here so the tkinter layer (MainWindow buttons,
WM_DELETE_WINDOW) never reaches into :class:`App` internals.  If cleanup
or teardown raises, the user is offered a choice between force-quitting
(destroy the window regardless) and cancelling.
"""

import logging
import tkinter as tk

from utils.theme import C

logger = logging.getLogger(__name__)


class AppController:
    def __init__(self, app):
        self.app = app

    def quit_app(self):
        """Run cleanup and teardown, handling errors gracefully.

        If cleanup or closing raises, an error dialog with "Force-quit"
        and "Cancel" buttons is shown.  "Force-quit" skips the remaining
        cleanup and destroys the window anyway; "Cancel" aborts the quit.
        """
        try:
            self.app.cleanup()
        except Exception as e:
            logger.exception("Cleanup failed: %s", e)
            if not self._confirm_force_quit(e):
                logger.info("Quit cancelled by user")
                return

        try:
            self.app.quit_app()
        except Exception as e:
            logger.exception("Failed to close the app: %s", e)
            if not self._confirm_force_quit(e):
                return
            # Last resort — tear the window down directly.
            try:
                self.app.root.destroy()
            except Exception:
                logger.exception("Failed to force-destroy the root window")

    def refresh_auth(self):
        self.app.refresh_auth()

    def _confirm_force_quit(self, error: BaseException) -> bool:
        """Show a Force-quit / Cancel dialog; return True to force-quit."""
        result = {"force": False}

        # If the main window is withdrawn (hidden to tray) the error
        # dialog, being transient to it, can hide behind the tray icon —
        # make the root visible again so the dialog has a parent on screen.
        try:
            if self.app.root.state() != "normal":
                self.app.root.deiconify()
        except tk.TclError:
            pass

        dialog = tk.Toplevel(self.app.root)
        dialog.title("Error")
        dialog.configure(background=C["frame_main_bg"])
        dialog.transient(self.app.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        tk.Label(
            dialog,
            text="An error occurred while closing PlaylistManager:",
            background=C["frame_main_bg"],
            foreground=C["label_def_fg"],
            font=("Noto", 10),
        ).pack(padx=16, pady=(12, 0))

        tk.Label(
            dialog,
            text=str(error),
            background=C["frame_main_bg"],
            foreground=C["label_playlist_error_fg"],
            font=("Noto", 9),
            wraplength=360,
            justify="left",
        ).pack(padx=16, pady=(4, 12))

        def _choose(force: bool) -> None:
            result["force"] = force
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", lambda: _choose(False))

        tk.Button(
            dialog,
            text="Force-quit",
            command=lambda: _choose(True),
            background=C["button_main_bg"],
            foreground=C["button_main_fg"],
            activebackground=C["button_main_a_bg"],
            bd=0,
        ).pack(side="left", padx=(16, 4), pady=(0, 12))

        tk.Button(
            dialog,
            text="Cancel",
            command=lambda: _choose(False),
            background=C["button_head_bg"],
            foreground=C["button_head_fg"],
            activebackground=C["button_head_a_bg"],
            bd=0,
        ).pack(side="left", padx=(4, 16), pady=(0, 12))

        self.app.root.wait_window(dialog)
        return result["force"]
