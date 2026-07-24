import logging
import os
import re
import ssl
import tempfile
import threading
import time
from typing import Optional
from queue import Queue, Empty
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.serving import make_server

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
    Manages a Flask server for receiving YouTube Music URLs via HTTPS.
    Runs in a daemon thread and uses a thread-safe queue for communication.
    """

    # YouTube Music URL pattern
    YT_MUSIC_URL_PATTERN = r"https://music\.youtube\.com/watch\?v=([\w-]+)"

    def __init__(self, host: str = "localhost", port: int = 5000, timeout: int = 30):
        """
        Initialize the URL receiver manager.

        Args:
            host: Host to bind Flask server to
            port: Port to bind Flask server to
            timeout: Seconds to wait for URL before timing out
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.url_queue: Queue = Queue()
        self.app = Flask(__name__)
        self.thread: Optional[threading.Thread] = None
        self._server = None
        self._running = False
        self._rate_limiter = _RateLimiter(max_requests=10, window_seconds=60)
        self._ssl_context = None
        self._setup_flask()

    def _create_ssl_context(self) -> ssl.SSLContext:
        """Create a self-signed SSL context for local HTTPS."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        certfile = os.path.join(tempfile.gettempdir(), "playlistmanager_receiver.pem")
        keyfile = os.path.join(tempfile.gettempdir(), "playlistmanager_receiver_key.pem")
        if not os.path.exists(certfile) or not os.path.exists(keyfile):
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import datetime

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime.utcnow())
                .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .sign(key, hashes.SHA256())
            )
            cert_pem = cert.public_bytes(serialization.Encoding.PEM)
            key_pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
            fd = os.open(certfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, cert_pem)
            finally:
                os.close(fd)
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, key_pem)
            finally:
                os.close(fd)
        ctx.load_cert_chain(certfile, keyfile)
        return ctx

    def _setup_flask(self):
        """Setup Flask app and routes."""
        CORS(
            self.app,
            resources={
                r"/*": {
                    "origins": "*",
                    "methods": ["GET", "POST", "OPTIONS"],
                    "allow_headers": ["Content-Type"],
                }
            },
        )

        @self.app.route("/receive-url", methods=["POST", "OPTIONS"])
        def receive_url():
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

                # Validate YouTube Music URL
                if not self._validate_youtube_url(url):
                    return jsonify({"error": "Invalid YouTube Music URL"}), 400

                # Extract video ID
                video_id = self._extract_video_id(url)

                # Put URL in queue for main app to retrieve
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

    @staticmethod
    def _validate_youtube_url(url: str) -> bool:
        """
        Validate if URL is a valid YouTube Music URL.

        Args:
            url: URL to validate

        Returns:
            True if valid, False otherwise
        """
        return re.match(URLReceiverManager.YT_MUSIC_URL_PATTERN, url) is not None

    @staticmethod
    def _extract_video_id(url: str) -> str:
        """
        Extract video ID from YouTube Music URL.

        Args:
            url: YouTube Music URL

        Returns:
            Video ID or empty string if not found
        """
        match = re.search(URLReceiverManager.YT_MUSIC_URL_PATTERN, url)
        if match:
            return match.group(1)
        return ""

    def start(self) -> Optional[threading.Thread]:
        """
        Start the Flask server in a daemon thread.

        Returns:
            The daemon thread
        """
        if self._running:
            logger.warning("URLReceiverManager is already running")
            return self.thread

        try:
            self._server = make_server(self.host, self.port, self.app, threaded=True, ssl_context=self._ssl_context)
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
            logger.warning("URLReceiverManager is not running")
            return

        try:
            if self._server:
                self._server.shutdown()
                self._server = None
            self._running = False
            logger.info("Stopped URL receiver")
        except Exception as e:
            logger.error(f"Error stopping URL receiver: {e}")

    def get_received_url(self, timeout: Optional[int] = None) -> str:
        """
        Get the received URL from the queue.

        Args:
            timeout: Seconds to wait for URL. Uses self.timeout if not specified.

        Returns:
            The received URL

        Raises:
            TimeoutError: If no URL received within timeout period
        """
        if timeout is None:
            timeout = self.timeout

        try:
            url = self.url_queue.get(timeout=timeout)
            logger.debug(f"Retrieved URL from queue")
            return url
        except Empty:
            logger.warning(f"Timeout waiting for URL after {timeout} seconds")
            raise TimeoutError(f"No URL received within {timeout} seconds")

    def is_running(self) -> bool:
        """Check if the Flask server is running."""
        return self._running
