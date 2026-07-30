"""
Keybind controller — hotkey recording, matching, and dispatch.

Modified for Issue #3: the controller no longer directly manipulates
tkinter widgets.  Instead, callers register a ``KeybindCallbacks``
instance that exposes typed UI-update methods.
"""

import threading
import logging
from configparser import ConfigParser
from typing import Callable, Dict, Optional, Set

from pynput import keyboard
from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC
from utils.config import ensure_settings_file, SETTINGS_PATH

from services.song_manager import SongManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Callback interface (replaces raw labels_dict)
# ---------------------------------------------------------------------------


class KeybindCallbacks:
    """Thin callback container that replaces direct widget access.

    A ``KeybindCallbacks`` instance is registered once per playlist
    and is called from worker threads — the implementation should
    route to the main thread (e.g. via ``root.after()``).
    """

    def __init__(
        self,
        on_status: Callable[[str, str], None] = lambda text, bg: None,
        on_song_info: Callable[[str, str], None] = lambda artist, name: None,
        on_entry_state: Callable[[str], None] = lambda state: None,
        on_reset: Callable[[str], None] = lambda entry_state: None,
    ):
        """
        Args:
            on_status: Called with (text, background_colour) to update status.
            on_song_info: Called with (artist_text, title_text).
            on_entry_state: Called with the new state for the keybind entry.
            on_reset: Called with entry_state when all labels should be cleared.
        """
        self.on_status = on_status
        self.on_song_info = on_song_info
        self.on_entry_state = on_entry_state
        self.on_reset = on_reset


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

_KEY_MAP = {
    keyboard.Key.ctrl_l: "ctrl",
    keyboard.Key.ctrl_r: "ctrl",
    keyboard.Key.alt_l: "alt",
    keyboard.Key.alt_r: "alt",
    # Note: AltGr deliberately omitted — it is a distinct modifier on European
    # layouts and should NOT be conflated with Alt. Keep the generic "alt" entry
    # only for actual Alt keys; AltGr falls through to key.name = "alt_gr".
    keyboard.Key.shift: "shift",
    keyboard.Key.shift_l: "shift",
    keyboard.Key.shift_r: "shift",
    keyboard.Key.cmd: "cmd",
    keyboard.Key.cmd_l: "cmd",
    keyboard.Key.cmd_r: "cmd",
}

_TK_KEY_MAP = {
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Control": "ctrl",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Alt": "alt",
    "Alt_gr": "alt",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Shift": "shift",
    "Super_L": "cmd",
    "Super_R": "cmd",
    "Escape": "escape",
}

_MODIFIER_NAMES = {"ctrl", "alt", "shift", "cmd"}


def _normalize_key(key) -> Optional[str]:
    if key in _KEY_MAP:
        return _KEY_MAP[key]
    if isinstance(key, keyboard.KeyCode) and key.char:
        return key.char.lower()
    if not isinstance(key, keyboard.KeyCode) and hasattr(key, "name"):
        return key.name
    return None


def _normalize_tk_key(keysym: str) -> Optional[str]:
    if keysym in _TK_KEY_MAP:
        return _TK_KEY_MAP[keysym]
    if len(keysym) == 1:
        return keysym.lower()
    if keysym.startswith("F") and keysym[1:].isdigit():
        return keysym.lower()
    if keysym in (
        "space",
        "Return",
        "BackSpace",
        "Tab",
        "Delete",
        "Home",
        "End",
        "Left",
        "Right",
        "Up",
        "Down",
        "Prior",
        "Next",
    ):
        return keysym.lower()
    return None


def _parse_hotkey(hotkey_str: str) -> Set[str]:
    return {k.strip().lower() for k in hotkey_str.split("+") if k.strip()}


