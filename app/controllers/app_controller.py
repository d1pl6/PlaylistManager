"""
App-level controller — quit and auth-refresh entry points for the UI.

Quit orchestration lives here so the tkinter layer (MainWindow buttons,
WM_DELETE_WINDOW) never reaches into :class:`App` internals.  If cleanup
or teardown raises, the user is offered a choice between force-quitting
(destroy the window regardless) and cancelling.
"""

import logging
import tkinter as tk

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

        dialog = tk.Toplevel(self.app.root)
        dialog.title("Error")
        dialog.configure(background="#252525")
        dialog.transient(self.app.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        tk.Label(
            dialog,
            text="An error occurred while closing PlaylistManager:",
            background="#252525",
            foreground="#EDEDED",
            font=("Noto", 10),
        ).pack(padx=16, pady=(12, 0))

        tk.Label(
            dialog,
            text=str(error),
            background="#252525",
            foreground="#FF8080",
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
            background="#6C6C6C",
            foreground="#FFFFFF",
            activebackground="#868686",
            bd=0,
        ).pack(side="left", padx=(16, 4), pady=(0, 12))

        tk.Button(
            dialog,
            text="Cancel",
            command=lambda: _choose(False),
            background="#3A3A3A",
            foreground="#D7D7D7",
            activebackground="#555555",
            bd=0,
        ).pack(side="left", padx=(4, 16), pady=(0, 12))

        self.app.root.wait_window(dialog)
        return result["force"]
