"""
Flask-based HTTP receiver for YouTube Music URLs from the browser extension.

All Flask imports are **lazy** — they happen only when the receiver is
actually started (at most ~30 s per keybind press).
"""

import logging
import re
import threading
import time
from typing import Optional
from queue import Queue, Empty

logger = logging.getLogger(__name__)


class _RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._timestamps = [t for t in self._timestamps if now - t < self.window_seconds]
            if len(self._timestamps) >= self.max_requests:
                return False
            self._timestamps.append(now)
            return True


class URLReceiverManager:
    """
    Manages a Flask server for receiving YouTube Music URLs from a browser extension.

    Protocol:
      1. Flow controller calls start() + set_waiting(True) when keybind is pressed.
      2. Extension polls GET /status -> {"ready": true} while server is up.
      3. Extension POSTs URL to /receive-url once.
      4. Flow controller calls set_waiting(False), retrieves URL from queue, stops server.

    The server is short-lived (up to ~30 s per keybind press) and binds to
    localhost only, so plain HTTP is acceptable.
    """

    YT_MUSIC_URL_PATTERN = r"https://music\.youtube\.com/watch\?v=([\w-]+)"

    def __init__(self, host: str = "localhost", port: int = 5000, timeout: int = 30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.url_queue: Queue = Queue()
        self.app = None   # created lazily by _ensure_app()
        self.thread: Optional[threading.Thread] = None
        self._server = None
        self._running = False
        self._waiting_for_url = False
        self._state_lock = threading.Lock()
        self._rate_limiter = _RateLimiter(max_requests=10, window_seconds=60)
        # NOTE: _ensure_app() is NOT called here — Flask is imported lazily.

    def _ensure_app(self):
        """Lazy initialisation of the Flask application and routes."""
        if self.app is not None:
            return

        from flask import Flask, request, jsonify
        from flask_cors import CORS
        from werkzeug.serving import make_server

        self._make_server = make_server

        app = Flask(__name__)
        CORS(
            app,
            resources={
                r"/*": {
                    "origins": "*",
                    "methods": ["GET", "POST", "OPTIONS"],
                    "allow_headers": ["Content-Type"],
                }
            },
        )

        @app.route("/status", methods=["GET"])
        def _status():
            """Extension polls this to know when to send a URL."""
            return jsonify({"ready": self._waiting_for_url})

        @app.route("/receive-url", methods=["POST", "OPTIONS"])
        def _receive_url():
            """Endpoint to receive YouTube Music URLs."""
            if request.method == "OPTIONS":
                return "", 200

            if not self._rate_limiter.is_allowed():
                return jsonify({"error": "Rate limit exceeded. Try again later."}), 429

            try:
                data = request.get_json()
                url = data.get("url", "").strip() if data else ""

                if not url:
                    return jsonify({"error": "No URL provided"}), 400

                video_id = self._extract_video_id(url)
                if video_id is None:
                    return jsonify({"error": "Invalid YouTube Music URL"}), 400

                with self._state_lock:
                    self._waiting_for_url = False
                    self.url_queue.put(url)

                logger.debug(f"Received valid YouTube Music URL: {video_id}")

                return (
                    jsonify(
                        {
                            "success": True,
                            "message": "URL received successfully",
                            "video_id": video_id,
                        }
                    ),
                    200,
                )

            except Exception as e:
                logger.error(f"Error in receive_url endpoint: {e}")
                return jsonify({"error": "Internal server error"}), 500

        self.app = app

    @staticmethod
    def _extract_video_id(url: str) -> str | None:
        """Validate and extract the video ID from a YouTube Music URL.

        Returns the video ID string, or None if the URL is not a valid
        YouTube Music watch URL.
        """
        match = re.match(URLReceiverManager.YT_MUSIC_URL_PATTERN, url)
        if not match:
            return None
        return match.group(1)

    def set_waiting(self, waiting: bool) -> None:
        """Control whether the /status endpoint reports ready."""
        with self._state_lock:
            self._waiting_for_url = waiting

    def start(self) -> Optional[threading.Thread]:
        """Start the Flask server in a daemon thread."""
        if self._running:
            logger.warning("URLReceiverManager is already running")
            return self.thread

        self._ensure_app()

        try:
            self._server = self._make_server(
                self.host, self.port, self.app, threaded=True
            )
            server = self._server
            self._running = True

            def run_flask():
                try:
                    server.serve_forever()
                except Exception as e:
                    logger.error(f"Flask server error: {e}")
                finally:
                    self._running = False

            self.thread = threading.Thread(target=run_flask, daemon=True)
            self.thread.start()
            logger.info(f"Started URL receiver on {self.host}:{self.port}")

            return self.thread

        except Exception as e:
            logger.error(f"Failed to start URL receiver: {e}")
            self._running = False
            raise

    def stop(self) -> None:
        """Stop the Flask server gracefully."""
        if not self._running:
            return

        with self._state_lock:
            self._waiting_for_url = False
        try:
            if self._server:
                self._server.shutdown()
                self._server = None
            self._running = False
            logger.info("Stopped URL receiver")
        except Exception as e:
            logger.error(f"Error stopping URL receiver: {e}")

    def get_received_url(self, timeout: Optional[int] = None) -> str:
        """Get the received URL from the queue.

        Args:
            timeout: Seconds to wait. Uses self.timeout if not specified.

        Raises:
            TimeoutError: If no URL received within timeout period.
        """
        if timeout is None:
            timeout = self.timeout

        try:
            url = self.url_queue.get(timeout=timeout)
            logger.debug("Retrieved URL from queue")
            return url
        except Empty:
            logger.warning(f"Timeout waiting for URL after {timeout} seconds")
            raise TimeoutError(f"No URL received within {timeout} seconds")

    def is_running(self) -> bool:
        return self._running
