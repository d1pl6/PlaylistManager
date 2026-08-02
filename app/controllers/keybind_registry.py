"""
Keybind registry — stores registered keybind combos and matches them
against the currently-pressed keys.

This module has no knowledge of pynput or tkinter; it is pure data.
"""

import threading
import logging
from typing import Callable, Dict, Optional, Set

from utils.key_mapping import parse_hotkey

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Callback interface (replaces raw labels_dict)
# ---------------------------------------------------------------------------


class KeybindCallbacks:
    """Thin callback container that replaces direct widget access.

    A ``KeybindCallbacks`` instance is registered once per playlist
    and is called from worker threads — the implementation should
    route to the main thread (e.g. via ``root.after()``).

    Args:
        on_status: Called with (text, background_colour) to update status.
        on_song_info: Called with (artist_text, title_text).
        on_entry_state: Called with the new state for the keybind entry.
        on_reset: Called with entry_state when all labels should be cleared.
    """

    def __init__(
        self,
        on_status: Callable[[str, str], None] = lambda text, bg: None,
        on_song_info: Callable[[str, str], None] = lambda artist, name: None,
        on_entry_state: Callable[[str], None] = lambda state: None,
        on_reset: Callable[[str], None] = lambda entry_state: None,
    ):
        self.on_status = on_status
        self.on_song_info = on_song_info
        self.on_entry_state = on_entry_state
        self.on_reset = on_reset


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class KeybindRegistry:
    """Stores registered keybind combos and matches them against pressed keys.

    Thread-safe — all mutation and reads are guarded by a single lock.
    """

    def __init__(self):
        self._keybind_map: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def register(
        self,
        playlist_name: str,
        hotkey: str,
        callbacks: KeybindCallbacks,
        platform: str,
    ):
        """Register a keybind + callbacks for *playlist_name*.

        Replaces any previous registration for the same playlist name.
        """
        self.unregister(playlist_name)
        if not hotkey:
            return
        with self._lock:
            existing = self._keybind_map.get(hotkey)
            if existing is not None and existing["playlist_name"] != playlist_name:
                # Two playlists can't share one hotkey — the new binding
                # silently wins, which is confusing.  Make it visible.
                logger.warning(
                    "Hotkey '%s' is already bound to playlist '%s' — "
                    "replacing its binding with '%s'",
                    hotkey, existing["playlist_name"], playlist_name,
                )
            self._keybind_map[hotkey] = {
                "playlist_name": playlist_name,
                "callbacks": callbacks,
                "platform": platform,
                "_parsed": parse_hotkey(hotkey),
            }
        logger.info(
            "Registered keybind '%s' for playlist '%s' (platform=%s)",
            hotkey, playlist_name, platform,
        )

    def unregister(self, playlist_name: str):
        """Remove all keybinds registered for *playlist_name*."""
        with self._lock:
            to_remove = [
                k
                for k, v in self._keybind_map.items()
                if v["playlist_name"] == playlist_name
            ]
            for k in to_remove:
                del self._keybind_map[k]
                logger.info("Unregistered keybind '%s' for playlist '%s'", k, playlist_name)

    def match(self, pressed_keys: Set[str]) -> Optional[tuple]:
        """Return the best (specificity, hotkey_str, info) or *None*.

        "Best" means the entry whose parsed key set matches *pressed_keys*
        exactly and has the highest specificity (most keys).  This
        prevents a single-key shortcut from shadowing a multi-key combo.
        """
        best = None  # (specificity, hotkey_str, info)
        with self._lock:
            for hotkey_str, info in self._keybind_map.items():
                expected = info["_parsed"]
                if not expected:
                    continue
                if expected == pressed_keys:
                    specificity = len(expected)
                    if best is None or specificity > best[0]:
                        best = (specificity, hotkey_str, info)
        return best
