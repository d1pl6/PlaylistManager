"""
Integration registry and platform-specific integration wrappers.

Concrete integration classes receive their auth manager as a constructor
argument rather than importing it at module level, so this file has zero
import-time side effects and does not pull in optional dependencies.

Since 0.3.0 the concrete integrations live in their plugin packages
(``integrations/<platform_id>/integration.py``) - see app/plugin_loader.py.
This module only carries the shared base class and registry.
"""

import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class BaseIntegration:
    id: str = ""
    display_name: str = ""
    # Whether this platform can serve as an "add playlist" source (i.e. it
    # can browse a library and manage playlists).  Service-only integrations
    # (e.g. Last.fm - scrobble/like but no playlists) set this False so they
    # are never offered as a source in the "Choose platform" picker, while
    # staying available for their service actions (get_all still returns
    # them; only get_active excludes them).
    supports_playlists: bool = True
    # Public auth-manager handle, passed in via the constructor and exposed
    # so bootstrap/auth plumbing (app.py) can reach it without reaching into
    # a private ``_auth`` attribute.  Platforms without credentials leave
    # it None.
    auth_manager = None

    def is_authenticated(self) -> bool:
        raise NotImplementedError

    def authenticate(self) -> bool:
        raise NotImplementedError

    def refresh_auth(self) -> bool:
        raise NotImplementedError

    def get_library_playlists(self) -> list:
        raise NotImplementedError

    def get_playlist_details(self, playlist_id: str, limit: int = 1) -> dict:
        raise NotImplementedError

    def get_playlist_id(self, name: str) -> Optional[str]:
        """Look up a playlist's platform ID by name.

        Returns *None* when the playlist cannot be found or the
        platform does not support name-based lookups.
        """
        return None

    def get_playlist_tracks(self, playlist_id: str) -> list:
        """Fetch all tracks for a playlist.

        Returns an empty list when the playlist doesn't exist or the
        platform returns an error.
        """
        return []

    def remove_track(self, playlist_id: str, track_id: str) -> bool:
        """Remove one track from a platform playlist.

        Returns True only when the platform confirmed the removal.
        The default returns False - implementations must override.
        """
        return False


class ScrobbleCapable:
    """Optional capability interface for scrobbling / liking backends.

    A plugin integration may implement this interface to expose shared
    scrobble actions consumed by the core (e.g., Last.fm). Core consumers
    use duck-typing:

        scrobble_fn = getattr(integration, "scrobble", None)
        if scrobble_fn:
            scrobble_fn(song_data)

    This keeps the scrobble backend self-contained under its plugin folder
    while the core remains platform-agnostic.
    """

    def scrobble(self, song_data: dict) -> bool:
        """Scrobble a song.

        *song_data* is a dict with at minimum::

            {
                "title": str,
                "artists": [str, ...],  # artist is artists[0]
                "duration": int,  # seconds
            }

        Album and track_number (from song_data["album"], song_data["track_number"])
        are optional but improve scrobble fidelity.

        Returns True only when the backend confirmed the scrobble.
        Failures must be logged but never block or fail the calling add-flow.
        """
        raise NotImplementedError

    def unlove(self, artist: str, track: str) -> bool:
        """Remove a track from the user's loved/liked list.

        Returns True only when the backend confirmed the removal.
        """
        raise NotImplementedError

    def love(self, artist: str, track: str) -> bool:
        """Add a track to the user's loved/liked list (like a song).

        This is a separate action from :meth:`scrobble`: liking must never
        create a scrobble, and auto-scrobbling on add must never like the
        song.  The core's like toggle calls this directly.

        Returns True only when the backend confirmed the like.
        """
        raise NotImplementedError

    def is_loved(self, artist: str, track: str) -> bool | None:
        """Check if a track is in the user's loved/liked list.

        Returns:
            True if loved, False if not loved, None if unknown/unauthenticated.

        Must return synchronously (cached value preferred to avoid network
        round trips per-track). Load state asynchronously on display and
        update via root.after(0, ...) from the worker thread.
        """
        raise NotImplementedError

    def delete_scrobble(self, artist: str, track: str, timestamp: int | None = None) -> bool:
        """Delete a scrobble entry.

        *timestamp* (seconds since epoch) is optional; when omitted, deletes
        the most recent scrobble of that track.

        Returns True only when the backend confirmed the deletion.
        Failures are best-effort; never fail or re-enqueue the calling remove.
        """
        raise NotImplementedError


class IntegrationRegistry:
    def __init__(self):
        self._integrations: Dict[str, BaseIntegration] = {}

    def register(self, integration: BaseIntegration):
        self._integrations[integration.id] = integration

    def get_all(self) -> Dict[str, BaseIntegration]:
        return dict(self._integrations)

    def get_active(self) -> Dict[str, BaseIntegration]:
        return {
            k: v
            for k, v in self._integrations.items()
            if v.is_authenticated() and v.supports_playlists
        }

    def get(self, integration_id: str) -> Optional[BaseIntegration]:
        return self._integrations.get(integration_id)

    def unregister(self, platform_id: str) -> None:
        """Drop one integration (Manage dialog uninstall path).

        Consumers iterate ``get_all()`` / ``get_active()``, so the id
        simply disappears from every derived view.  An in-flight flow
        that already captured the object keeps running to completion -
        its platform-first write path fails loudly the same way any API
        error does.
        """
        removed = self._integrations.pop(platform_id, None)
        if removed is not None:
            logger.info("Unregistered integration '%s'", platform_id)


class BaseFlowController:
    """Protocol for platform-specific flow controllers.

    Plugin flow classes subclass this (or duck-type it); KeybindController
    dispatches against the interface below.
    """

    def execute_flow(
        self,
        playlist_name: str,
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
        on_success,
        playlist_id: str | None = None,
        url: str | None = None,
        song_data: dict | None = None,
        skip_duplicate_check: bool = False,
    ) -> None:
        """Run one add-song attempt for *playlist_name*.

        Exactly one of *on_success* / *on_error* fires when the flow
        finishes. *url* / *song_data*, when given, skip song acquisition
        (pre-captured by the caller). *skip_duplicate_check* bypasses the
        opt-in near-duplicate check - used ONLY by the activity window's
        Add action, otherwise Add would re-trigger the check it is
        trying to satisfy.
        """
        raise NotImplementedError
