"""Per-platform auto-scrobble gate.

The add-path scrobbles a song with the Last.fm backend when
``scrobble_on_add`` is enabled.  Which *source* platforms scrobble is
configurable via the ``[scrobble] platforms`` setting (a comma-separated
list of enabled platform ids).  An empty/absent list means **every
installed music platform** scrobbles - the historical global behavior -
so a fresh install with the setting unset behaves exactly as before.

This module is the single place the master/``platforms`` face is turned
into a per-playlist-platform decision, shared by the keybind hotkey path
(``controllers/keybind_controller.py``) and the CLI batch add (``cli.py``).
The standalone scrobble actions (``scrobble_keybind`` hotkey, ``-s`` CLI)
are explicit user commands and deliberately bypass this gate.
"""

from __future__ import annotations

from typing import List

from utils.config import (
    SCROBBLE_PLATFORMS_OPTION,
    SCROBBLE_PLATFORMS_SECTION,
    get_setting,
    get_setting_value,
)

# Stored value meaning "no platform auto-scrobbles".  The Settings list
# writes this when the user unchecks every platform; an EMPTY value keeps
# meaning "all installed" (historical default), so the UI needs an explicit
# marker to express the opposite.
SCROBBLE_NONE = "none"


def split_platforms(value: str) -> List[str]:
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
    return platform in split_platforms(raw)