"""
Shared "capture + scrobble the currently-playing song" orchestration.

Used by the scrobble keybind action
(``controllers/keybind_controller._scrobble_current_action``) and the
CLI ``-s`` batch command (``cli.run_scrobble``).  Both callers iterate
authenticated platform plugins, capture through the first platform that
reports a song, and scrobble it via Last.fm — the only difference is
how they build a flow for a platform and what they do with the result.
"""

import logging

from collections.abc import Callable

logger = logging.getLogger(__name__)


def scrobble_current_song(
    integrations,
    plugin_registry,
    flow_provider: Callable[[str], object | None],
    scrobble_integ,
    timeout: int = 30,
    integration_provider: Callable[[str], object | None] | None = None,
) -> tuple | None:
    """Capture the currently-playing song and scrobble it.

    Iterates registered plugins in order, skipping platforms that are
    not authenticated or declare no keybind flow.  *flow_provider*
    supplies the flow object for a platform id (or ``None`` when it
    cannot be built — that platform is skipped).  The first platform
    that captures a song scrobbles it and the loop stops.

    *integration_provider* supplies the (already authenticated)
    integration for a platform id and replaces the default registry
    lookup + ``is_authenticated()`` gate — the CLI uses it to
    authenticate one platform at a time right before its capture
    attempt, so a scrobble never pays ``authenticate()`` for a
    platform that is not the current song source.

    Returns ``(song_data, ts)`` — the captured song and the accepted
    scrobble timestamp (``None`` when the backend refused the scrobble)
    — or ``None`` when no platform produced a song.
    """
    for platform_id in plugin_registry.get_platform_ids():
        if integration_provider is not None:
            integration = integration_provider(platform_id)
            if integration is None:
                continue
        else:
            integration = integrations.get(platform_id)
            if integration is None or not integration.is_authenticated():
                continue

        plugin = plugin_registry.get(platform_id)
        if plugin is None or not plugin.flow_class:
            continue

        try:
            flow = flow_provider(platform_id)
            if flow is None:
                continue

            _url, song_data, error = flow.capture(timeout)
            if not song_data:
                continue

            return song_data, scrobble_integ.scrobble(song_data)
        except Exception as e:
            # One platform's capture/scrobble failure must not sink the
            # whole action — try the next.  The user triggered this, so
            # log at info (not debug): a silently failed scrobble is the
            # kind of thing worth surfacing when verbose logging is off.
            logger.info("Scrobble capture failed on %s: %s", platform_id, e)
            continue

    return None
