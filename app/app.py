import logging
import threading
import tkinter as tk
from configparser import ConfigParser
from pathlib import Path
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
from utils.config import SETTINGS_PATH, ensure_settings_file
from _version import __version__

logger = logging.getLogger(__name__)


class App:
    def __init__(self, args):
        self.args = args
        self.root = tk.Tk()

        yt_client = None
        youtube_auth = None
        try:
            from integrations.music_youtube.music_youtube import youtube_auth as _yt_auth
            youtube_auth = _yt_auth
            if _yt_auth.setup_auth():
                try:
                    yt_client = _yt_auth.get_yt_music()
                    logger.info("YouTube Music authenticated")
                except Exception as e:
                    logger.error(f"YouTube Music auth failed: {e}")
                    messagebox.showwarning(
                        "YouTube Music",
                        f"YouTube Music authentication failed:\n{e}",
                    )
            else:
                logger.warning("YouTube Music not configured (no browser.json)")
        except ImportError:
            logger.info(
                "ytmusicapi not installed — YouTube Music integration disabled"
            )

        self.integrations = IntegrationRegistry()
        yt_integration = YouTubeMusicIntegration(auth_manager=youtube_auth)
        yt_integration.yt_client = yt_client
        self.integrations.register(yt_integration)

        sp_api = None
        spotify_auth = None
        try:
            from integrations.music_spotify.music_spotify import spotify_auth as _sp_auth
            spotify_auth = _sp_auth
            if _sp_auth.setup_auth():
                sp_api = _sp_auth.get_api()
                logger.info("Spotify authenticated")
            else:
                logger.warning("Spotify not configured")
        except Exception as e:
            logger.error(f"Spotify auth failed: {e}")
            messagebox.showwarning("Spotify", f"Spotify authentication failed:\n{e}")
        sp_integration = SpotifyIntegration(auth_manager=spotify_auth)
        sp_integration.spotify_api = sp_api
        self.integrations.register(sp_integration)

        keybind_controller = KeybindController(yt_client, spotify_integration=sp_integration)
        app_controller = AppController(self)
        self.ac = app_controller

        # Migrate legacy playlists (without playlist_id) so the new
        # (platform, playlist_id) dedup key works for existing entries.
        # The lookups call the platform APIs (network) — run them in the
        # background so they never block first paint of the UI thread.
        threading.Thread(target=self._migrate_playlist_schema, daemon=True).start()

        self.main_window = MainWindow(
            self.root,
            integrations=self.integrations,
            keybind_controller=keybind_controller,
            app_controller=app_controller,
        )

        ensure_settings_file()
        cfg = ConfigParser()
        cfg.read(str(SETTINGS_PATH))
        if cfg.getboolean("center_windows", "is_true", fallback=True):
            center_window(self.root)
        self._check_updates()

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

    def refresh_auth(self):
        for integration in self.integrations.get_all().values():
            if integration.refresh_auth():
                logger.info(f"{integration.display_name} re-authenticated")
            else:
                logger.error(f"{integration.display_name} re-authentication failed")

        yt_client = None
        spotify_integration = None

        yt = self.integrations.get(PLATFORM_YOUTUBE_MUSIC)
        if isinstance(yt, YouTubeMusicIntegration):
            yt_client = yt.yt_client
        sp = self.integrations.get(PLATFORM_SPOTIFY)
        if isinstance(sp, SpotifyIntegration):
            spotify_integration = sp

        self.main_window.kc.update_credentials(
            yt_client=yt_client, spotify_integration=spotify_integration
        )

    def _check_updates(self):
        def on_result(
            available, latest_version=None, download_url=None, body=None, error=None
        ):
            if available:
                logger.info("Update v%s available at %s", latest_version, download_url)
                self.root.after(
                    0, show_update_dialog, self.root, latest_version, download_url, body
                )
            elif error:
                logger.warning(error)
                self.root.after(0, messagebox.showwarning, "Update Check Failed", error)

        updater.check(on_result)

    def _start_tray(self):
        """Start the system tray icon (best effort).

        Both callbacks hop through ``root.after(0, ...)`` because the tray
        backend thread must never touch tkinter directly.  ``after`` may
        be called before ``mainloop()`` starts — timers simply queue and
        fire once the loop runs.
        """
        tray = TrayService()
        if not tray.available:
            logger.info("Tray unavailable — hide-to-tray disabled")
            self.main_window.tray_service = None
            return
        try:
            tray.start(
                on_open=lambda: self.root.after(0, self.main_window.show_from_tray),
                on_quit=lambda: self.root.after(0, self.ac.quit_app),
            )
        except Exception:
            # Best-effort feature — a backend quirk must not kill the app.
            logger.exception("Tray failed to start — hide-to-tray disabled")
            self.main_window.tray_service = None
            return
        self._tray_service = tray
        self.main_window.tray_service = tray

    def run(self):
        logger.info("Starting app")
        try:
            self.main_window.setup()
            self._start_tray()
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
        # listener) — no join quirk.
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
