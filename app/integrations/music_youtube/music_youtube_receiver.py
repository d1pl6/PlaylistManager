"""
Flask-based HTTP receiver for YouTube Music URLs from the browser extension.

All Flask imports are **lazy** - they happen only when the receiver is
actually started (at most ~30 s per keybind press).

Security: the server is bound to localhost and issues a random per-run
token that the extension must echo back in an ``X-PM-Token`` header.
CORS is restricted to the YouTube Music origin, so an arbitrary webpage
cannot read the token or POST a URL.
"""

import logging
import re
import secrets
import threading
import time
from typing import Optional
from queue import Queue, Empty

from constants import FLASK_RECEIVER_PORT

logger = logging.getLogger(__name__)

YT_MUSIC_URL_PATTERN = r"https://music\.youtube\.com/watch\?v=([\w-]+)"


def extract_video_id(url: Optional[str]) -> Optional[str]:
    """Validate and extract the video ID from a YouTube Music URL.

    Returns the video ID string, or None if the URL is not a valid
    YouTube Music watch URL (or not a string at all).
    """
    if not url or not isinstance(url, str):
        return None
    match = re.match(YT_MUSIC_URL_PATTERN, url)
    if not match:
        return None
    return match.group(1)


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
      2. Extension polls GET /status -> {"ready": true, "token": "<per-run token>"} while server is up.
      3. Extension POSTs URL to /receive-url once, echoing the token in X-PM-Token.
      4. Flow controller calls set_waiting(False), retrieves URL from queue, stops server.

    The server is short-lived (up to ~30 s per keybind press) and binds to
    localhost only, so plain HTTP is acceptable.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = FLASK_RECEIVER_PORT,
        timeout: int = 30,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.url_queue: Queue = Queue()
        self.app = None
        self.thread: Optional[threading.Thread] = None
        self._server = None
        self._running = False
        self._waiting_for_url = False
        self._token: Optional[str] = None
        self._state_lock = threading.Lock()
        self._rate_limiter = _RateLimiter(max_requests=10, window_seconds=60)
        # NOTE: _ensure_app() is NOT called here - Flask is imported lazily.

    def _ensure_app(self):
        """Lazy initialisation of the Flask application and routes."""
        if self.app is not None:
            return

        from flask import Flask, request, jsonify
        from flask_cors import CORS
        from werkzeug.serving import make_server

        self._make_server = make_server

        app = Flask(__name__)
        # Only the YT Music tab (extension content script) may talk to the
        # receiver.  An arbitrary webpage must not be able to read /status
        # (to steal the token) or POST a URL.
        CORS(
            app,
            resources={
                r"/*": {
                    "origins": [
                        "https://music.youtube.com",
                        f"http://localhost:{self.port}",
                    ],
                    "methods": ["GET", "POST", "OPTIONS"],
                    "allow_headers": ["Content-Type", "X-PM-Token"],
                }
            },
        )

        @app.route("/status", methods=["GET"])
        def _status():
            """Extension polls this to know when to send a URL."""
            with self._state_lock:
                ready = self._waiting_for_url
                # Only expose the per-run token once the flow is actually
                # waiting - a stale token between flows is needless surface.
                token = self._token if ready else None
            return jsonify({"ready": ready, "token": token})

        @app.route("/receive-url", methods=["POST", "OPTIONS"])
        def _receive_url():
            """Endpoint to receive YouTube Music URLs."""
            if request.method == "OPTIONS":
                return "", 200

            # Every POST must prove it learned the per-run token from
            # /status (a browser page can only do that via the CORS
            # allowlist above; the extension content script runs on the
            # YouTube Music origin, so it passes).
            if not self._token or request.headers.get("X-PM-Token") != self._token:
                return jsonify({"error": "Forbidden"}), 403

            if not self._rate_limiter.is_allowed():
                return jsonify({"error": "Rate limit exceeded. Try again later."}), 429

            try:
                data = request.get_json()
                url = data.get("url", "").strip() if data else ""

                if not url:
                    return jsonify({"error": "No URL provided"}), 400

                video_id = extract_video_id(url)
                if video_id is None:
                    return jsonify({"error": "Invalid YouTube Music URL"}), 400

                with self._state_lock:
                    self._waiting_for_url = False
                    # Invalidate the token the moment the URL is consumed,
                    # not just at stop(): between consumption and the
                    # flow's stop() call there is no legitimate second
                    # receiver, so a duplicate POST in that window must
                    # get 403 rather than enqueue a stale URL.
                    self._token = None
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

    def set_waiting(self, waiting: bool) -> None:
        """Control whether the /status endpoint reports ready.

        Each transition to waiting starts a new flow: issue a fresh per-run
        token so the extension can distinguish a new keybind press from the
        previous (finished) one even when this server instance was reused
        (e.g. the previous flow's shutdown was still in flight and start()
        skipped regenerating it). Also drain any URL a previous flow left
        in the queue so a stale song cannot be consumed by the next flow.
        """
        with self._state_lock:
            self._waiting_for_url = waiting
            if waiting:
                self._token = secrets.token_hex(16)
                while True:
                    try:
                        self.url_queue.get_nowait()
                    except Empty:
                        break

    def start(self) -> Optional[threading.Thread]:
        """Start the Flask server in a daemon thread."""
        if self._running:
            logger.warning("URLReceiverManager is already running")
            return self.thread

        self._ensure_app()

        # Fresh token per run - a token from a previous (finished) flow
        # must not be accepted.
        self._token = secrets.token_hex(16)

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
        """Stop the Flask server gracefully.

        ``_running`` is cleared and the flow sentinel is queued *before*
        ``shutdown()``: a shutdown failure cannot wedge the receiver in
        the "running" state (which would make every later flow report
        "already running" and wait on a dead server), and a flow blocked
        in :meth:`get_received_url` aborts promptly even when an
        in-flight ``/receive-url`` request would otherwise deliver a URL
        to a flow that was meant to be aborted.
        """
        if not self._running:
            return

        with self._state_lock:
            self._waiting_for_url = False
            self._token = None

        self._running = False
        # Wake a flow blocked in get_received_url() so it aborts promptly
        # instead of holding the flow lock until its timeout expires (e.g.
        # update_credentials stops the receiver mid-wait).  The sentinel
        # (None) is drained by set_waiting(True) before the next flow
        # starts.
        self.url_queue.put(None)
        try:
            if self._server:
                self._server.shutdown()
                self._server = None
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
            if url is None:
                raise RuntimeError(
                    "URL receiver stopped while waiting for a URL"
                )
            logger.debug("Retrieved URL from queue")
            return url
        except Empty:
            logger.warning(f"Timeout waiting for URL after {timeout} seconds")
            raise TimeoutError(f"No URL received within {timeout} seconds")

    def is_running(self) -> bool:
        return self._running
