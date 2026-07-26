import threading
import logging
from configparser import ConfigParser
from typing import Optional, Dict, Set, Callable

from pynput import keyboard
from utils.config import ensure_settings_file, SETTINGS_PATH

from services.song_manager import SongManager
from integrations.music_youtube.music_youtube_receiver import URLReceiverManager
from services.keybind_flow import KeybindFlowController, SpotifyFlowController

logger = logging.getLogger(__name__)

_KEY_MAP = {
    keyboard.Key.ctrl_l: "ctrl",
    keyboard.Key.ctrl_r: "ctrl",
    keyboard.Key.alt_l: "alt",
    keyboard.Key.alt_r: "alt",
    keyboard.Key.alt_gr: "alt",
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
        val = cfg.get("global_listener", "is_true", fallback="yes").lower()
        return val == "yes"
    except Exception:
        return True


class KeybindController:
    def __init__(self, yt_client, spotify_api=None):
        self.yt = yt_client
        self.spotify_api = spotify_api
        self.song_manager: Optional[SongManager] = None
        self.url_receiver: Optional[URLReceiverManager] = None
        self.keybind_flow: Optional[KeybindFlowController] = None
        self.spotify_flow: Optional[SpotifyFlowController] = None
        self.keybind_thread: Optional[threading.Thread] = None

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

        self._global_mode = _read_global_listener_setting()

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
            if self._listener is not None:
                self._listener.stop()
                self._listener = None
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

    def _handle_press(self, name: str):
        if self._recording:
            if name in ("esc", "escape", "Escape"):
                with self._pressed_keys_lock:
                    self._pressed_keys.discard(name)
                self._recording = False
                self._last_recording_combo = ""
                if self._recording_callback and self._root:
                    self._root.after(0, self._recording_callback, "")
                self._recording_callback = None
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

    def _check_hotkeys(self):
        with self._hotkey_lock:
            snapshot_map = dict(self._hotkey_map)
        with self._pressed_keys_lock:
            snapshot_keys = set(self._pressed_keys)
        for hotkey_str, info in snapshot_map.items():
            expected = _parse_hotkey(hotkey_str)
            if expected and expected.issubset(snapshot_keys):
                playlist_name = info["playlist_name"]
                labels_dict = info["labels_dict"]
                platform = info.get("platform", "youtube_music")
                if self._root:
                    self._root.after(
                        0, self.handle_keybind, playlist_name, labels_dict, platform
                    )
                break

    def start_recording(self, callback: Callable[[str], None]):
        self._recording = True
        self._last_recording_combo = ""
        self._recording_callback = callback
        logger.debug("Started recording keybind")

    def stop_recording(self) -> str:
        self._recording = False
        combo = self._last_recording_combo
        self._last_recording_combo = ""
        self._recording_callback = None
        logger.debug(f"Stopped recording keybind: {combo}")
        return combo

    def register_hotkey(
        self,
        playlist_name: str,
        hotkey: str,
        labels_dict: dict,
        platform: str = "youtube_music",
    ):
        self.unregister_hotkey(playlist_name)
        if hotkey:
            with self._hotkey_lock:
                self._hotkey_map[hotkey] = {
                    "playlist_name": playlist_name,
                    "labels_dict": labels_dict,
                    "platform": platform,
                }
            logger.info(
                f"Registered hotkey '{hotkey}' for playlist '{playlist_name}' (platform={platform})"
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
                logger.info(f"Unregistered hotkey '{k}' for playlist '{playlist_name}'")

    def handle_keybind(self, playlist_name, labels_dict, platform="youtube_music"):
        log_status_label = labels_dict["status"]
        log_artist_label = labels_dict["artist"]
        log_name_label = labels_dict["name"]
        playlist_keybind_entry = labels_dict["keybind_entry"]

        playlist_keybind_entry.config(state="readonly")
        log_status_label.config(text="Loading...", background="#4A5A00")

        if not self._ensure_initialized(
            platform, log_status_label, playlist_keybind_entry
        ):
            return

        def on_status(msg):
            def _apply():
                display_msg = msg[:5] if msg else "..."
                log_status_label.config(text=display_msg, background="#4A5A00")

            if self._root is not None:
                self._root.after(0, _apply)

        def on_error(error_msg):
            def _apply():
                log_status_label.config(text="Error", background="#A00000")

            logger.error(f"Keybind flow error: {error_msg}")
            if self._root is not None:
                self._root.after(0, _apply)

        def on_success(result):
            def _apply():
                status = result.get("status", "error")
                if status == "added":
                    log_status_label.config(text="Added", background="#006713")
                elif status == "exists":
                    log_status_label.config(text="Exists", background="#AA8800")
                else:
                    log_status_label.config(text="Error", background="#A00000")

                song_data = result.get("song", {})
                if song_data:
                    artists = song_data.get("artists", [])
                    if isinstance(artists, list):
                        artists_str = ", ".join(artists[:2])
                    else:
                        artists_str = str(artists)[:8]
                    log_artist_label.config(text=artists_str[:8])
                    log_name_label.config(text=song_data.get("title", "")[:18])

                playlist_keybind_entry.config(state="readonly")

            if self._root is not None:
                self._root.after(0, _apply)
            else:
                _apply()

        def run_flow():
            try:
                if platform == "spotify":
                    if self.spotify_flow is None:
                        on_error("Spotify not initialized")
                        return
                    self.spotify_flow.execute_flow(
                        playlist_name, on_status, on_error, on_success
                    )
                else:
                    if self.keybind_flow is None:
                        on_error("Flow not initialized")
                        return
                    self.keybind_flow.execute_flow(
                        playlist_name, on_status, on_error, on_success
                    )
            except Exception as e:
                logger.error(f"Keybind flow exception: {e}", exc_info=True)
                on_error(str(e))

        self.keybind_thread = threading.Thread(target=run_flow, daemon=True)
        self.keybind_thread.start()

    def _ensure_initialized(self, platform, log_status_label, playlist_keybind_entry):
        if self.song_manager is None:
            try:
                self.song_manager = SongManager()
            except Exception as e:
                logger.error(f"Failed to create SongManager: {e}")
                log_status_label.config(text="Error", background="#A00000")
                playlist_keybind_entry.config(state="readonly")
                return False

        if platform == "spotify":
            if self.spotify_flow is not None:
                return True
            if self.spotify_api is None:
                log_status_label.config(text="Error", background="#A00000")
                playlist_keybind_entry.config(state="readonly")
                logger.error("Spotify not authenticated.")
                return False
            self.spotify_flow = SpotifyFlowController(
                self.spotify_api, self.song_manager
            )
            logger.info("Initialized Spotify flow")
            return True
        else:
            if self.keybind_flow is not None:
                return True
            if self.yt is None:
                log_status_label.config(text="Error", background="#A00000")
                playlist_keybind_entry.config(state="readonly")
                logger.error("YouTube Music not authenticated.")
                return False
            try:
                self.url_receiver = URLReceiverManager()
                self.keybind_flow = KeybindFlowController(
                    self.yt, self.song_manager, self.url_receiver
                )
                logger.info("Initialized YouTube Music flow")
                return True
            except Exception as e:
                logger.error(f"Failed to initialize managers: {e}")
                log_status_label.config(text="Error", background="#A00000")
                playlist_keybind_entry.config(state="readonly")
                return False

    def stop_receiver(self):
        if self.url_receiver is not None:
            try:
                if self.url_receiver.is_running():
                    self.url_receiver.stop()
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
        self.url_receiver = None
        self.keybind_flow = None
        self.spotify_flow = None
        self.keybind_thread = None
