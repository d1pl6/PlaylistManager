"""
Shared constants for the PlaylistManager application.

Platform identifiers are NOT defined here any more (0.3.0 step 1): each
integration declares its id, display name and URL hosts in
``integrations/<id>/plugin.json``, discovered at runtime by
``plugin_loader.PluginRegistry``.  Core code that needs a literal id
(legacy-entry defaults, per-platform health probes) declares a local
constant instead.
"""

# Port for the short-lived local Flask URL receiver used by the YouTube Music flow.
# NOTE: the browser extension's host_permissions in
# youtube-music-extension/manifest.json must match this port.
FLASK_RECEIVER_PORT = 5000
