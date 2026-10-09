"""
Keybind registry - stores registered keybind combos and matches them
against the currently-pressed keys.

``KeybindCallbacks`` lives here next to the registry that stores it, so
both ends of the contract import one module.
"""

import threading
import logging
from typing import Callable

from utils.key_mapping import parse_keybind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Callback interface (replaces raw labels_dict)
# ---------------------------------------------------------------------------


class KeybindCallbacks:
    """
    Thin callback container that replaces direct widget access.

    Registered once per playlist and called from worker threads - the
    implementation should route to the main thread (e.g. ``root.after()``).

    Args:
        on_status: Called with (text, background_colour) to update status.
        on_song_info: Called with (artist_text, title_text).
        on_entry_state: Called with the new state for the keybind entry.
        on_reset: Called with entry_state when all labels should be cleared.
        on_song_added: Called (no args) after a song was added by the flow,
            so the caller can refresh song-derived UI.  Not called for
            "exists" results - the song data did not change.
    """

    def __init__(
        self,
        on_status: Callable[[str, str], None] = lambda text, bg: None,
        on_song_info: Callable[[str, str], None] = lambda artist, name: None,
        on_entry_state: Callable[[str], None] = lambda state: None,
        on_reset: Callable[[str], None] = lambda entry_state: None,
        on_song_added: Callable[[], None] = lambda: None,
    ):
        self.on_status = on_status
        self.on_song_info = on_song_info
        self.on_entry_state = on_entry_state
        self.on_reset = on_reset
        self.on_song_added = on_song_added


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class KeybindRegistry:
    """Stores registered keybind combos and matches them against pressed keys.

    Thread-safe - all mutation and reads are guarded by a single lock.
    """

    def __init__(self):
        self._keybind_map: dict[str, dict] = {}
        self._lock = threading.Lock()

    def register(
        self,
        playlist_name: str,
        keybind: str,
        callbacks: KeybindCallbacks,
        platform: str,
        playlist_id: str = "",
        kind: str = "playlist",
    ) -> dict | None:
        """
        Register a keybind + callbacks for *playlist_name*.

        Playlists are identified by ``(playlist_name, platform,
        playlist_id)`` so the same name keeps independent bindings.
        Replaces any previous registration for that combination.

        Returns the binding info of a *different* playlist that owned
        *keybind* before this registration (its binding is now displaced),
        or None when nothing was displaced.  Callers should clear the
        displaced playlist's persisted keybind so the store cannot
        resurrect a binding that no longer fires anything.
        """
        self.unregister(playlist_name, platform=platform, playlist_id=playlist_id)
        displaced = None
        if keybind:
            with self._lock:
                existing = self._keybind_map.get(keybind)
                if existing is not None and (
                    existing["playlist_name"] != playlist_name
                    or existing["platform"] != platform
                    or (existing.get("playlist_id", "") or "") != (playlist_id or "")
                ):
                    displaced = existing
                    # Two playlists can't share one keybind - the new binding
                    # silently wins, which is confusing.  Make it visible.
                    logger.warning(
                        "Keybind '%s' is already bound to playlist '%s' (%s) - "
                        "replacing its binding with '%s' (%s)",
                        keybind,
                        existing["playlist_name"],
                        existing["platform"],
                        playlist_name,
                        platform,
                    )
                self._keybind_map[keybind] = {
                    "playlist_name": playlist_name,
                    "callbacks": callbacks,
                    "platform": platform,
                    "playlist_id": playlist_id,
                    "kind": kind,
                    "_parsed": parse_keybind(keybind),
                }
            logger.info(
                "Registered keybind '%s' for playlist '%s' (platform=%s)",
                keybind, playlist_name, platform,
            )
        return displaced

    def unregister(self, playlist_name: str, platform: str = "", playlist_id: str = ""):
        """Remove keybinds registered for *playlist_name*.

        With *platform* / *playlist_id* given, only that combination is
        removed, so a same-named playlist elsewhere stays bound.
        """
        with self._lock:
            to_remove = [
                k
                for k, v in self._keybind_map.items()
                if v["playlist_name"] == playlist_name
                and (
                    not platform
                    or v["platform"] == platform
                )
                and (not playlist_id or (v.get("playlist_id", "") or "") == playlist_id)
            ]
            for k in to_remove:
                del self._keybind_map[k]
                logger.info("Unregistered keybind '%s' for playlist '%s'", k, playlist_name)

    def find(
        self, playlist_name: str, platform: str = "", playlist_id: str = ""
    ) -> dict | None:
        """
        Return the current binding info for *playlist_name*.

        Used to validate a queued keybind event at execution time, since
        the frame may have been closed (or re-bound) between the registry
        match and the ``after(0, ...)`` dispatch.
        """
        with self._lock:
            for info in self._keybind_map.values():
                if info["playlist_name"] == playlist_name and (
                    not platform
                    or info["platform"] == platform
                ) and (
                    not playlist_id
                    or (info.get("playlist_id", "") or "") == playlist_id
                ):
                    return info
        return None

    def match(self, pressed_keys: set[str]) -> tuple | None:
        """
        Return the best (specificity, keybind_str, info) or *None*.

        "Best" is the entry matching *pressed_keys* exactly with the most
        keys, so a single-key shortcut cannot shadow a multi-key combo.
        The *info* dict carries 'kind' ("playlist" or "action").
        """
        best = None
        with self._lock:
            for keybind_str, info in self._keybind_map.items():
                expected = info["_parsed"]
                if not expected:
                    continue
                if expected == pressed_keys:
                    specificity = len(expected)
                    if best is None or specificity > best[0]:
                        best = (specificity, keybind_str, info)
        return best
