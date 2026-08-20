import logging
import threading
import tkinter as tk
from typing import Callable, Dict, Optional
from tkinter import messagebox

from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC
from controllers.app_controller import AppController
from controllers.keybind_controller import KeybindController
from services.integration import (
    IntegrationRegistry,
    SpotifyIntegration,
    YouTubeMusicIntegration,
)
from services.playlist_store import PlaylistStore
from services.tray import TrayService
from ui.main_window import MainWindow
from ui.updater_ui import show_update_dialog
from utils.window import center_window
from utils import updater
from utils.config import get_setting
from utils.logging_config import user_log
from utils import scaling
from _version import __version__

logger = logging.getLogger(__name__)


class App:
    def __init__(self, args):
        self.args = args
        self.root = tk.Tk()
        # Must run before any widget exists: picks the ui_scale profile from
        # settings + display Xft.dpi, which every ui_font()/px()/IconService
        # call reads afterwards (see utils/scaling.py).
        scaling.init(self.root)
        # Default font for any widget that doesn't set its own font (entries,
        # dialogs, ...). Under a manual profile TkDefaultFont would keep
        # following the display Xft.dpi while the rest of the UI follows the
        # profile - the option database makes the default follow too.
        self.root.option_add("*Font", scaling.ui_font(10))

        yt_client = None
        youtube_auth = None
        try:
            from integrations.music_youtube.music_youtube import youtube_auth as _yt_auth
            youtube_auth = _yt_auth
            if _yt_auth.setup_auth():
                try:
                    yt_client = _yt_auth.get_yt_music()
                    user_log(logger, "YouTube Music authenticated")
                except Exception as e:
                    logger.error(f"YouTube Music auth failed: {e}")
                    messagebox.showwarning(
                        "YouTube Music",
                        f"YouTube Music authentication failed:\n{e}",
                    )
            else:
                logger.warning("YouTube Music not configured (no browser.json)")
        except ImportError:
            user_log(
                logger,
                "ytmusicapi not installed - YouTube Music integration disabled",
            )

        self.integrations = IntegrationRegistry()
        yt_integration = YouTubeMusicIntegration(auth_manager=youtube_auth)
        yt_integration.yt_client = yt_client
        self.integrations.register(yt_integration)

        # Spotify: setup_auth() verifies the stored credentials against
        # /v1/me - a network round trip with a 15 s timeout.  Running it
        # synchronously here would delay first paint by up to 15 s when
        # the network is slow or offline (same rule as refresh_auth:
        # platform round trips must never block the tkinter main thread).
        # The verification runs on a worker thread; the finished API is
        # swapped in via root.after when it lands.  A login or refresh
        # that completes first wins and is never clobbered.
        spotify_auth = None
        try:
            from integrations.music_spotify.music_spotify import spotify_auth as _sp_auth
            spotify_auth = _sp_auth
        except ImportError:
            user_log(
                logger,
                "Spotify integration unavailable (requests missing)",
            )
        sp_integration = SpotifyIntegration(auth_manager=spotify_auth)
        self.integrations.register(sp_integration)

        def _spotify_auth_worker() -> None:
            ok, api = False, None
            if spotify_auth is not None:
                try:
                    if spotify_auth.setup_auth():
                        api = spotify_auth.get_api()
                        ok = True
                except Exception as e:
                    logger.error(f"Spotify auth failed: {e}")
                if not ok:
                    logger.warning("Spotify is not configured")

            def _apply() -> None:
                if sp_integration.spotify_api is None:
                    # No login/refresh landed in the meantime - safe to swap.
                    sp_integration.spotify_api = api
                if ok:
                    user_log(logger, "Spotify authenticated")

            try:
                self.root.after(0, _apply)
            except Exception:
                # The window closed while verification was in flight; the
                # credentials are picked up on the next launch.
                logger.debug(
                    "Spotify auth finished after the window closed - it "
                    "applies on the next launch"
                )

        threading.Thread(target=_spotify_auth_worker, daemon=True).start()

        keybind_controller = KeybindController(yt_client, spotify_integration=sp_integration)
        app_controller = AppController(self)
        self.ac = app_controller

        # Migrate legacy playlists (without playlist_id) so the new
        # (platform, playlist_id) dedup key works for existing entries.
        # The lookups call the platform APIs (network) - run them in the
        # background so they never block first paint of the UI thread.
        threading.Thread(target=self._migrate_playlist_schema, daemon=True).start()

        self.main_window = MainWindow(
            self.root,
            integrations=self.integrations,
            keybind_controller=keybind_controller,
            app_controller=app_controller,
        )

        PlaylistStore.ensure_playlists_file()
        if get_setting("center_windows", True):
            center_window(self.root)

    def _migrate_playlist_schema(self) -> None:
        """Backfill missing *playlist_id* values in the store.

        Uses the registered integrations to look up playlist IDs by name
        for any legacy entries that lack one.
        """
        if not hasattr(self, "integrations"):
            return

        def lookup(name: str, platform: str) -> str:
            integration = self.integrations.get(platform)
            if integration is None:
                return ""
            try:
                if hasattr(integration, "get_playlist_id"):
                    pid = integration.get_playlist_id(name)
                elif hasattr(integration, "get_playlist_id_by_name"):
                    pid = integration.get_playlist_id_by_name(name)
                else:
                    pid = None
                return pid or ""
            except Exception:
                logger.exception("Migration lookup failed for '%s' (%s)", name, platform)
                return ""

        PlaylistStore.migrate_schema(lookup_playlist_id=lookup)

    def refresh_auth(
        self,
        platform: Optional[str] = None,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Re-authenticate integrations and push fresh clients to the keybind flow.

        With *platform* given (a ``constants.PLATFORM_*`` value) only that
        integration is refreshed - a YouTube Music login event must not
        re-verify (and, on a transient failure, deauthenticate) Spotify,
        and vice versa.  Without it (legacy callers) every integration is
        refreshed.

        *on_done*, when given, is called on the main thread after the
        credential swap has been applied - e.g. the playlist picker uses
        it to retry a fetch that failed because the login refresh raced a
        mid-write ``browser.json``.  It fires whether or not the refresh
        itself succeeded; the caller's retry then fails fast and shows
        the real error.

        The refresh runs on a worker thread: Spotify's re-auth validates
        the credentials against ``/v1/me`` (a network round trip with a
        15 s timeout) and must never block the tkinter main thread.  Only
        the final credential swap (``update_credentials``) and *on_done*
        are marshaled back to the main thread via ``root.after``.
        """
        integrations = self.integrations.get_all()
        if platform is not None:
            if platform not in integrations:
                logger.error(f"Unknown platform '{platform}' - cannot refresh")
                return
            targets = [integrations[platform]]
        else:
            targets = list(integrations.values())

        def _refresh_worker() -> None:
            for integration in targets:
                try:
                    ok = integration.refresh_auth()
                except Exception:
                    logger.exception(
                        "refresh_auth failed for %s", integration.display_name
                    )
                    ok = False
                if ok:
                    user_log(logger, "%s re-authenticated", integration.display_name)
                else:
                    logger.error(f"{integration.display_name} re-authentication failed")

            # update_credentials treats a missing kwarg as "leave untouched"
            # and an explicit None as "clear" - so a scoped refresh must only
            # pass the integration that was actually refreshed.
            kwargs: Dict[str, object] = {}
            if platform is None or platform == PLATFORM_YOUTUBE_MUSIC:
                yt = self.integrations.get(PLATFORM_YOUTUBE_MUSIC)
                if isinstance(yt, YouTubeMusicIntegration):
                    kwargs["yt_client"] = yt.yt_client
            if platform is None or platform == PLATFORM_SPOTIFY:
                sp = self.integrations.get(PLATFORM_SPOTIFY)
                if isinstance(sp, SpotifyIntegration):
                    kwargs["spotify_integration"] = sp

            def _apply() -> None:
                self.main_window.kc.update_credentials(**kwargs)
                if on_done is not None:
                    try:
                        on_done()
                    except Exception:
                        logger.exception(
                            "on_done callback failed after auth refresh"
                        )

            try:
                self.root.after(0, _apply)
            except Exception:
                # The app quit while the refresh was in flight - the new
                # credentials are picked up on the next launch.
                logger.debug(
                    "Refresh finished after the window closed - it applies "
                    "on the next launch"
                )

        threading.Thread(target=_refresh_worker, daemon=True).start()

    def _check_updates(self, *, force: bool = False, on_done=None):
        """Check GitHub for a newer release.

        Args:
            force: When *False* the check honours the user's
                ``update_check`` INI toggle; when *True* it always runs
                (manual "Check for updates now" button).
            on_done: Optional callback ``f(available, error)`` marshalled
                to the main thread after the check completes.  The first
                positional arg is a bool (``True`` when an update is
                available), the second is *None* or an error string.
                Used by the settings dialog to show "Up to date!" / error
                feedback and to re-enable the button.
        """
        def on_result(available, latest_version=None, download_url=None, body=None, error=None):
            if available:
                user_log(logger, "Update v%s available at %s", latest_version, download_url)
                try:
                    self.root.after(0, show_update_dialog, self.root, latest_version, download_url, body)
                except Exception:
                    # Check finished before mainloop() started, or the app
                    # quit while the request was in flight.  Best-effort -
                    # the updater thread must never raise uncaught.
                    logger.debug("Update dialog not shown: %s", "mainloop not running or app shutting down")
            elif error:
                # No modal: an offline/blocked network at startup would pop
                # an unavoidable dialog on every launch.  USER level keeps
                # it visible in normal runs without stealing focus.
                user_log(logger, "Update check failed: %s", error)

            if on_done:
                try:
                    self.root.after(0, on_done, available, error)
                except Exception:
                    pass

        updater.check(on_result, force=force)

    def _start_tray(self):
        """Start the system tray icon (best effort).

        Both callbacks hop through ``root.after(0, ...)`` because the tray
        backend thread must never touch tkinter directly.  ``after`` may
        be called before ``mainloop()`` starts - timers simply queue and
        fire once the loop runs.
        """
        tray = TrayService()
        if not tray.available:
            user_log(logger, "Tray unavailable - hide-to-tray disabled")
            self.main_window.tray_service = None
            return

        def _tray_after(fn) -> None:
            """Marshal a tray callback to the main thread.

            Tray callbacks fire on the tray backend thread; ``after``
            raises if the root is already destroyed (quit raced a
            last-second tray click) and the exception would otherwise
            surface in the backend's thread.
            """
            try:
                self.root.after(0, fn)
            except Exception:
                logger.debug("App is shutting down; dropped tray callback")

        try:
            tray.start(
                on_open=lambda: _tray_after(self.main_window.show_from_tray),
                on_quit=lambda: _tray_after(self.ac.quit_app),
            )
        except Exception:
            # Best-effort feature - a backend quirk must not kill the app.
            logger.exception("Tray failed to start - hide-to-tray disabled")
            self.main_window.tray_service = None
            return
        self._tray_service = tray
        self.main_window.tray_service = tray

    def run(self):
        logger.info("Starting app")
        try:
            self.main_window.setup()
            # setup() restores playlist frames, and auto-resize may have
            # grown the window past the 650x460 geometry that __init__
            # centered - re-center now that the initial size has settled.
            if get_setting("center_windows", True):
                center_window(self.root)
            self._start_tray()
            # Kick off the update check here, immediately before the
            # mainloop starts: launched from __init__, a fast network could
            # complete the check before mainloop() ran, and the marshaled
            # root.after(0, ...) would raise "main thread is not in main
            # loop" - dropping the update dialog on the fastest startups.
            self._check_updates()
            self.root.mainloop()
        except Exception:
            logger.exception("Unhandled exception")
            raise

    def cleanup(self):
        """Best-effort teardown of listeners, receiver, and UI widgets.

        Called by :class:`AppController` before quitting.  Raises the
        first error so the controller can offer Force-quit / Cancel.
        """
        # Stop the tray first so no tray callback can fire against a
        # destroyed root.  Icon.stop() is non-blocking (unlike the pynput
        # listener) - no join quirk.
        tray = getattr(self, "_tray_service", None)
        if tray is not None:
            tray.stop()
        if hasattr(self, "main_window") and self.main_window:
            self.main_window.kc.stop_receiver()
            self.main_window.kc.stop_listener(wait=False)
            self.main_window.cleanup()

    def quit_app(self):
        logger.info("Stopping app")
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            logger.exception("Failed to close the root window")
            raise
