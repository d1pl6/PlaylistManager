import logging
import tkinter as tk
from tkinter import messagebox
from configparser import ConfigParser
from pathlib import Path

from ui.main_window import MainWindow
from ui.updater_ui import show_update_dialog
from controllers.app_controller import AppController
from controllers.keybind_controller import KeybindController
from services.playlist_store import PlaylistStore
from services.playlist_service import PlaylistService
from integrations.music_youtube.music_youtube import youtube_auth
from integrations.music_spotify.music_spotify import spotify_auth
from services.integration import IntegrationRegistry, YouTubeMusicIntegration, SpotifyIntegration
from utils import updater, center_window
from utils.config import ensure_settings_file, SETTINGS_PATH
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

        playlist_store = PlaylistStore()
        playlist_service = PlaylistService(yt_client)
        keybind_controller = KeybindController(yt_client, spotify_api=sp_api)
        app_controller = AppController(self)

        self.main_window = MainWindow(
            self.root,
            integrations=self.integrations,
            playlist_service=playlist_service,
            playlist_store=playlist_store,
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
        yt = self.integrations.get("youtube_music")
        if isinstance(yt, YouTubeMusicIntegration):
            self.main_window.ps.yt = yt.yt_client
            self.main_window.kc.yt = yt.yt_client
            self.main_window.kc.keybind_flow = None
        sp = self.integrations.get("spotify")
        if isinstance(sp, SpotifyIntegration):
            self.main_window.kc.spotify_api = sp.spotify_api
            self.main_window.kc.spotify_flow = None

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
