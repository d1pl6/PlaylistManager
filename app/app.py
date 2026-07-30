import logging
import tkinter as tk
from configparser import ConfigParser
from pathlib import Path
from tkinter import messagebox

from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC
from controllers.app_controller import AppController
from controllers.keybind_controller import KeybindController
from integrations.music_spotify.music_spotify import spotify_auth
from integrations.music_youtube.music_youtube import youtube_auth
from services.integration import (
    IntegrationRegistry,
    SpotifyIntegration,
    YouTubeMusicIntegration,
)
from services.playlist_store import PlaylistStore
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
        if youtube_auth.setup_auth():
            try:
                yt_client = youtube_auth.get_yt_music()
                logger.info("YouTube Music authenticated")
            except Exception as e:
                logger.error(f"YouTube Music auth failed: {e}")
                messagebox.showwarning("YouTube Music", f"YouTube Music authentication failed:\n{e}")
        else:
            logger.warning("YouTube Music not configured")

        self.integrations = IntegrationRegistry()
        yt_integration = YouTubeMusicIntegration()
        yt_integration.yt_client = yt_client
        self.integrations.register(yt_integration)

        sp_api = None
        try:
            if spotify_auth.setup_auth():
                sp_api = spotify_auth.get_api()
                logger.info("Spotify authenticated")
            else:
                logger.warning("Spotify not configured")
        except Exception as e:
            logger.error(f"Spotify auth failed: {e}")
            messagebox.showwarning("Spotify", f"Spotify authentication failed:\n{e}")
        sp_integration = SpotifyIntegration()
        sp_integration.spotify_api = sp_api
        self.integrations.register(sp_integration)

        keybind_controller = KeybindController(yt_client, spotify_integration=sp_integration)
        app_controller = AppController(self)

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

    def run(self):
        logger.info("Starting app")
        try:
            self.main_window.setup()
            self.root.mainloop()
        except Exception:
            logger.exception("Unhandled exception")
            raise

    def quit_app(self):
        logger.info("Stopping app")
        try:
            if hasattr(self, "main_window") and self.main_window:
                self.main_window.kc.stop_receiver()
                self.main_window.kc.stop_listener()
                self.main_window.cleanup()
        finally:
            self.root.quit()
            self.root.destroy()
