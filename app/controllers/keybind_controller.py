"""
Keybind controller — hotkey recording and flow dispatch.

Responsibilities (after the A4 split):
  1. Key event processing (recording state machine)
  2. Credential management / flow invalidation
  3. Flow controller lazy-initialisation and dispatch
  4. Listener lifecycle (global hotkey vs local tk bindings)

Delegates keybind storage/matching to :class:`KeybindRegistry` and key
normalisation to :mod:`utils.key_mapping`.
"""

import threading
import logging
from typing import Callable, Dict, Optional, Set

from pynput import keyboard
from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC
from utils.key_mapping import (
    MODIFIER_NAMES,
    normalize_key,
    normalize_tk_key,
    read_global_listener_setting,
)
from controllers.keybind_registry import KeybindCallbacks, KeybindRegistry
from services.song_manager import SongManager
from utils.theme import C

logger = logging.getLogger(__name__)


class KeybindController:
    """Orchestrates hotkey listeners, recording, and flow dispatch."""

    def __init__(self, yt_client, spotify_integration=None):
        self.yt = yt_client
        self.spotify_integration = spotify_integration
        self.song_manager: Optional[SongManager] = None
        self.keybind_thread: Optional[threading.Thread] = None

        # Flow controllers — lazily created on first keybind trigger
        self._keybind_flow = None
        self._spotify_flow = None
        self._url_receiver = None

        # Registry
        self.registry = KeybindRegistry()

        # Listener / key state
        self._pressed_keys: Set[str] = set()
        self._pressed_keys_lock = threading.Lock()
        self._listener: Optional[keyboard.Listener] = None
        self._listener_lock = threading.Lock()
        self._root = None

        # Recording state machine
        self._recording = False
        self._last_recording_combo = ""
        self._recording_callback: Optional[Callable[[str], None]] = None
        self._recording_stop_callback: Optional[Callable[[], None]] = None

        self._global_mode = read_global_listener_setting()

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

    def _stop_global_listener(self, wait: bool = True):
        with self._listener_lock:
            listener = self._listener
            self._listener = None
        if listener is not None:
            listener.stop()
            if wait:
                # pynput's X11 listener thread does not reliably exit after
                # stop() — its record_enable_context loop can stay blocked
                # indefinitely, so joining here would stall the calling
                # thread for the full timeout.  stop() already prevents any
                # further event delivery (running == False); keep this join
                # short and bounded so mode switches stay responsive.
                listener.join(timeout=0.5)
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
        name = normalize_key(key)
        if name is None:
            return
        with self._pressed_keys_lock:
            self._pressed_keys.add(name)
        self._handle_press(name)

    def _on_global_release(self, key):
        name = normalize_key(key)
        if name is None:
            return
        with self._pressed_keys_lock:
            self._pressed_keys.discard(name)

    def _on_tk_press(self, event):
        name = normalize_tk_key(event.keysym)
        if name is None:
            return
        with self._pressed_keys_lock:
            self._pressed_keys.add(name)
        self._handle_press(name)
        if self._recording:
            return "break"

    def _on_tk_release(self, event):
        name = normalize_tk_key(event.keysym)
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
            if name not in MODIFIER_NAMES:
                combo = self._build_combo()
                self._last_recording_combo = combo
                if self._recording_callback and self._root:
                    self._root.after(0, self._recording_callback, combo)
        else:
            self._check_hotkeys()

    def _build_combo(self) -> str:
        with self._pressed_keys_lock:
            snapshot = set(self._pressed_keys)
        modifiers = sorted(k for k in snapshot if k in MODIFIER_NAMES)
        non_modifiers = sorted(k for k in snapshot if k not in MODIFIER_NAMES)
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
    # Hotkey delegation
    # ------------------------------------------------------------------

    def register_hotkey(
        self,
        playlist_name: str,
        hotkey: str,
        callbacks: KeybindCallbacks,
        platform: str = PLATFORM_YOUTUBE_MUSIC,
    ):
        self.registry.register(playlist_name, hotkey, callbacks, platform)

    def unregister_hotkey(self, playlist_name: str):
        self.registry.unregister(playlist_name)

    def _check_hotkeys(self):
        with self._pressed_keys_lock:
            pressed = frozenset(self._pressed_keys)
        match = self.registry.match(pressed)
        if match is not None:
            _, hotkey_str, info = match
            playlist_name = info["playlist_name"]
            callbacks = info["callbacks"]
            platform = info.get("platform", PLATFORM_YOUTUBE_MUSIC)
            if self._root:
                self._root.after(
                    0, self.handle_keybind, playlist_name, callbacks, platform,
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
            callbacks.on_status("Busy", C["label_playlist_warn_bg"])
            logger.warning("Flow already in progress, ignoring keybind")
            return

        callbacks.on_entry_state("readonly")
        callbacks.on_status("Loading", C["label_playlist_warn_bg"])
        callbacks.on_song_info("", "")

        if not self._ensure_initialized(platform, callbacks):
            callbacks.on_reset("readonly")
            return

        def on_status(msg):
            def _apply():
                callbacks.on_status(msg, C["label_playlist_warn_bg"])
            if self._root is not None:
                self._root.after(0, _apply)

        def on_error(error_msg):
            def _apply():
                callbacks.on_reset("readonly")
                callbacks.on_status("Error", C["label_playlist_error_bg"])
            logger.error(f"Keybind flow error: {error_msg}")
            if self._root is not None:
                self._root.after(0, _apply)

        def on_success(result):
            def _apply():
                status = result.get("status", "error")
                if status == "added":
                    callbacks.on_status("Added", C["label_playlist_good_bg"])
                elif status == "exists":
                    callbacks.on_status("Exists", C["label_playlist_warn_bg"])
                else:
                    callbacks.on_status("Error", C["label_playlist_error_bg"])

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
                callbacks.on_status("Error", C["label_playlist_error_bg"])
                callbacks.on_entry_state("readonly")
                return False

        if platform == PLATFORM_SPOTIFY:
            if self._spotify_flow is not None:
                return True
            if (
                self.spotify_integration is None
                or not self.spotify_integration.is_authenticated()
            ):
                callbacks.on_status("Error", C["label_playlist_error_bg"])
                callbacks.on_entry_state("readonly")
                logger.error("Spotify not authenticated.")
                return False

            from controllers.keybind_flow import SpotifyFlowController

            self._spotify_flow = SpotifyFlowController(
                self.spotify_integration, self.song_manager
            )
            logger.info("Initialized Spotify flow")
            return True

        # YouTube Music path
        if self._keybind_flow is not None:
            return True
        if self.yt is None:
            callbacks.on_status("Error", C["label_playlist_error_bg"])
            callbacks.on_entry_state("readonly")
            logger.error("YouTube Music not authenticated.")
            return False

        try:
            from integrations.music_youtube.music_youtube_receiver import (
                URLReceiverManager,
            )
            from controllers.keybind_flow import KeybindFlowController

            self._url_receiver = URLReceiverManager()
            self._keybind_flow = KeybindFlowController(
                self.yt, self.song_manager, self._url_receiver
            )
            logger.info("Initialized YouTube Music flow")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize managers: {e}")
            callbacks.on_status("Error", C["label_playlist_error_bg"])
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

    def stop_listener(self, wait: bool = True):
        if self._global_mode:
            self._stop_global_listener(wait=wait)
        else:
            self._unbind_local_keys()

    def cleanup(self):
        # Don't wait on the listener thread at quit — it may never exit
        # (see _stop_global_listener) and the process is about to die anyway.
        self.stop_listener(wait=False)
        self.stop_receiver()
        self.song_manager = None
        self._url_receiver = None
        self._keybind_flow = None
        self._spotify_flow = None
        self.spotify_integration = None
        self.keybind_thread = None
