"""Scrobble policy: which platforms are scrobble SOURCES, and which service
backends RECEIVE the scrobble.

Two independent questions, both answered here:

* **Does this add scrobble at all, and if so from which source?**  The
  add-path scrobbles a song when ``scrobble_on_add`` is enabled.
  Which *source* platforms scrobble is configurable via the ``[scrobble]
  platforms`` setting (a comma-separated list of enabled platform ids).
  An empty/absent list means **every installed music platform** scrobbles -
  the historical global behavior - so a fresh install with the setting unset
  behaves exactly as before.  This is the master/``platforms`` face, turned
  into a per-playlist-platform decision shared by the keybind hotkey path
  (``controllers/keybind_controller.py``) and the CLI batch add (``cli.py``).
  The standalone scrobble actions (``scrobble_keybind`` hotkey, ``-s`` CLI)
  are explicit user commands and deliberately bypass this gate.

* **Which service backends receive the scrobble?**  Every scrobble backend
  is a plugin implementing (part of) the duck-typed
  :class:`services.integration.ScrobbleCapable` capability, and there is
  more than one: Last.fm, ListenBrainz.  Each is switched on or off
  individually, in its own settings section::

      [lastfm]
      scrobbles = true
      [listenbrainz]
      scrobbles = false

  A per-platform boolean rather than one "the backend" setting, because
  "which services should my listens go to" is naturally a set: 2 of 3
  enabled has to be expressible, and a third plugin must not need a new
  setting.  Absent means **enabled**, so installing and logging into a
  second backend makes it work, and turning one off is always an explicit
  act (:func:`scrobble_backend_enabled`, written by the Settings checkboxes).

  :func:`scrobble_backends` is the single place that turns "the installed
  backends" into "the ones to use":

  * the result is ordered by platform id, never by plugin discovery order,
    so two scrobbles of the same song never race to a different service;
  * a backend reporting itself unauthenticated is never returned, so a
    stale credential file cannot shadow a working one;
  * the capability a caller needs is part of the query, so a backend that
    implements only part of the interface (ListenBrainz has no love/hate
    feedback - it is keyed on a MusicBrainz recording MBID) is selected for
    the scrobble path and *not* for the showcase heart button.

  Every enabled backend gets the scrobble; that is the point of the switch.
  The love paths are the one place a single backend is picked (a heart is
  one service's loved list, and only Last.fm can answer it), and the
  remove-song path is the other (it must address the service the scrobble
  actually went to - see ``services/scrobble_log.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from utils.config import (
    SCROBBLE_PLATFORMS_OPTION,
    SCROBBLE_PLATFORMS_SECTION,
    SCROBBLES_OPTION,
    get_setting,
    get_setting_bool,
    get_setting_value,
)

logger = logging.getLogger(__name__)

# Stored value of [scrobble] platforms meaning "no platform auto-scrobbles".
# The Settings list writes this when the user unchecks every platform; an
# EMPTY value keeps meaning "all installed" (historical default), so the UI
# needs an explicit marker to express the opposite.  The per-backend
# ``[<platform>] scrobbles`` switch needs no such marker: it is a real
# boolean, so "off" is a value rather than an absence.
SCROBBLE_NONE = "none"

# The capabilities the core resolves a backend for.  Named here so the
# selection reads the same at every call site (and so a typo is a NameError
# at import, not a silently ineligible backend).
SCROBBLE_WRITE = ("scrobble",)
SCROBBLE_DELETE = ("delete_scrobble",)
SCROBBLE_LIKE = ("love", "unlove", "is_loved")
SCROBBLE_LOVED_SET = ("get_loved_set",)


def split_platforms(value: str) -> list[str]:
    """Split a stored platform list into stable, deduped ids.

    Comma-separated, whitespace-trimmed, empty entries dropped, order
    preserved.  ``""`` and ``None`` produce ``[]``.
    """
    seen = []
    for raw in (value or "").split(","):
        item = raw.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def scrobble_enabled_for(platform: str) -> bool:
    """Whether an add on the given playlist *platform* should auto-scrobble.

    Decision table::

        scrobble_on_add off                    -> False (master gate)
        [scrobble] platforms empty/absent      -> True  (all installed)
        [scrobble] platforms == "none"         -> False (explicitly none)
        platform in [scrobble] platforms       -> True
        otherwise                              -> False

    Reads the settings INI each call (cheap; the master gate is always
    evaluated before this in practice).
    """
    if not get_setting("scrobble_on_add"):
        return False
    raw = get_setting_value(SCROBBLE_PLATFORMS_SECTION, SCROBBLE_PLATFORMS_OPTION)
    if not raw or not raw.strip():
        return True
    if raw.strip().lower() == SCROBBLE_NONE:
        return False
    # Match the "none" marker's case-folding: a user typing "Spotify" in
    # settings.ini must not silently disable scrobbling for "spotify".
    return platform.lower() in (p.lower() for p in split_platforms(raw))


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def _capability_id(integration) -> str:
    """The platform id an integration registers under (may be missing)."""
    return str(getattr(integration, "id", "") or "")


def _is_authenticated(integration) -> bool:
    """Whether *integration* can act right now.

    An integration that exposes ``is_authenticated`` and answers False (or
    raises) is skipped: selecting it would make every scrobble a silent
    no-op while a working second backend went unused.  A duck-typed stub
    with no ``is_authenticated`` at all is taken at face value - the
    capability check, not the auth check, is what makes it eligible.
    """
    probe = getattr(integration, "is_authenticated", None)
    if not callable(probe):
        return True
    try:
        return bool(probe())
    except Exception:
        return False


def scrobble_backend_enabled(platform: str) -> bool:
    """Whether *platform* is switched on as a scrobble TARGET.

    Named against :func:`scrobble_enabled_for`, which asks the opposite
    question about a *source* platform.

    Reads ``[<platform>] scrobbles`` (``utils.config.SCROBBLES_OPTION``),
    defaulting to **True** when the key is absent: a user who has never
    touched the switch wants their listens recorded, and a backend that is
    installed and authenticated has already said yes by being there.  An
    unreadable value (hand-edited to nonsense) also reads as enabled, for
    the same reason - the resolver's own log is where a surprise belongs,
    not a silent no-scrobbling.

    Read from the settings INI each call (cheap, and the Settings
    checkboxes write it live without a restart).
    """
    pid = (platform or "").strip()
    if not pid:
        return False
    return get_setting_bool(pid, SCROBBLES_OPTION, fallback=True)


# One "more than one target" line per process: the resolver runs on every
# scrobble, and a user who sees a listen appear in two services deserves to
# be told why - but only once, not per song.
_noted_targets: set[str] = set()


def _note_targets(ids: list[str]) -> None:
    key = "+".join(ids)
    if key in _noted_targets:
        return
    _noted_targets.add(key)
    logger.info(
        "Scrobbling to %s - every backend with `[<platform>] scrobbles = true` "
        "receives each listen. Switch one off in Settings -> Integrations -> "
        "Scrobbling.",
        ", ".join(ids),
    )


def scrobble_backends(
    integrations,
    required: Iterable[str] = SCROBBLE_WRITE,
    *,
    only_enabled: bool = True,
) -> list:
    """The service backends to use for the *required* capabilities.

    *integrations* is an :class:`services.integration.IntegrationRegistry`
    (anything with ``get_all()``) or ``None``.  Candidates are the
    installed integrations that expose every method in *required* and
    report themselves authenticated; with *only_enabled* (the default) the
    ``[<platform>] scrobbles`` switch also has to be on.  The result is
    ordered by platform id, case-folded, so it is the same list on every
    run and on every machine.

    *only_enabled* is False on exactly two paths, and the reason is that
    neither is about *writing*:

    * the remove-song path, which must be able to reach a service the user
      has since switched off - the listen exists there and the local row is
      about to be deleted, so the flag governs new scrobbles, not cleanup
      of recorded ones (which service got it is what
      ``services.scrobble_log`` records);
    * the love paths, which are a *read* of a loved list and a write to it,
      but not a scrobble: a user who sends listens to ListenBrainz only
      still wants the heart to work on Last.fm.
    """
    if not integrations:
        return []
    try:
        all_integrations = integrations.get_all().values()
    except AttributeError:
        return []
    required = tuple(required)
    candidates = [
        integ
        for integ in all_integrations
        if all(callable(getattr(integ, cap, None)) for cap in required)
        and _is_authenticated(integ)
    ]
    if only_enabled:
        candidates = [
            integ for integ in candidates if scrobble_backend_enabled(_capability_id(integ))
        ]
        if len(candidates) > 1:
            ids = [_capability_id(i) for i in candidates]
            _note_targets(ids)
    return sorted(candidates, key=lambda i: _capability_id(i).casefold())


def scrobble_backend_for(
    integrations,
    required: Iterable[str],
    platform_id: str,
):
    """The one integration registered under *platform_id*, if it qualifies.

    Used by the remove-song path to address a service named in the scrobble
    ledger.  A recorded backend that is no longer installed, no longer
    authenticated, or no longer offers *required* resolves to ``None`` -
    the listen on that service is then simply unreachable, which the caller
    reports rather than papering over by deleting from a different service.
    """
    wanted = (platform_id or "").strip().casefold()
    if not wanted:
        return None
    # only_enabled=False: the flag governs new scrobbles, not the cleanup of
    # one the ledger already recorded.
    for integ in scrobble_backends(integrations, required, only_enabled=False):
        if _capability_id(integ).casefold() == wanted:
            return integ
    return None


def resolve_scrobble_backend(
    integrations,
    required: Iterable[str] = SCROBBLE_WRITE,
):
    """The single backend for a capability only one backend can serve.

    Last.fm's loved list is read and written through the showcase heart, and
    only a backend implementing the whole love half can answer it; with more
    than one such backend (none today) the first by platform id wins, which
    matches the deterministic ordering of :func:`scrobble_backends`.

    ``None`` means "nobody can", and the caller degrades (the heart stays
    empty / unavailable) instead of failing.
    """
    backends = scrobble_backends(integrations, required, only_enabled=False)
    return backends[0] if backends else None


def is_capture_platform(plugin_registry, platform_id: str) -> bool:
    """Whether *platform_id* can capture a song - i.e. is a scrobble SOURCE.

    The scrobble *target* is a service plugin (Last.fm, ListenBrainz) and
    the sources are the playlist platforms, which is exactly the set whose
    manifest declares a ``flow_class`` - the same test the standalone
    scrobble action uses to decide which platform it can capture from
    (``services.scrobble_service``).  Settings' per-platform scrobble list
    uses this instead of hardcoding an id per service backend, so a second
    service backend does not show up as something to capture from.

    False when no registry is supplied (nothing can be confirmed then).
    """
    if plugin_registry is None:
        return False
    try:
        plugin = plugin_registry.get(platform_id)
    except AttributeError:
        return False
    return plugin is not None and bool(getattr(plugin, "flow_class", ""))
