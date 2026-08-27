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


class IntegrationRegistry:
    def __init__(self):
        self._integrations: Dict[str, BaseIntegration] = {}

    def register(self, integration: BaseIntegration):
        self._integrations[integration.id] = integration

    def get_all(self) -> Dict[str, BaseIntegration]:
        return dict(self._integrations)

    def get_active(self) -> Dict[str, BaseIntegration]:
        return {
            k: v for k, v in self._integrations.items() if v.is_authenticated()
        }

    def get(self, integration_id: str) -> Optional[BaseIntegration]:
        return self._integrations.get(integration_id)


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
