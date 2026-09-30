"""
Shared "capture + scrobble the currently-playing song" orchestration.

Used by the scrobble keybind action
(``controllers/keybind_controller._scrobble_current_action``) and the
CLI ``-s`` batch command (``cli.run_scrobble``).  Both callers iterate
authenticated platform plugins, capture through the first platform that
reports a song, and scrobble it to every enabled service backend — the
only difference is how they build a flow for a platform and what they do
with the result.
"""

from collections.abc import Callable
import logging

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
    that captures a song scrobbles it and the loop stops; a *capture*
    failure falls through to the next platform, but a captured song whose
    scrobble every backend refuses does not (see below).

    *integration_provider* supplies the (already authenticated)
    integration for a platform id and replaces the default registry
    lookup + ``is_authenticated()`` gate — the CLI uses it to
    authenticate one platform at a time right before its capture
    attempt, so a scrobble never pays ``authenticate()`` for a
    platform that is not the current song source.

    *scrobble_integ* is the scrobble target: one integration, or an
    iterable of them (every backend enabled with
    ``[<platform>] scrobbles = true``).  All of them receive the same
    captured song with the same timestamp — the backends are
    independent services, so one refusing must not stop the others, and a
    listener recorded once with a single timestamp can be deleted from
    each of them by the remove path.

    Returns ``(song_data, ts)`` — the captured song and the accepted
    scrobble timestamp (``None`` when no backend accepted the scrobble) —
    or ``None`` when no platform produced a song.  *ts* is the first
    backend's timestamp; the capture loop is what callers report, and
    every backend here was handed the same song and derived the same
    ``played_at`` from it.

    The three outcomes are therefore distinguishable by the caller:
    ``None`` = nothing was playing, ``(song, None)`` = a song was playing
    and every backend refused it, ``(song, ts)`` = scrobbled.
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

            # `error` is the flow's own capture message; the service-level
            # "this platform could not report a song" reason is logged in
            # the handler below, and a song-less capture is not a scrobble
            # failure the caller needs to act on.
            _url, song_data, _error = flow.capture(timeout)
            if not song_data:
                continue

            backends = (
                [scrobble_integ]
                if not isinstance(scrobble_integ, (list, tuple, set))
                else list(scrobble_integ)
            )
            accepted_ts = None
            any_failed = False
            for backend in backends:
                try:
                    ts = backend.scrobble(song_data)
                except Exception as e:
                    # One service refusing must not cost the others their
                    # listen; keep going down the list.
                    any_failed = True
                    logger.info(
                        "Scrobble to %s failed: %s",
                        getattr(backend, "id", "?"), e,
                    )
                    continue
                if ts is not None and accepted_ts is None:
                    accepted_ts = ts
            if accepted_ts is None and any_failed:
                # Every backend refused or raised.  STOP here rather than
                # falling through to the next source platform: a song WAS
                # captured, so re-capturing from another player can only
                # report a different song (or the same one, scrobbled twice)
                # and, because the fall-through eventually returns None, it
                # reports the failure to the caller as "no song found" -
                # telling the user their player is idle when in fact their
                # scrobble service rejected the listen.
                return song_data, None
            return song_data, accepted_ts
        except Exception as e:
            # One platform's capture/scrobble failure must not sink the
            # whole action — try the next.  The user triggered this, so
            # log at info (not debug): a silently failed scrobble is the
            # kind of thing worth surfacing when verbose logging is off.
            logger.info("Scrobble capture failed on %s: %s", platform_id, e)
            continue

    return None
