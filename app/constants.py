"""
Shared constants for the PlaylistManager application.

Centralises platform identifiers and other magic strings to prevent
typos and make future additions easier.
"""

PLATFORM_YOUTUBE_MUSIC = "youtube_music"
PLATFORM_SPOTIFY = "spotify"

# Port for the short-lived local Flask URL receiver used by the YouTube Music flow.
# NOTE: the browser extension's host_permissions in
# youtube-music-extension/manifest.json must match this port.
FLASK_RECEIVER_PORT = 5000