def _read_global_listener_setting() -> bool:
    ensure_settings_file()
    cfg = ConfigParser()
    try:
        cfg.read(str(SETTINGS_PATH))
        return cfg.getboolean("global_listener", "is_true", fallback=True)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class KeybindController:
    def __init__(self, yt_client, spotify_integration=None):
        self.yt = yt_client
        self.spotify_integration = spotify_integration
        self.song_manager: Optional[SongManager] = None
        self.keybind_thread: Optional[threading.Thread] = None

        # Flow controllers — lazily created on first keybind trigger
        self._keybind_flow = None
        self._spotify_flow = None
        self._url_receiver = None

        self._hotkey_map: Dict[str, Dict] = {}
        self._hotkey_lock = threading.Lock()
        self._pressed_keys: Set[str] = set()
        self._pressed_keys_lock = threading.Lock()
        self._listener: Optional[keyboard.Listener] = None
        self._listener_lock = threading.Lock()
        self._root = None

        self._recording = False
        self._last_recording_combo = ""
        self._recording_callback: Optional[Callable[[str], None]] = None
        self._recording_stop_callback: Optional[Callable[[], None]] = None

        self._global_mode = _read_global_listener_setting()

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def update_credentials(self, yt_client=None, spotify_integration=None):
        """Update API clients and invalidate active flow controllers.

        Called from the UI thread after re-authentication.  The flow
        controllers are set to None so they will be lazily re-created
        on the next keybind trigger with the new credentials.

        The old URL receiver is stopped to free port 5000 before a new
        receiver is created on the next keybind.
        """
        self.stop_receiver()
        if yt_client is not None:
            self.yt = yt_client
        if spotify_integration is not None:
            self.spotify_integration = spotify_integration
        self._keybind_flow = None
        self._spotify_flow = None
        self._url_receiver = None
        logger.info("KeybindController credentials updated, flows invalidated")

    # ------------------------------------------------------------------
    # Root window & listener mode
    # ------------------------------------------------------------------

    def set_root(self, root):
        self._root = root
        if self._global_mode:
            self._start_global_listener()
        else:
            self._bind_local_keys()

    def set_global_listener(self, enabled: bool):
        if self._global_mode == enabled:
            return
        self._global_mode = enabled
        if enabled:
            self._unbind_local_keys()
            self._start_global_listener()
            logger.info("Switched to global key listener")
        else:
            self._stop_global_listener()
            self._bind_local_keys()
            logger.info("Switched to local key listener")

    def _start_global_listener(self):
        with self._listener_lock:
            if self._listener is not None:
                return
            try:
                self._listener = keyboard.Listener(
                    on_press=self._on_global_press,
                    on_release=self._on_global_release,
                )
                self._listener.daemon = True
                self._listener.start()
                logger.info("Global hotkey listener started")
            except Exception as e:
                logger.error(f"Failed to start hotkey listener: {e}")
                self._listener = None

    def _stop_global_listener(self):
        with self._listener_lock:
            listener = self._listener
            self._listener = None
        if listener is not None:
            listener.stop()
            listener.join(timeout=2.0)
            logger.info("Global hotkey listener stopped")

    def _bind_local_keys(self):
        if self._root is None:
            return
        self._root.bind("<KeyPress>", self._on_tk_press)
        self._root.bind("<KeyRelease>", self._on_tk_release)
        self._root.bind("<FocusOut>", self._on_focus_out)
        logger.info("Local key listener bound")

    def _unbind_local_keys(self):
        if self._root is None:
            return
        self._root.unbind("<KeyPress>")
        self._root.unbind("<KeyRelease>")
        self._root.unbind("<FocusOut>")
        with self._pressed_keys_lock:
            self._pressed_keys.clear()
        logger.info("Local key listener unbound")

    def _on_focus_out(self, event):
        with self._pressed_keys_lock:
            self._pressed_keys.clear()
        if self._recording:
            self._recording = False
            combo = self._last_recording_combo
            self._last_recording_combo = ""
            if self._recording_callback and self._root:
                self._root.after(0, self._recording_callback, combo)
            self._recording_callback = None
            if self._recording_stop_callback and self._root:
                self._root.after(0, self._recording_stop_callback)
            self._recording_stop_callback = None

    # ------------------------------------------------------------------
    # Key press / release
    # ------------------------------------------------------------------

    def _on_global_press(self, key):
        name = _normalize_key(key)
        if name is None:
            return
        with self._pressed_keys_lock:
            self._pressed_keys.add(name)
        self._handle_press(name)

    def _on_global_release(self, key):
        name = _normalize_key(key)
        if name is None:
            return
        with self._pressed_keys_lock:
            self._pressed_keys.discard(name)

    def _on_tk_press(self, event):
        name = _normalize_tk_key(event.keysym)
        if name is None:
            return
        with self._pressed_keys_lock:
            self._pressed_keys.add(name)
        self._handle_press(name)
        if self._recording:
            return "break"

    def _on_tk_release(self, event):
        name = _normalize_tk_key(event.keysym)
        if name is None:
            return
        with self._pressed_keys_lock:
            self._pressed_keys.discard(name)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _handle_press(self, name: str):
        if self._recording:
            if name == "escape":
                with self._pressed_keys_lock:
                    self._pressed_keys.discard(name)
                self._recording = False
                self._last_recording_combo = ""
                if self._recording_callback and self._root:
                    self._root.after(0, self._recording_callback, "")
                self._recording_callback = None
                if self._recording_stop_callback and self._root:
                    self._root.after(0, self._recording_stop_callback)
                self._recording_stop_callback = None
                return
            if name not in _MODIFIER_NAMES:
                combo = self._build_combo()
                self._last_recording_combo = combo
                if self._recording_callback and self._root:
                    self._root.after(0, self._recording_callback, combo)
        else:
            self._check_hotkeys()

    def _build_combo(self) -> str:
        with self._pressed_keys_lock:
            snapshot = set(self._pressed_keys)
        modifiers = sorted(k for k in snapshot if k in _MODIFIER_NAMES)
        non_modifiers = sorted(
            k for k in snapshot if k not in _MODIFIER_NAMES
        )
        return "+".join(modifiers + non_modifiers)

    def start_recording(
        self,
        callback: Callable[[str], None],
        on_stop: Callable[[], None] | None = None,
    ):
        self._recording = True
        self._last_recording_combo = ""
        self._recording_callback = callback
        self._recording_stop_callback = on_stop
        logger.debug("Started recording keybind")

    def stop_recording(self) -> str:
        self._recording = False
        combo = self._last_recording_combo
        self._last_recording_combo = ""
        self._recording_callback = None
        logger.debug(f"Stopped recording keybind: {combo}")
        return combo

    # ------------------------------------------------------------------
    # Hotkey registry
    # ------------------------------------------------------------------

    def register_hotkey(
        self,
        playlist_name: str,
        hotkey: str,
        callbacks: KeybindCallbacks,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
    ):
        """Register a hotkey + callbacks for a playlist.

        Args:
            playlist_name: Name of the playlist.
            hotkey: Combo string (e.g. ``"ctrl+shift+a"``).
            callbacks: :class:`KeybindCallbacks` for UI updates.
            platform: Platform identifier.
        """
        self.unregister_hotkey(playlist_name)
        if hotkey:
            with self._hotkey_lock:
                self._hotkey_map[hotkey] = {
                    "playlist_name": playlist_name,
                    "callbacks": callbacks,
                    "platform": platform,
                    "_parsed": _parse_hotkey(hotkey),
                }
            logger.info(
                f"Registered hotkey '{hotkey}' for playlist '{playlist_name}' "
                f"(platform={platform})"
            )

    def unregister_hotkey(self, playlist_name: str):
        with self._hotkey_lock:
            to_remove = [
                k
                for k, v in self._hotkey_map.items()
                if v["playlist_name"] == playlist_name
            ]
            for k in to_remove:
                del self._hotkey_map[k]
                logger.info(
                    f"Unregistered hotkey '{k}' for playlist '{playlist_name}'"
                )

    # ------------------------------------------------------------------
    # Hotkey matching
    # ------------------------------------------------------------------

    def _check_hotkeys(self):
        with self._hotkey_lock:
            snapshot_map = dict(self._hotkey_map)
        with self._pressed_keys_lock:
            snapshot_keys = frozenset(self._pressed_keys)

        best_match = None  # (specificity, hotkey_str, info)
        for hotkey_str, info in snapshot_map.items():
            expected = info["_parsed"]
            if not expected:
                continue
            if expected == snapshot_keys:
                specificity = len(expected)
                if best_match is None or specificity > best_match[0]:
                    best_match = (specificity, hotkey_str, info)

        if best_match is not None:
            _, hotkey_str, info = best_match
            playlist_name = info["playlist_name"]
            callbacks = info["callbacks"]
            platform = info.get("platform", PLATFORM_YOUTUBE_MUSIC)
            if self._root:
                self._root.after(
                    0, self.handle_keybind, playlist_name, callbacks, platform
                )

    # ------------------------------------------------------------------
    # Flow execution
    # ------------------------------------------------------------------

    def handle_keybind(
        self,
        playlist_name: str,
        callbacks: KeybindCallbacks,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
    ):
        """Execute the add-to-playlist flow for the given keybind.

        All UI updates go through *callbacks* so the controller never
        touches tkinter widgets directly.
        """
        if self.keybind_thread is not None and self.keybind_thread.is_alive():
            callbacks.on_status("Busy", "#AA8800")
            logger.warning("Flow already in progress, ignoring keybind")
            return

        callbacks.on_entry_state("readonly")
        callbacks.on_status("Loading...", "#4A5A00")
        callbacks.on_song_info("", "")

        if not self._ensure_initialized(platform, callbacks):
            callbacks.on_reset("readonly")
            return

        def on_status(msg):
            def _apply():
                display_msg = msg[:5] if msg else "..."
                callbacks.on_status(display_msg, "#4A5A00")
            if self._root is not None:
                self._root.after(0, _apply)

        def on_error(error_msg):
            def _apply():
                callbacks.on_reset("readonly")
                callbacks.on_status("Error", "#A00000")
            logger.error(f"Keybind flow error: {error_msg}")
            if self._root is not None:
                self._root.after(0, _apply)

        def on_success(result):
            def _apply():
                status = result.get("status", "error")
                if status == "added":
                    callbacks.on_status("Added", "#006713")
                elif status == "exists":
                    callbacks.on_status("Exists", "#AA8800")
                else:
                    callbacks.on_status("Error", "#A00000")

                song_data = result.get("song", {})
                if song_data:
                    artists = song_data.get("artists", [])
                    if isinstance(artists, list):
                        artists_str = ", ".join(artists[:2])
                    else:
                        artists_str = str(artists)[:8]
                    title = song_data.get("title", "")[:18]
                    callbacks.on_song_info(artists_str[:8], title)

                callbacks.on_entry_state("readonly")

            if self._root is not None:
                self._root.after(0, _apply)
            else:
                logger.warning(
                    "Cannot apply success result: root window unavailable"
                )

        def run_flow():
            try:
                if platform == PLATFORM_SPOTIFY:
                    if self._spotify_flow is None:
                        on_error("Spotify not initialized")
                        return
                    self._spotify_flow.execute_flow(
                        playlist_name, on_status, on_error, on_success
                    )
                else:
                    if self._keybind_flow is None:
                        on_error("Flow not initialized")
                        return
                    self._keybind_flow.execute_flow(
                        playlist_name, on_status, on_error, on_success
                    )
            except Exception as e:
                logger.error(f"Keybind flow exception: {e}", exc_info=True)
                on_error(str(e))

        self.keybind_thread = threading.Thread(target=run_flow, daemon=True)
        self.keybind_thread.start()

    def _ensure_initialized(
        self, platform: str, callbacks: KeybindCallbacks
    ) -> bool:
        """Lazily initialise SongManager and the appropriate flow controller."""
        if self.song_manager is None:
            try:
                self.song_manager = SongManager()
            except Exception as e:
                logger.error(f"Failed to create SongManager: {e}")
                callbacks.on_status("Error", "#A00000")
                callbacks.on_entry_state("readonly")
                return False

        if platform == PLATFORM_SPOTIFY:
            if self._spotify_flow is not None:
                return True
            if (
                self.spotify_integration is None
                or not self.spotify_integration.is_authenticated()
            ):
                callbacks.on_status("Error", "#A00000")
                callbacks.on_entry_state("readonly")
                logger.error("Spotify not authenticated.")
                return False

            from services.keybind_flow import SpotifyFlowController

            self._spotify_flow = SpotifyFlowController(
                self.spotify_integration, self.song_manager
            )
            logger.info("Initialized Spotify flow")
            return True

        # YouTube Music path
        if self._keybind_flow is not None:
            return True
        if self.yt is None:
            callbacks.on_status("Error", "#A00000")
            callbacks.on_entry_state("readonly")
            logger.error("YouTube Music not authenticated.")
            return False

        try:
            from integrations.music_youtube.music_youtube_receiver import (
                URLReceiverManager,
            )
            from services.keybind_flow import KeybindFlowController

            self._url_receiver = URLReceiverManager()
            self._keybind_flow = KeybindFlowController(
                self.yt, self.song_manager, self._url_receiver
            )
            logger.info("Initialized YouTube Music flow")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize managers: {e}")
            callbacks.on_status("Error", "#A00000")
            callbacks.on_entry_state("readonly")
            return False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop_receiver(self):
        receiver = self._url_receiver
        if receiver is not None:
            try:
                receiver.stop()
            except Exception as e:
                logger.error(f"Error stopping URL receiver: {e}")

    def stop_listener(self):
        if self._global_mode:
            self._stop_global_listener()
        else:
            self._unbind_local_keys()

    def cleanup(self):
        self.stop_listener()
        self.stop_receiver()
        self.song_manager = None
        self._url_receiver = None
        self._keybind_flow = None
        self._spotify_flow = None
        self.spotify_integration = None
        self.keybind_thread = None
