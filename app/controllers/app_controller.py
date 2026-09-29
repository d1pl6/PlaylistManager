"""
App-level controller - quit and auth-refresh entry points for the UI.

Quit orchestration lives here so the tkinter layer (MainWindow buttons,
WM_DELETE_WINDOW) never reaches into :class:`App` internals.  If cleanup
or teardown raises, the user is offered a choice between force-quitting
(destroy the window regardless) and cancelling — the dialog itself lives
in :mod:`ui.dialogs` so the controller stays free of widget code.
"""

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def should_confirm_quit(
    confirm_on_quit: bool, hide_to_tray: bool, tray_available: bool
) -> bool:
    """Whether a quit request must be confirmed with the user first.

    Pure decision function (headless-testable): the prompt only appears
    when all three hold —

    - ``confirm_on_quit``: the setting is on,
    - ``hide_to_tray``: the user opted into hide-to-tray, so a full exit
      while the icon sits in the tray is plausibly accidental,
    - ``tray_available``: a tray backend actually started, so the dialog
      can offer "Hide to tray" as a real alternative to quitting.

    With hide-to-tray off (the default) or unavailable, quitting stays a
    single action and a Cancel/Quit-only prompt would be pure friction.
    """
    return bool(confirm_on_quit and hide_to_tray and tray_available)


class AppController:
    def __init__(self, app):
        self.app = app
        # Guards against re-entrant quit requests.  There are two quit
        # sources (WM_DELETE_WINDOW and the tray "Quit" item) and both
        # can be delivered in the same event-loop window, or a second
        # request can arrive while the Force-quit dialog is open
        # (wait_window re-enters the mainloop).  Without the guard the
        # second call re-runs teardown and root.destroy() on an already
        # destroyed window raises TclError that escapes the dialog.
        # The quit-CONFIRMATION dialog is openable too, so the guard is
        # taken before it and released again if the user cancels or hides
        # to tray - otherwise a second request (or a tray click landing
        # while the modal is up) would stack a second dialog.
        self._quitting = False

    def quit_app(self):
        """Run cleanup and teardown, handling errors gracefully.

        When hide-to-tray is available and ``[confirm_on_quit]`` is on,
        the user first gets a Cancel / Hide to tray / Quit dialog; "Hide
        to tray" aborts the quit (the window is withdrawn) and returns.

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

        if not self._confirm_quit():
            self._quitting = False
            return

        try:
            self.app.cleanup()
        except Exception as e:
            logger.exception("Cleanup failed: %s", e)
            if not self._confirm_force_quit(self.app, e):
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

    def _confirm_quit(self) -> bool:
        """Ask the user how to quit; return True to proceed with the quit.

        Returns True immediately when the prompt does not apply (see
        :func:`should_confirm_quit`) so the no-tray / setting-off paths
        behave exactly as before.  "Cancel" and "Hide to tray" both abort
        the quit; the hide path withdraws the window through the UI layer
        (``MainWindow.hide_to_tray``) and falls back to a real quit if
        that fails - the user asked to keep the app running, but a window
        withdrawn with no tray icon would be unreachable.
        """
        from utils.config import get_setting

        tray = getattr(getattr(self.app, "main_window", None), "tray_service", None)
        tray_available = bool(getattr(tray, "available", False))
        if not should_confirm_quit(
            get_setting("confirm_on_quit", True),
            get_setting("hide_to_tray", False),
            tray_available,
        ):
            return True

        from ui.dialogs import (
            QUIT_CANCELLED,
            QUIT_CONFIRMED,
            QUIT_HIDE,
            confirm_quit,
        )

        choice = confirm_quit(self.app.root)
        if choice == QUIT_CANCELLED:
            logger.info("Quit cancelled by user")
            return False
        if choice == QUIT_HIDE:
            if self.app.main_window.hide_to_tray():
                logger.info("Quit cancelled - window hidden to the tray")
                return False
            logger.warning("Hide-to-tray failed - quitting instead")
            return True
        return choice == QUIT_CONFIRMED

    def check_updates(self, *, force: bool = True, on_done=None) -> None:
        """Check for application updates and show a dialog when one is found."""
        self.app.check_updates(force=force, on_done=on_done)

    def restart_app(self) -> None:
        """Cleanly quit and re-launch the app.

        Used by the profile switch flow: the new active profile is read
        at import time, so a profile change only takes effect after a
        full restart.  This reloads everything against the new profile's
        paths (settings, theme, playlists, auth).

        Re-launch mirrors how the user started the app: ``sys.executable
        main.py`` when they ran it directly, otherwise the global
        ``playlistmanager`` command.  The subprocess is spawned *before*
        the window is destroyed so that a compositor-side close binding
        (e.g. Hyprland's ``hl.dsp.window.close()``) or a Wayland window
        manager tearing down the surface cannot prevent the relaunch —
        the child process is already running in its own session.  If
        ``Popen`` fails the error is logged but the app is left running
        (the window still exists at that point).
        """
        import subprocess
        import sys
        from pathlib import Path

        # Run teardown first (pynput listener, receiver, thread-cached
        # SQLite conns) so nothing from the old profile survives.
        try:
            self.app.cleanup()
        except Exception:
            logger.exception("Cleanup failed during restart")
        try:
            self.app.root.update_idletasks()
        except Exception:
            pass

        root_path = Path(__file__).resolve().parents[2]
        main_py = root_path / "main.py"
        if main_py.exists():
            cmd = [sys.executable, str(main_py)]
        else:
            # Fall back to the user-installed global command.
            cmd = ["playlistmanager"]

        # Spawn the child first.  start_new_session=True keeps the
        # child alive if the parent dies or a compositor close action
        # arrives between Popen and root.destroy().
        try:
            subprocess.Popen(
                cmd,
                cwd=str(root_path),
                start_new_session=True,
            )
        except OSError as e:
            logger.error("Failed to restart the app: %s", e)
            return  # window still exists — leave the app running

        try:
            self.app.root.destroy()
        except Exception:
            pass

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

    @staticmethod
    def _confirm_force_quit(app, error: BaseException) -> bool:
        """Show a Force-quit / Cancel dialog; return True to force-quit.

        Delegates to :func:`ui.dialogs.confirm_force_quit` so the
        controller layer never touches tkinter widgets directly.
        """
        from ui.dialogs import confirm_force_quit
        return confirm_force_quit(app.root, error)
