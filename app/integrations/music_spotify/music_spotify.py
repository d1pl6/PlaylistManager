"""
Spotify OAuth manager and API wrapper.

Only standard-library and core dependencies (``requests``, ``platformdirs``)
are imported at module level.  The filesystem side effect
``AUTH_FOLDER.mkdir()`` is deferred to the first access of ``AUTH_FOLDER``
via the ``_ensure_auth_dir()`` helper.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional, Dict, List

import requests
from platformdirs import user_config_dir

from utils.logging_config import user_log

logger = logging.getLogger(__name__)

AUTH_FOLDER = Path(user_config_dir("playlistmanager")) / "auth"
SPOTIFY_AUTH_FILE = AUTH_FOLDER / "spotify.json"

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SCOPES = "user-read-currently-playing playlist-read-private playlist-read-collaborative playlist-modify-public playlist-modify-private"

# Lazy AUTH_FOLDER initialisation - created on first method call.
_auth_dir_created = False


def _ensure_auth_dir():
    global _auth_dir_created
    if not _auth_dir_created:
        AUTH_FOLDER.mkdir(parents=True, exist_ok=True, mode=0o700)
        _auth_dir_created = True


def save_spotify_credentials_file(
    client_id: str, client_secret: str, refresh_token: str
) -> None:
    """Write Spotify credentials to disk with secure permissions.

    Single writer for the credential file - used both by the login UI
    (via :mod:`services.auth_setup`) and by the token-refresh path in
    :class:`SpotifyAPI`.

    Raises ``OSError`` on write failure.
    """
    _ensure_auth_dir()
    creds = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    fd = os.open(
        str(SPOTIFY_AUTH_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    try:
        os.write(fd, json.dumps(creds, indent=2).encode("utf-8"))
    finally:
        os.close(fd)


class SpotifyAPI:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._token_expires: float = 0
        self._lock = threading.Lock()
        self._session = requests.Session()
        original_request = self._session.request
        self._session.request = lambda *a, **kw: original_request(
            *a, **{**kw, "timeout": kw.get("timeout", 15)}
        )

    def _refresh_access_token(self) -> bool:
        with self._lock:
            # Double-check: another thread might have refreshed while we waited
            if self._access_token and time.time() < self._token_expires:
                return True
            try:
                resp = self._session.post(
                    SPOTIFY_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
                if resp.status_code != 200:
                    logger.error(
                        f"Spotify token refresh failed: {resp.status_code}"
                    )
                    return False
                data = resp.json()
                self._access_token = data["access_token"]
                self._token_expires = time.time() + data.get("expires_in", 3600) - 60
                if "refresh_token" in data:
                    self.refresh_token = data["refresh_token"]
                    self._save_credentials()
                logger.info("Spotify access token refreshed")
                return True
            except Exception as e:
                logger.error(f"Spotify token refresh error: {e}")
                return False

    def _ensure_token(self) -> bool:
        """Refresh the access token if expired. Returns True if valid."""
        if self._access_token and time.time() < self._token_expires:
            return True
        return self._refresh_access_token()

    def _get_headers(self) -> Dict[str, str]:
        if not self._ensure_token():
            raise RuntimeError("Failed to refresh Spotify access token")
        return {"Authorization": f"Bearer {self._access_token}"}

    def _request(
        self, method: str, endpoint: str, **kwargs
    ) -> Optional[Dict]:
        """Make an API request with automatic 401 refresh+retry.
        Returns parsed JSON response, or None on error/204.
        """
        url = f"{SPOTIFY_API_BASE}{endpoint}"
        try:
            resp = self._session.request(
                method, url, headers=self._get_headers(), **kwargs
            )
            if resp.status_code == 204:
                return None
            if resp.status_code == 401:
                if not self._refresh_access_token():
                    logger.error("Failed to refresh token after 401")
                    return None
                resp = self._session.request(
                    method, url, headers=self._get_headers(), **kwargs
                )
            if resp.status_code >= 400:
                logger.error(
                    f"Spotify API error {resp.status_code}: {resp.text[:200]}"
                )
                return None
            return resp.json()
        except Exception as e:
            logger.error(f"Spotify API request failed: {e}")
            return None

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        return self._request("GET", endpoint, params=params)

    def get_me(self) -> Optional[Dict]:
        return self._get("/me")

    def get_playlists(self, limit: int = 50) -> List[Dict]:
        playlists = []
        url = "/me/playlists"
        params = {"limit": limit}
        while url:
            data = self._get(url, params)
            if not data:
                break
            playlists.extend(data.get("items", []))
            url = data.get("next")
            if url:
                url = url.replace(SPOTIFY_API_BASE, "")
                params = None
        return playlists

    def get_playlist(self, playlist_id: str) -> Optional[Dict]:
        return self._get(f"/playlists/{playlist_id}")

    def get_playlist_tracks(self, playlist_id: str) -> List[Dict]:
        tracks = []
        url = f"/playlists/{playlist_id}/tracks"
        params = {"limit": 100}
        while url:
            data = self._get(url, params)
            if not data:
                break
            for item in data.get("items", []):
                track = item.get("track")
                if track and track.get("id"):
                    tracks.append(track)
            url = data.get("next")
            if url:
                url = url.replace(SPOTIFY_API_BASE, "")
                params = None
        return tracks

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: List[str]) -> bool:
        chunk_size = 100
        success = True
        for chunk_start in range(0, len(track_ids), chunk_size):
            chunk = track_ids[chunk_start:chunk_start + chunk_size]
            uris = [f"spotify:track:{tid}" for tid in chunk]
            try:
                resp = self._session.post(
                    f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks",
                    headers=self._get_headers(),
                    json={"uris": uris},
                )
                if resp.status_code == 401:
                    if not self._refresh_access_token():
                        logger.error("Failed to refresh token for track addition")
                        return False
                    resp = self._session.post(
                        f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks",
                        headers=self._get_headers(),
                        json={"uris": uris},
                    )
                if resp.status_code in (200, 201):
                    logger.info(
                        f"Added {len(chunk)} tracks to playlist {playlist_id}"
                    )
                else:
                    logger.error(
                        f"Failed to add tracks: {resp.status_code} {resp.text[:200]}"
                    )
                    success = False
            except Exception as e:
                logger.error(f"Error adding tracks to playlist: {e}")
                return False
        return success

    def get_playlist_id_by_name(self, name: str) -> Optional[str]:
        playlists = self.get_playlists(limit=50)
        for playlist in playlists:
            if playlist.get("name") == name:
                return playlist.get("id")
        return None

    def get_currently_playing(self) -> Optional[Dict]:
        data = self._get("/me/player/currently-playing")
        if not data or not data.get("item"):
            return None
        track = data["item"]
        images = track.get("album", {}).get("images", [])
        return {
            "track_id": track["id"],
            "title": track.get("name", "Unknown"),
            "artists": [a.get("name", "Unknown") for a in track.get("artists", [])],
            "duration_ms": track.get("duration_ms", 0),
            "thumbnail": images[0]["url"] if images else None,
            "album_name": track.get("album", {}).get("name", ""),
        }

    def _save_credentials(self):
        try:
            save_spotify_credentials_file(
                self.client_id, self.client_secret, self.refresh_token
            )
        except Exception as e:
            logger.error(f"Failed to save Spotify credentials: {e}")


class SpotifyAuthManager:
    def __init__(self):
        self.api: Optional[SpotifyAPI] = None

    def setup_auth(self) -> bool:
        _ensure_auth_dir()
        if not SPOTIFY_AUTH_FILE.exists():
            logger.info(
                f"Spotify credentials not found at {SPOTIFY_AUTH_FILE}."
                f"See INTEGRATIONS.MD.",
            )
            return False

        try:
            creds = json.loads(SPOTIFY_AUTH_FILE.read_text(encoding="utf-8"))
            self.api = SpotifyAPI(
                client_id=creds["client_id"],
                client_secret=creds["client_secret"],
                refresh_token=creds["refresh_token"],
            )
            me = self.api.get_me()
            if me:
                user_log(
                    logger,
                    f"Spotify authenticated as {me.get('display_name', 'unknown')}",
                )
                return True
            logger.error("Spotify auth validation failed")
            self.api = None
            return False
        except Exception as e:
            logger.error(f"Spotify auth failed: {e}")
            self.api = None
            return False

    @staticmethod
    def verify_credentials(
        client_id: str, client_secret: str, refresh_token: str
    ) -> Optional[Dict]:
        """Validate credentials against the Spotify API.

        Creates a temporary :class:`SpotifyAPI` client and calls ``/v1/me``
        so the caller doesn't have to construct the API wrapper itself.
        Returns the parsed response dict, or None if the request failed.
        """
        api = SpotifyAPI(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
        return api.get_me()

    def is_authenticated(self) -> bool:
        return self.api is not None

    def get_api(self) -> Optional[SpotifyAPI]:
        return self.api


spotify_auth = SpotifyAuthManager()
