"""
Keybind callback interface shared by the controllers and the UI.

``KeybindCallbacks`` is the controller<->UI contract for keybind flows:
the controller (``controllers/keybind_controller.py``) emits status/song/
state updates through it and never touches tkinter widgets directly; the
UI side (``ui/main_window.py``) implements it per playlist card.

It lives in its own module - not in ``keybind_registry``, which is pure
data (hotkey -> binding info), and not in the controller, which would
force the UI to import the full controller just for the type.
"""

from typing import Callable


class KeybindCallbacks:
    """Thin callback container that replaces direct widget access.

    A ``KeybindCallbacks`` instance is registered once per playlist
    and is called from worker threads - the implementation should
    route to the main thread (e.g. via ``root.after()``).

    Args:
        on_status: Called with (text, background_colour) to update status.
        on_song_info: Called with (artist_text, title_text).
        on_entry_state: Called with the new state for the keybind entry.
        on_reset: Called with entry_state when all labels should be cleared.
        on_song_added: Called (no args) after a song was added by the
            flow, so the caller can refresh song-derived UI (e.g. the
            showcase section).  Not called for "exists" results - the
            song data did not change.
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