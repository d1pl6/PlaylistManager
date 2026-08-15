"""
App-level controller - quit and auth-refresh entry points for the UI.

Quit orchestration lives here so the tkinter layer (MainWindow buttons,
WM_DELETE_WINDOW) never reaches into :class:`App` internals.  If cleanup
or teardown raises, the user is offered a choice between force-quitting
(destroy the window regardless) and cancelling.
"""

import logging
import tkinter as tk
from typing import Callable, Optional

from utils.scaling import px, ui_font
from utils.theme import C, btn_colors

logger = logging.getLogger(__name__)


class AppController:
    def __init__(self, app):
        self.app = app
        # Guards against re-entrant quit requests.  There are two quit
        # sources (WM_DELETE_WINDOW and the tray "Quit" item) and both
        # can be delivered in the same event-loop window, or a second
        # request can arrive while the Force-quit dialog is open
        # (wait_window re-enters the mainloop).  Without the guard the
        # second call re-runs teardown and root.destroy() on an already
        # destroyed window raises TclError that escapes _confirm_force_quit.
        self._quitting = False

    def quit_app(self):
        """Run cleanup and teardown, handling errors gracefully.

        If cleanup or closing raises, an error dialog with "Force-quit"
        and "Cancel" buttons is shown.  "Force-quit" skips the remaining
        cleanup and destroys the window anyway; "Cancel" aborts the quit
        and leaves the app running (the guard is released so a later
        quit request is honoured).
        """
        if self._quitting:
            logger.debug("Quit already in progress, ignoring duplicate request")
            return
        self._quitting = True

        try:
            self.app.cleanup()
        except Exception as e:
            logger.exception("Cleanup failed: %s", e)
            if not self._confirm_force_quit(e):
                self._quitting = False
                logger.info("Quit cancelled by user")
                return

        try:
            self.app.quit_app()
        except Exception as e:
            logger.exception("Failed to close the app: %s", e)
            # By this point cleanup() has already run (or the user chose
            # Force-quit over its failure), so there is nothing left to
            # cancel: listeners are stopped, frames destroyed and DB
            # connections closed.  Offering Cancel would strand the app
            # in that half-torn-down state - tear the window down
            # directly instead.
            try:
                self.app.root.destroy()
            except Exception:
                logger.exception("Failed to force-destroy the root window")

    def refresh_auth(
        self,
        platform: Optional[str] = None,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Re-authenticate after a login/credentials change.

        *platform* (a ``constants.PLATFORM_*`` value) scopes the refresh
        to the integration that actually changed, so an unrelated
        platform's transient network failure cannot deauthenticate it.

        *on_done* is invoked on the main thread once the new credentials
        have been applied - used by the playlist picker to retry a fetch
        that raced the login.
        """
        self.app.refresh_auth(platform, on_done)

    def _confirm_force_quit(self, error: BaseException) -> bool:
        """Show a Force-quit / Cancel dialog; return True to force-quit.

        If the root window is already destroyed there is nothing left to
        cancel (and nowhere to host a dialog), so force-quit is the only
        meaningful outcome.
        """
        try:
            if not self.app.root.winfo_exists():
                return True
        except tk.TclError:
            return True

        result = {"force": False}

        # If the main window is withdrawn (hidden to tray) the error
        # dialog, being transient to it, can hide behind the tray icon -
        # make the root visible again so the dialog has a parent on screen.
        try:
            if self.app.root.state() != "normal":
                self.app.root.deiconify()
        except tk.TclError:
            pass

        try:
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
                text="Force-quit",
                command=lambda: _choose(True),
                **btn_colors(C["button_main_bg"], C["button_main_fg"]),
                bd=0,
            ).pack(side="left", padx=(16, 4), pady=(0, 12))

            tk.Button(
                dialog,
                text="Cancel",
                command=lambda: _choose(False),
                **btn_colors(C["button_head_bg"], C["button_head_fg"]),
                bd=0,
            ).pack(side="left", padx=(4, 16), pady=(0, 12))

            self.app.root.wait_window(dialog)
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
