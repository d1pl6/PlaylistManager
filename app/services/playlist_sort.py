"""Pure grid-sort logic for the playlist grid.

``sort_playlists`` is display logic: it takes the raw registry list
(from ``PlaylistStore.load_playlists()``, BEFORE the platform filter)
and returns a new list in the requested order.  Pinned playlists
always float to the top; Name/Platform/Added/Used then apply within
each group (pinned and unpinned separately).

No imports from the rest of the app - this module is a pure function
so it is trivially testable headlessly.
"""

from typing import Dict, List

GRID_SORT_KEYS = ("name", "platform", "added", "used")
DEFAULT_GRID_SORT_KEY = "name"
DEFAULT_GRID_SORT_DIRECTION = "asc"


def sort_playlists(
    entries: List[Dict],
    key: str = DEFAULT_GRID_SORT_KEY,
    direction: str = DEFAULT_GRID_SORT_DIRECTION,
) -> List[Dict]:
    """Return a NEW list sorted by *key*.

    Args:
        entries: registry rows from PlaylistStore.load_playlists().
        key: one of GRID_SORT_KEYS ("name"/"platform"/"added"/"used").
            Unknown keys fall back to "name".
        direction: "asc" or "desc" (anything else falls back to "asc").

    Semantics per key:
        name     - case-insensitive playlist name.
        platform - case-insensitive platform id.
        added    - registry insertion order (the entry's index in
                   ``entries``, captured BEFORE the pinned partition).
        used     - ``last_used_at`` timestamp; entries without one are
                   sorted to the END in BOTH directions (never used is
                   not interesting in either ordering).

    Pinned playlists (``entry.get("pinned")``) always precede the rest;
    the active key sorts within each group.  The sort is stable, so
    ties keep registry order.
    """
    if key not in GRID_SORT_KEYS:
        key = DEFAULT_GRID_SORT_KEY
    if direction != "desc":
        direction = "asc"

    # Capture the true insertion order before partitioning, so "added"
    # stays correct across the pinned boundary.
    indexed = list(enumerate(entries))
    pinned = [(i, e) for i, e in indexed if e.get("pinned")]
    unpinned = [(i, e) for i, e in indexed if not e.get("pinned")]

    return _sort_group(pinned, key, direction) + _sort_group(
        unpinned, key, direction
    )


def _sort_group(indexed, key: str, direction: str) -> List[Dict]:
    """Sort one partition by *key*; returns entry dicts in order."""
    reverse = direction == "desc"
    if key == "added":
        # Index IS the added order.
        ordered = sorted(indexed, key=lambda pair: pair[0], reverse=reverse)
    elif key == "used":
        used = [pair for pair in indexed if pair[1].get("last_used_at")]
        never = [pair for pair in indexed if not pair[1].get("last_used_at")]
        ordered = sorted(
            used, key=lambda pair: pair[1]["last_used_at"], reverse=reverse
        )
        ordered += never  # never-used always trails
    else:  # name / platform
        field = "name" if key == "name" else "platform"
        ordered = sorted(
            indexed,
            key=lambda pair: str(pair[1].get(field, "")).lower(),
            reverse=reverse,
        )
    return [entry for _i, entry in ordered]