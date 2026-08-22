import logging
import threading
import tkinter as tk
from typing import Callable, Optional
from tkinter import messagebox

from controllers.app_controller import AppController
from controllers.keybind_controller import KeybindController
from plugin_loader import PluginRegistry
from services.integration import IntegrationRegistry
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

        # Plugin discovery scans integrations/*/plugin.json - no plugin
        # Python is imported here; every class reference below resolves
        # lazily inside its try/except so one broken plugin (or a missing
        # optional dependency) cannot take down startup or the other
        # platforms.
        self.plugin_registry = PluginRegistry().discover()

        self.integrations = IntegrationRegistry()
        for pid, plugin in self.plugin_registry.get_all().items():
            try:
                integration_cls = plugin.import_integration()
            except ImportError as e:
                # Optional dependency of this plugin is missing - degrade
                # gracefully exactly like the pre-plugin code did.
                user_log(
                    logger,
                    "%s integration unavailable (%s)",
                    plugin.display_name,
                    e,
                )
                continue
            except Exception as e:
                logger.warning("Plugin %s failed to load: %s", pid, e)
                continue

            auth_manager = None
            if plugin.auth_module:
                try:
                    auth_manager = plugin.import_auth_attr()
                except ImportError as e:
                    user_log(
                        logger,
                        "%s integration unavailable (%s)",
                        plugin.display_name,
                        e,
                    )
                    continue
                except Exception as e:
                    logger.warning(
                        "Plugin %s: auth manager unavailable (%s)", pid, e
                    )
                    continue

            integration = integration_cls(auth_manager=auth_manager)
            self.integrations.register(integration)

        self._bootstrap_auth()

        keybind_controller = KeybindController(self.plugin_registry, self.integrations)
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

    def _bootstrap_auth(self) -> None:
        """Initial authentication for every registered integration.

        Auth logic stays platform-specific in Step 1 (see 0.3.0/step1.md) -
        each integration owns its auth manager; this only wires them up.
        YouTube Music's setup_auth() reads local files (fast -> sync);
        Spotify's verifies credentials against /v1/me - a network round
        trip with a 15 s timeout.  Running that synchronously would delay
        first paint by up to 15 s when the network is slow or offline
        (same rule as refresh_auth: platform round trips must never block
        the tkinter main thread).  The verification runs on a worker
        thread; the finished API is swapped in via root.after when it
        lands.  A login or refresh that completes first wins and is never
        clobbered.
        """
        yt_integration = self.integrations.get("youtube_music")
        if yt_integration is not None and yt_integration._auth is not None:
            try:
                if yt_integration.authenticate():
                    user_log(logger, "YouTube Music authenticated")
                else:
                    logger.warning("YouTube Music not configured (no browser.json)")
            except ImportError as e:
                # setup_auth imports ytmusicapi lazily - a missing optional
                # dependency disables the integration quietly (log, no modal).
                user_log(
                    logger,
                    "ytmusicapi not installed - YouTube Music integration "
                    "disabled (%s)",
                    e,
                )
            except Exception as e:
                yt_integration.yt_client = None
                logger.error(f"YouTube Music auth failed: {e}")
                messagebox.showwarning(
                    "YouTube Music",
                    f"YouTube Music authentication failed:\n{e}",
                )

        sp_integration = self.integrations.get("spotify")
        sp_auth = sp_integration._auth if sp_integration is not None else None
        if sp_integration is None:
            return

        def _spotify_auth_worker() -> None:
            ok, api = False, None
            if sp_auth is not None:
                try:
                    if sp_auth.setup_auth():
                        api = sp_auth.get_api()
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
        """Re-authenticate integrations and invalidate the affected flows.

        With *platform* given (a plugin id, e.g. ``"youtube_music"``) only
        that integration is refreshed - a YouTube Music login event must
        not re-verify (and, on a transient failure, deauthenticate)
        Spotify, and vice versa.  Without it (legacy callers) every
        integration is refreshed.

        *on_done*, when given, is called on the main thread after the
        credential swap has been applied - e.g. the playlist picker uses
        it to retry a fetch that failed because the login refresh raced a
        mid-write ``browser.json``.  It fires whether or not the refresh
        itself succeeded; the caller's retry then fails fast and shows
        the real error.

        The refresh runs on a worker thread: Spotify's re-auth validates
        the credentials against ``/v1/me`` (a network round trip with a
        15 s timeout) and must never block the tkinter main thread.  Only
        the flow invalidation (``update_credentials``) and *on_done* are
        marshaled back to the main thread via ``root.after``.
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
            # Only platforms whose refresh SUCCEEDED are invalidated -
            # their flows are rebuilt lazily with the fresh clients.  A
            # platform that failed keeps its existing (still working)
            # flow instead of being disabled until the next launch.
            refreshed_ids = []
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
                    refreshed_ids.append(integration.id)
                else:
                    logger.error(f"{integration.display_name} re-authentication failed")

            def _apply() -> None:
                self.main_window.kc.update_credentials(refreshed_ids=refreshed_ids)
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
