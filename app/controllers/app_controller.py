import logging

logger = logging.getLogger(__name__)


class AppController:
    def __init__(self, app):
        self.app = app

    def quit_app(self):
        self.app.quit_app()

    def refresh_auth(self):
        self.app.refresh_auth()

    def cleanup_main_window(self, main_window):
        if not hasattr(main_window, "frames"):
            return
        try:
            main_window.img_refs.clear()
            main_window.frame_img_refs.clear()
            if hasattr(main_window, "active_log_labels"):
                main_window.active_log_labels.clear()
            for frame in main_window.frames:
                try:
                    frame.grid_forget()
                    frame.destroy()
                except Exception as e:
                    logger.warning(f"Error destroying frame: {e}")
            main_window.frames.clear()
            main_window.frame_positions.clear()
            main_window.playlist_name_labels.clear()
            logger.debug("MainWindow cleanup completed")
        except Exception as e:
            logger.error(f"Error during MainWindow cleanup: {e}")
