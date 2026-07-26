import logging

logger = logging.getLogger(__name__)


class AppController:
    def __init__(self, app):
        self.app = app

    def quit_app(self):
        self.app.quit_app()

    def refresh_auth(self):
        self.app.refresh_auth()
