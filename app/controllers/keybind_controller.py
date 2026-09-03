"""
Keybind controller - keybind recording and flow dispatch.

Responsibilities (after the A4 split):
  1. Key event processing (recording state machine)
  2. Credential management / flow invalidation
  3. Flow controller lazy-initialisation and dispatch
  4. Listener lifecycle (global keybind vs local tk bindings)

Delegates keybind storage/matching to :class:`KeybindRegistry` and key
normalisation to :mod:`utils.key_mapping`.
"""

import threading
import logging
from typing import Callable, Dict, Optional, Set

from pynput import keyboard
from utils.key_mapping import (
    MODIFIER_NAMES,
    normalize_key,
    normalize_tk_key,
    read_global_listener_setting,
)
from utils.config import get_setting
from utils.platform import is_wayland_session
from controllers.keybind_registry import KeybindCallbacks, KeybindRegistry
from services import duplicate_queue
from services import scrobble_log
from services.playlist_store import PlaylistStore
from services.song_manager import SongManager
from utils.theme import C

logger = logging.getLogger(__name__)


class KeybindController:
    """Orchestrates keybind listeners, recording, and flow dispatch."""

    def __init__(self, plugin_registry, integrations):
        # Plugin metadata (plugin_loader.PluginRegistry) and live platform
        # clients (services.integration.IntegrationRegistry).  Flows are
        # built lazily per platform from these two - the controller holds
        # no platform-specific references itself.
        self.plugin_registry = plugin_registry
        self.integrations = integrations
        self.song_manager: Optional[SongManager] = None

        # Guards the single in-flight flow.  Acquired synchronously here on
        # the main thread BEFORE the flow thread starts, released when the
        # flow ends - closing the race where two keybind events in the same
        # event-loop tick could both pass the busy check.
        self._flow_busy = threading.Lock()

        # Flow controllers / URL receivers, keyed by platform id -
        # lazily created on first keybind trigger for that platform.
        self._flows: Dict[str, object] = {}
        self._receivers: Dict[str, object] = {}

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

    def update_credentials(self, refreshed_ids: Optional[list] = None):
        """Invalidate flow controllers so they re-initialize with fresh clients.

        Called from the UI thread after re-authentication.  Flows are
        dropped so they will be lazily re-created on the next keybind
        trigger, pulling the new clients from the integration registry.

        When *refreshed_ids* is given, only those platforms' flows are
        cleared (a scoped refresh must not deauthenticate the platforms it
        did not touch).  ``None`` clears all flows - legacy callers that
        re-authenticated everything.

        The affected URL receivers are stopped to free their ports before
        a new receiver is created on the next keybind.
        """
        if refreshed_ids is not None:
            for pid in refreshed_ids:
                self._flows.pop(pid, None)
                receiver = self._receivers.pop(pid, None)
                if receiver is not None:
                    try:
                        receiver.stop()
                    except Exception as e:
                        logger.error("Error stopping URL receiver: %s", e)
        else:
            self.stop_receiver()
            self._flows.clear()
            self._receivers.clear()
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
        if enabled and is_wayland_session():
            # Global keybinds are impossible on native Wayland - the
            # compositor owns input and pynput's X11 listener never sees
            # keys.  Refuse the switch and keep local key bindings.
            logger.warning(
                "Global key listener is not available on Wayland - "
                "falling back to local key bindings"
            )
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
        if is_wayland_session():
            # Never start the (X11-only) pynput listener on a native
            # Wayland session - it would sit idle and give the user a
            # false "listener started" impression.
            logger.warning(
                "Wayland session detected - not starting the global "
                "keybinds listener (it cannot capture keys on native "
                "Wayland). Bind compositor shortcuts to "
                "'playlistmanager add N' for reliable global keybinds "
                "(see cli.md)."
            )
            self._global_mode = False
            self._bind_local_keys()
            return
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
                logger.info("Global keybind listener started")
            except Exception as e:
                logger.error("Failed to start keybind listener: %s", e)
                self._listener = None

    def _stop_global_listener(self, wait: bool = True):
        with self._listener_lock:
            listener = self._listener
            self._listener = None
        if listener is not None:
            listener.stop()
            if wait:
                listener.join(timeout=0.5)
            logger.info("Global keybind listener stopped")

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
                    self._schedule_recording_callback(self._recording_callback, "")
                self._recording_callback = None
                if self._recording_stop_callback and self._root:
                    self._schedule_recording_callback(self._recording_stop_callback)
                self._recording_stop_callback = None
                return
            if name not in MODIFIER_NAMES:
                combo = self._build_combo()
                self._last_recording_combo = combo
                if self._recording_callback and self._root:
                    self._schedule_recording_callback(self._recording_callback, combo)
        else:
            self._check_keybinds()

    def _schedule_recording_callback(self, callback: Callable, *args) -> None:
        """Schedule a recording callback on the tkinter thread, guarded.

        The recording branch of ``_handle_press`` runs on the pynput
        listener thread when global listening is enabled; the same
        mainloop-not-running / root-destroyed exceptions that
        ``_check_keybinds`` guards against apply here.  A stray key at
        shutdown must not kill the listener.
        """
        try:
            self._root.after(0, callback, *args)
        except Exception as e:
            logger.debug("Failed to schedule recording callback: %s", e)

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
        logger.debug("Stopped recording keybind: %s", combo)
        return combo

    # ------------------------------------------------------------------
    # Keybind delegation
    # ------------------------------------------------------------------

    def register_keybind(
        self,
        playlist_name: str,
        keybind: str,
        callbacks: KeybindCallbacks,
        platform: str = "youtube_music",
        playlist_id: str = "",
    ) -> Optional[Dict]:
        """Register *keybind* for *playlist_name*; see KeybindRegistry.register.

        *playlist_id* disambiguates two playlists that share a name on
        one platform.

        Returns the binding info displaced by this registration (a
        different playlist that owned the same keybind), or None.
        """
        return self.registry.register(
            playlist_name, keybind, callbacks, platform, playlist_id
        )

    def unregister_keybind(
        self, playlist_name: str, platform: str = "", playlist_id: str = ""
    ):
        self.registry.unregister(
            playlist_name, platform=platform, playlist_id=playlist_id
        )

    def _check_keybinds(self):
        with self._pressed_keys_lock:
            pressed = frozenset(self._pressed_keys)
        match = self.registry.match(pressed)
        if match is not None:
            _, keybind_str, info = match
            kind = info.get("kind", "playlist")
            if kind == "action":
                # Dispatch to the action handler (e.g., scrobble_current)
                if self._root:
                    try:
                        self._root.after(0, self._handle_action_keybind, info)
                    except Exception as e:
                        logger.debug("Failed to schedule action keybind dispatch: %s", e)
            else:
                # Dispatch to the playlist add-flow handler
                playlist_name = info["playlist_name"]
                callbacks = info["callbacks"]
                # Legacy bindings predate the platform field - they were all
                # YouTube Music entries.
                platform = info.get("platform", "youtube_music")
                playlist_id = info.get("playlist_id", "")
                if self._root:
                    try:
                        self._root.after(
                            0,
                            self.handle_keybind,
                            playlist_name,
                            callbacks,
                            platform,
                            playlist_id,
                        )
                    except Exception as e:
                        # _check_keybinds also runs on the pynput listener
                        # thread; after() raises "main thread is not in main
                        # loop" when a key lands outside the mainloop
                        # (startup/shutdown window).  A stray key must not
                        # kill the listener.
                        logger.debug("Failed to schedule keybind dispatch: %s", e)

    # ------------------------------------------------------------------
    # Flow execution
    # ------------------------------------------------------------------

    def handle_keybind(
        self,
        playlist_name: str,
        callbacks: KeybindCallbacks,
        platform: str = "youtube_music",
        playlist_id: str = "",
    ):
        """Execute the add-to-playlist flow for the given keybind.

        All UI updates go through *callbacks* so the controller never
        touches tkinter widgets directly.
        """
        if not self._flow_busy.acquire(blocking=False):
            callbacks.on_status("Busy", C["label_playlist_warn_bg"])
            logger.warning("Flow already in progress, ignoring keybind")
            return

        # The match + after(0, ...) dispatch is asynchronous - the frame
        # may have been closed (or its keybind re-bound) while the event
        # was queued.  Running the flow anyway would add the song to a
        # playlist the user removed from the window and resurrect its
        # deleted local DB file, so drop stale events.
        current = self.registry.find(playlist_name, platform, playlist_id)
        if current is None or current.get("callbacks") is not callbacks:
            logger.debug(
                "Keybind for '%s' (%s) is no longer active, dropping event",
                playlist_name, platform,
            )
            self._flow_busy.release()
            return

        # Resolve the stored playlist ID so the flow does not re-scan the
        # platform library by name (a full-library network round trip) - the
        # store persisted the ID at add-playlist time.  Legacy entries have
        # none, and the flow then falls back to the by-name scan.  The id
        # from the registry binding pins the lookup so a same-named
        # playlist with a different id can never be picked.
        entry = PlaylistStore.find_playlist(
            playlist_name, platform=platform, playlist_id=playlist_id or ""
        )
        stored_playlist_id = (entry or {}).get("playlist_id") or None

        callbacks.on_entry_state("readonly")
        callbacks.on_status("Loading", C["label_playlist_warn_bg"])
        callbacks.on_song_info("", "")

        if not self._ensure_initialized(platform, callbacks):
            callbacks.on_reset("readonly")
            self._flow_busy.release()
            return

        def _schedule_ui(fn):
            """Marshal *fn* to the main thread; drop it if the app is gone.

            The flow runs on a worker thread and can outlive the mainloop
            (a quit during the ~30 s receiver wait).  ``after`` raises
            "main thread is not in main loop" / "application has been
            destroyed" in that window, and an uncaught raise here would
            escape execute_flow's error handler and kill the flow thread
            with a traceback during shutdown.  Same guard as
            ``_check_keybinds``.
            """
            if self._root is None:
                return
            try:
                self._root.after(0, fn)
            except Exception:
                logger.debug("App is shutting down; dropped flow UI update")

        def on_status(msg):
            _schedule_ui(
                lambda: callbacks.on_status(msg, C["label_playlist_warn_bg"])
            )

        def on_error(error_msg):
            logger.error("Keybind flow error: %s", error_msg)
            # Persist for the activity window's Errors tab - the red card
            # status alone is gone at the next keybind.
            if self.integrations.get(platform) is None:
                # The platform was uninstalled mid-flow (Manage dialog):
                # its purge already ran, so recording an error now would
                # resurrect a dead platform in the Errors tab forever.
                logger.debug(
                    "Platform '%s' no longer registered - skipping error record",
                    platform,
                )
            else:
                try:
                    duplicate_queue.record_error(playlist_name, platform, error_msg)
                except Exception:
                    logger.debug("Could not log flow error to extra.json", exc_info=True)
            _schedule_ui(
                lambda: (
                    callbacks.on_reset("readonly"),
                    callbacks.on_status("Error", C["label_playlist_error_bg"]),
                )
            )

        def on_success(result):
            def _apply():
                status = result.get("status", "error")
                if status == "added":
                    callbacks.on_status("Added", C["label_playlist_good_bg"])
                elif status == "exists":
                    callbacks.on_status("Exists", C["label_playlist_warn_bg"])
                elif status == "duplicate":
                    # Queued in db/extra.json, added nowhere - resolved
                    # later in the activity window's Duplicates tab.
                    callbacks.on_status("Dup?", C["label_playlist_warn_bg"])
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

                # A new song landed in the DB - let the UI refresh any
                # song-derived sections (showcase).  "exists" results are
                # skipped above: the song data did not change.
                if status == "added":
                    callbacks.on_song_added()
                    # Scrobble the song if auto-scrobble is enabled and a ScrobbleCapable
                    # integration is available.  An accepted scrobble is
                    # recorded in the scrobble ledger so the remove-song
                    # path can later delete THIS exact scrobble (not the
                    # track's most recent one).
                    if get_setting("scrobble_on_add"):
                        song_data = result.get("song", {})
                        if song_data and self.integrations:
                            song_id = result.get("song_id")
                            playlist_id = stored_playlist_id or ""

                            def scrobble_async():
                                try:
                                    scrobble_integ = next(
                                        (integ for integ in self.integrations.get_all().values()
                                         if getattr(integ, "scrobble", None) is not None),
                                        None,
                                    )
                                    if scrobble_integ is not None:
                                        ts = scrobble_integ.scrobble(song_data)
                                        if ts is not None and song_id is not None:
                                            scrobble_log.record_scrobble(
                                                platform, playlist_id, song_id, ts
                                            )
                                except Exception as e:
                                    logger.debug("Failed to scrobble song: %s", e)

                            threading.Thread(target=scrobble_async, daemon=True).start()

            if self._root is not None:
                _schedule_ui(_apply)
            else:
                logger.warning(
                    "Cannot apply success result: root window unavailable"
                )

        def run_flow():
            try:
                flow = self._flows.get(platform)
                if flow is None:
                    on_error("Flow not initialized")
                    return
                flow.execute_flow(
                    playlist_name, on_status, on_error, on_success,
                    playlist_id=stored_playlist_id,
                )
            except Exception as e:
                logger.error("Keybind flow exception: %s", e, exc_info=True)
                on_error(str(e))
            finally:
                self._flow_busy.release()

        threading.Thread(target=run_flow, daemon=True).start()

    def get_flow(self, platform_id: str):
        """Return the initialized flow for *platform_id*, building it lazily.

        Public accessor for the activity window's Add action, which runs
        ``execute_flow`` itself with the queued record's pre-captured
        url/song_data (the keybind path builds flows through
        ``handle_keybind`` instead).  Returns None when initialization
        fails (not authenticated, no flow declared) - the caller is
        responsible for reporting that; the no-op callbacks here keep
        init-failure silent instead of poking a card widget.
        """
        if not self._ensure_initialized(platform_id, KeybindCallbacks()):
            return None
        return self._flows.get(platform_id)

    def _handle_action_keybind(self, info: dict) -> None:
        """Dispatch an action-type keybind (e.g., scrobble).

        Unlike playlist keybinds which drive add-flows, action keybinds
        trigger standalone operations. Currently supports scrobble-only.
        """
        action = info.get("playlist_name", "")  # action name stored as playlist_name
        if action == "scrobble":
            self._scrobble_current_action()
        else:
            logger.warning("Unknown action keybind: %s", action)

    def _scrobble_current_action(self) -> None:
        """Scrobble the currently-playing song without adding to any playlist.

        Captures the current song via the platform's existing capture path
        (exactly as add-flow would), then scrobbles it if Last.fm is available.
        Runs on a worker thread to avoid blocking the UI during capture.

        Holds ``_flow_busy`` for the capture + scrobble so a standalone
        scrobble action does not run its receiver concurrently with an
        add-flow's receiver wait (both are extension-capture on the same
        local port) and double-handle the same command.
        """
        def work() -> None:
            # Non-blocking: if an add-flow is mid-capture, skip the scrobble
            # rather than queue behind it (a stale/duplicate command is
            # worse than no-op).
            if not self._flow_busy.acquire(blocking=False):
                logger.warning("Flow already in progress, ignoring scrobble action")
                return
            try:
                scrobble_integ = next(
                    (integ for integ in self.integrations.get_all().values()
                     if getattr(integ, "scrobble", None) is not None),
                    None,
                )
                if scrobble_integ is None:
                    logger.info("Last.fm integration not available for scrobble")
                    return

                # Try each authenticated platform's capture path until one succeeds
                for platform_id, integration in self.integrations.get_all().items():
                    if not integration.is_authenticated():
                        continue
                    
                    plugin = self.plugin_registry.get(platform_id)
                    if plugin is None or not plugin.flow_class:
                        continue

                    try:
                        # Ensure flow is initialized for this platform.  The
                        # KeybindCallbacks is a throwaway: it is only used on
                        # the LazyInit path, and that path is skipped (early
                        # return) when the flow is already in ``self._flows``,
                        # so the real add-flow callbacks are never displaced.
                        if not self._ensure_initialized(platform_id, KeybindCallbacks()):
                            continue

                        flow = self._flows.get(platform_id)
                        if flow is None:
                            continue

                        # Capture the current song using the flow's capture path
                        url, song_data, error = flow.capture(30)  # 30s timeout

                        if song_data:
                            # Found a currently-playing song, scrobble it
                            result = scrobble_integ.scrobble(song_data)
                            if result:
                                logger.info("Scrobbled: %s", song_data.get("title", ""))
                            else:
                                logger.debug("Failed to scrobble: %s", song_data.get("title", ""))
                            return
                        else:
                            logger.debug("No song playing on %s: %s", platform_id, error)
                    except Exception as e:
                        logger.debug("Failed to scrobble from %s: %s", platform_id, e)
                        continue

                logger.info("No currently-playing song found")
            except Exception as e:
                logger.error("Scrobble action failed: %s", e, exc_info=True)
            finally:
                self._flow_busy.release()

        threading.Thread(target=work, daemon=True).start()

    def _ensure_initialized(
        self, platform_id: str, callbacks: KeybindCallbacks
    ) -> bool:
        """Lazily initialise SongManager and the appropriate flow controller.

        Construction is data-driven from the plugin manifest: extension-type
        plugins get a URL receiver injected, api-type plugins talk to their
        platform directly.  No platform-specific branches here - adding a
        platform never touches this file.
        """
        if self.song_manager is None:
            try:
                self.song_manager = SongManager()
            except Exception as e:
                logger.error("Failed to create SongManager: %s", e)
                callbacks.on_status("Error", C["label_playlist_error_bg"])
                callbacks.on_entry_state("readonly")
                return False

        if platform_id in self._flows:
            return True

        integration = self.integrations.get(platform_id)
        if integration is None or not integration.is_authenticated():
            callbacks.on_status("Error", C["label_playlist_error_bg"])
            callbacks.on_entry_state("readonly")
            logger.error("%s not authenticated.", platform_id)
            return False

        plugin = self.plugin_registry.get(platform_id)
        if plugin is None or not plugin.flow_class:
            callbacks.on_status("Error", C["label_playlist_error_bg"])
            callbacks.on_entry_state("readonly")
            logger.error(
                "No keybind flow declared for platform '%s'", platform_id
            )
            return False

        try:
            flow_cls = plugin.import_flow()

            receiver = None
            if plugin.receiver_class:
                # A plugin that declares a receiver_class gets its receiver
                # built and passed to the flow regardless of flow_type.  For
                # extension-type platforms this is the receiver they need; for
                # api/hybrid platforms (e.g. SoundCloud) the flow decides
                # whether to consult it, so a browser-extension capture path
                # can be wired in without the manifest being "extension".
                try:
                    receiver = plugin.build_receiver()
                    self._receivers[platform_id] = receiver
                except Exception as e:
                    logger.error(
                        "Failed to build receiver for %s: %s", platform_id, e
                    )

            if receiver is not None:
                flow = flow_cls(integration, self.song_manager, receiver)
            elif plugin.flow_type == "extension":
                logger.error(
                    "Extension-type plugin %s declares no receiver_class",
                    platform_id,
                )
                raise RuntimeError(
                    f"Extension-type plugin '{plugin.display_name}' is missing "
                    "its receiver_class"
                )
            else:
                # "api" type - reads the platform directly, no receiver.
                flow = flow_cls(integration, self.song_manager)

            self._flows[platform_id] = flow
            logger.info(
                "Initialized %s flow (%s)", plugin.display_name, plugin.flow_type
            )
            return True
        except Exception as e:
            logger.error("Failed to initialize %s flow: %s", platform_id, e)
            callbacks.on_status("Error", C["label_playlist_error_bg"])
            callbacks.on_entry_state("readonly")
            return False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop_receiver(self):
        """Stop every URL receiver (all extension-type platforms)."""
        for receiver in list(self._receivers.values()):
            try:
                receiver.stop()
            except Exception as e:
                logger.error("Error stopping URL receiver: %s", e)

    def stop_listener(self, wait: bool = True):
        if self._global_mode:
            self._stop_global_listener(wait=wait)
        else:
            self._unbind_local_keys()

    def cleanup(self):
        # Don't wait on the listener thread at quit - it may never exit
        # (see _stop_global_listener) and the process is about to die anyway.
        self.stop_listener(wait=False)
        self.stop_receiver()
        self.song_manager = None
        self._flows.clear()
        self._receivers.clear()
