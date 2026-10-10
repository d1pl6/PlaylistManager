"""
Keybind controller - keybind recording and flow dispatch.

Responsibilities:
  1. Key event processing (recording state machine)
  2. Credential management / flow invalidation
  3. Flow controller lazy-initialisation and dispatch
  4. Listener lifecycle (global keybind vs local tk bindings)

Delegates keybind storage/matching to :class:`KeybindRegistry` and key
normalisation to :mod:`utils.key_mapping`.
"""

import logging
import threading
from typing import Callable, Optional

from utils.key_mapping import (
    MODIFIER_NAMES,
    PYNPUT_AVAILABLE,
    keyboard,
    normalize_key,
    normalize_tk_key,
)
from utils.config import get_setting
from utils.platform import is_wayland_session
from controllers.keybind_registry import (
    KeybindCallbacks,
    KeybindRegistry,
)
from services import duplicate_queue, receiver_port, scrobble_log
from services.scrobble import (
    SCROBBLE_WRITE,
    scrobble_backends,
    scrobble_enabled_for,
)
from services.scrobble_service import scrobble_current_song
from services.playlist_store import PlaylistStore
from services.song_manager import SongManager
from utils.theme import C

logger = logging.getLogger(__name__)


class KeybindController:
    """
    Orchestrates keybind listeners, recording, and flow dispatch.
    """

    def __init__(self, plugin_registry, integrations):
        # Plugin metadata (plugin_loader.PluginRegistry) and live platform
        # clients (services.integration.IntegrationRegistry).
        self.plugin_registry = plugin_registry
        self.integrations = integrations
        self.song_manager: Optional[SongManager] = None

        self._flow_busy = threading.Lock()

        self._init_lock = threading.Lock()

        # Flow controllers / URL receivers, keyed by platform id
        self._flows: dict[str, object] = {}
        self._receivers: dict[str, object] = {}
        # Receivers parked by update_credentials() because a mid-flight flow
        # still holds their port; reclaimed in _build_flow once it unwinds.
        self._stale_receivers: dict[str, object] = {}

        self.registry = KeybindRegistry()

        # Listener / key state
        self._pressed_keys: Set[str] = set()
        self._pressed_keys_lock = threading.Lock()
        # Quoted: this annotation is evaluated at runtime (PEP 526 complex
        # target), and `keyboard` is None when pynput failed to import.
        self._listener: Optional["keyboard.Listener"] = None
        self._listener_lock = threading.Lock()
        self._root = None

        # Recording state machine
        self._recording = False
        self._last_recording_combo = ""
        self._recording_callback: Optional[Callable[[str], None]] = None
        self._recording_stop_callback: Optional[Callable[[], None]] = None

        self._global_mode = get_setting("global_listener", True)

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def update_credentials(self, refreshed_ids: Optional[list] = None):
        """
        Invalidate flow controllers so they re-initialize with fresh clients.

        Called from the UI thread after re-authentication.  Flows are dropped so they
        re-create lazily on the next keybind, picking up the new clients from
        the integration registry.

        When *refreshed_ids* is given, only those platforms' flows are
        cleared; ``None`` clears all of them.  A receiver still held by a
        mid-capture flow is parked in ``_stale_receivers`` and stopped once
        the flow unwinds, so the capture is not torn down under it.
        """
        with self._init_lock:
            if refreshed_ids is not None:
                for pid in refreshed_ids:
                    self._flows.pop(pid, None)
                    receiver = self._receivers.pop(pid, None)
                    if receiver is not None:
                        if self._flow_busy.locked():
                            self._stale_receivers[pid] = receiver
                        else:
                            self._stop_receiver_safe(pid, receiver)
            else:
                self._stop_all_receivers()
                self._flows.clear()
                self._receivers.clear()
        logger.info("KeybindController credentials updated, flows invalidated")

    def force_stop_platform(self, platform_id: str) -> None:
        """
        Stop *platform_id*'s receiver now, even if a flow is mid-capture.

        Unlike :meth:`update_credentials`, which parks a mid-capture receiver
        for later reclamation, an uninstalled platform is never built again -
        the parked receiver would hold its port until the process exits.
        """
        with self._init_lock:
            live = self._receivers.pop(platform_id, None)
            if live is not None:
                self._stop_receiver_safe(platform_id, live)
            stale = self._stale_receivers.pop(platform_id, None)
            if stale is not None:
                self._stop_receiver_safe(platform_id, stale)
            self._flows.pop(platform_id, None)
        logger.info("KeybindController force-stopped platform %s", platform_id)

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
        if not PYNPUT_AVAILABLE:
            # pynput could not reach an X server at import time - typically a
            # native Wayland session without XWayland, where DISPLAY is unset.
            # Checked before the Wayland branch because that is the more
            # specific cause and gives the more actionable message.
            logger.warning(
                "pynput is unavailable (no usable X display) - not starting "
                "the global keybinds listener. Bind compositor shortcuts to "
                "`playlistmanager -a <playlist number> or <'URL' or 'name'>` "
                "for reliable global keybinds (see CLI.MD)."
            )
            self._global_mode = False
            self._bind_local_keys()
            return
        if is_wayland_session():
            # Never start the pynput listener on a native Wayland session
            logger.warning(
                "Wayland session detected - not starting the global "
                "keybinds listener. Bind compositor shortcuts to "
                "`playlistmanager -a <playlist number> or <'URL' or 'name'>` "
                "for reliable global keybinds (see CLI.MD)."
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
                # Keep the focus-out guard so that a focus-away cancels an
                # in-progress recording consistently in both modes.
                self._bind_focus_out()
                logger.info("Global keybind listener started")
            except Exception as e:
                logger.error("Failed to start keybind listener: %s", e)
                self._listener = None

    def _stop_global_listener(self, wait: bool = True):
        with self._listener_lock:
            listener = self._listener
            self._listener = None
        self._unbind_focus_out()
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
        self._bind_focus_out()
        logger.info("Local key listener bound")

    def _unbind_local_keys(self):
        if self._root is None:
            return
        self._root.unbind("<KeyPress>")
        self._root.unbind("<KeyRelease>")
        self._unbind_focus_out()
        with self._pressed_keys_lock:
            self._pressed_keys.clear()
        logger.info("Local key listener unbound")

    def _bind_focus_out(self):
        """
        Bind the <FocusOut> guard in both local and global mode, so a
        focus-away cancels an in-progress recording either way.
        """
        if self._root is None:
            return
        self._root.bind("<FocusOut>", self._on_focus_out)

    def _unbind_focus_out(self):
        if self._root is None:
            return
        self._root.unbind("<FocusOut>")

    def _on_focus_out(self, event):
        with self._pressed_keys_lock:
            self._pressed_keys.clear()
        if self._recording:
            self._recording = False
            combo = self._last_recording_combo
            self._last_recording_combo = ""
            if combo and self._recording_callback and self._root:
                try:
                    self._root.after(0, self._recording_callback, combo)
                except Exception:
                    logger.debug("App is shutting down; dropped focus-out callback")
            self._recording_callback = None
            if self._recording_stop_callback and self._root:
                try:
                    self._root.after(0, self._recording_stop_callback)
                except Exception:
                    logger.debug(
                        "App is shutting down; dropped focus-out stop callback"
                    )
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
        """
        Schedule a recording callback on the tkinter thread, guarded.

        The recording branch of ``_handle_press`` runs on the pynput
        listener thread, so it needs the same ``after()`` guard as
        ``_check_keybinds``.
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
        return " + ".join(modifiers + non_modifiers)

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
        self._recording_stop_callback = None
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
        platform: str,
        playlist_id: str = "",
    ) -> dict | None:
        """
        Register *keybind* for *playlist_name*

        *playlist_id* disambiguates two playlists that share a name on
        one platform.
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
                platform = info["platform"]
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
                        # Also runs on the pynput listener thread; after()
                        # raises "main thread is not in main loop" for a key
                        # outside the mainloop window, which must not kill the
                        # listener.
                        logger.debug("Failed to schedule keybind dispatch: %s", e)

    # ------------------------------------------------------------------
    # Flow execution
    # ------------------------------------------------------------------

    def handle_keybind(
        self,
        playlist_name: str,
        callbacks: KeybindCallbacks,
        platform: str,
        playlist_id: str = "",
    ):
        """
        Execute the add-to-playlist flow for the given keybind.

        All UI updates go through *callbacks* so the controller never
        touches tkinter widgets directly.
        """
        if not self._flow_busy.acquire(blocking=False):
            callbacks.on_status("busy", C["label_playlist_warn_bg"])
            logger.warning("Flow already in progress, ignoring keybind")
            return

        # The match + after(0, ...) dispatch is asynchronous, so the frame may
        # have been closed (or re-bound) while the event was queued.  Running it
        # anyway would add to a removed playlist and resurrect its deleted DB
        # file - drop stale events.
        current = self.registry.find(playlist_name, platform, playlist_id)
        if current is None or current.get("callbacks") is not callbacks:
            logger.debug(
                "Keybind for '%s' (%s) is no longer active, dropping event",
                playlist_name, platform,
            )
            self._flow_busy.release()
            return

        # Resolve the stored playlist ID so the flow does not re-scan the platform
        # library by name (a full-library network round trip).  Legacy entries
        # have none and fall back to the by-name scan; the registry id pins the
        # lookup so a same-named playlist with a different id can never be picked.
        entry = PlaylistStore.find_playlist(
            playlist_name, platform=platform, playlist_id=playlist_id or ""
        )
        stored_playlist_id = (entry or {}).get("playlist_id") or None

        callbacks.on_entry_state("readonly")
        callbacks.on_status("loading", C["label_playlist_warn_bg"])
        callbacks.on_song_info("", "")

        if not self._ensure_initialized(platform, callbacks):
            callbacks.on_reset("readonly")
            self._flow_busy.release()
            return

        # Pre-flight the receiver's pinned port. A collision is invisible from
        # the flow's side: _start_receiver swallows the EADDRINUSE and the
        # capture then times out, so the user is told "the extension never sent
        # anything" about a port that was never free. Abort here instead, with
        # the port and its holder named.
        conflict = receiver_port.conflict_for(
            self.plugin_registry.get(platform),
            self._receivers.get(platform),
        )
        if conflict:
            logger.warning("Aborting add: %s", conflict)
            # Same persistence the on_error path uses, so the actionable
            # text reaches the activity window's Errors tab
            try:
                duplicate_queue.record_error(playlist_name, platform, conflict)
            except Exception:
                logger.debug("Could not log port conflict to extra.json", exc_info=True)
            callbacks.on_reset("readonly")
            callbacks.on_status("receiver_port_busy", C["status_error_bg"])
            self._flow_busy.release()
            return

        def _schedule_ui(fn):
            """
            The flow runs on a worker thread and can outlive the mainloop
            (a quit during the ~30 s receiver wait), so ``after`` may raise
            "main thread is not in main loop" / "application has been
            destroyed".  An uncaught raise would kill the flow thread with a
            traceback during shutdown.
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
            logger.debug("Keybind flow error: %s", error_msg)
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
                    callbacks.on_status("error", C["status_error_bg"]),
                )
            )

        def on_success(result):
            def _apply():
                status = result.get("status", "error")
                if status == "added":
                    callbacks.on_status("added", C["label_playlist_good_bg"])
                    # Grid "Used" sort: bump only on an actual add
                    # (reloads and exists/duplicate outcomes don't count).
                    PlaylistStore.mark_used(playlist_name, platform, playlist_id)
                elif status == "exists":
                    callbacks.on_status("exists", C["label_playlist_warn_bg"])
                elif status == "duplicate":
                    # Queued in db/extra.json, added nowhere - resolved
                    # later in the activity window's Duplicates tab.
                    callbacks.on_status("duplicate", C["label_playlist_warn_bg"])
                else:
                    callbacks.on_status("error", C["status_error_bg"])

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

                # A new song landed in the DB
                if status == "added":
                    callbacks.on_song_added()
                    # Scrobble the song if auto-scrobble is enabled and a
                    # ScrobbleCapable integration is available.
                    if get_setting("scrobble_on_add") and scrobble_enabled_for(platform):
                        if song_data and self.integrations:
                            song_id = result.get("song_id")
                            scrobble_playlist_id = stored_playlist_id or ""

                            def scrobble_async():
                                try:
                                    self._scrobble_all(song_data, lambda integ, ts: (
                                        scrobble_log.record_scrobble(
                                            platform,
                                            scrobble_playlist_id,
                                            song_id,
                                            ts,
                                            [getattr(integ, "id", "")],
                                        )
                                    ))
                                except Exception as e:
                                    logger.debug("Failed to scrobble song: %s", e)

                            threading.Thread(target=scrobble_async, daemon=True)

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
                    # The only on_error path that no flow logs itself
                    logger.error("Flow not initialized for platform '%s'", platform)
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
        """
        Return the initialized flow for *platform_id*, building it lazily.
        """
        if not self._ensure_initialized(platform_id, KeybindCallbacks()):
            return None
        return self._flows.get(platform_id)

    def flow_busy(self):
        """
        Return the global single-flow lock.

        The activity window's Add worker must hold this while it runs
        ``flow.execute_flow`` so its capture does not race an in-flight
        keybind/scrobble flow's receiver on the same local port.
        """
        return self._flow_busy

    def _scrobble_integration(self):
        """
        The scrobble backends to write to (possibly several), or ``[]``.

        Delegates to :func:`services.scrobble.scrobble_backends` so every
        scrobble path (this controller, the CLI) agrees on the set -
        deterministic platform-id order with the user's per-platform
        switches applied.
        """
        return scrobble_backends(self.integrations, SCROBBLE_WRITE)

    def _scrobble_all(self, song_data: dict, on_accepted=None) -> list:
        """
        Scrobble *song_data* to every enabled backend.

        *on_accepted* is called once per backend that accepted the listen,
        with ``(integration, timestamp)`` - the scrobble ledger records that
        so the remove path can delete exactly those scrobbles later.  A
        backend that rejects (or fails) the listen is logged and skipped so
        one dead service cannot cost the user the others' listens.

        Returns the backends that accepted.
        """
        accepted = []
        for integration in self._scrobble_integration():
            try:
                ts = integration.scrobble(song_data)
            except Exception as e:
                logger.debug(
                    "Scrobble via %s failed: %s",
                    getattr(integration, "id", "?"), e,
                )
                continue
            if ts is None:
                continue
            accepted.append(integration)
            if on_accepted is not None:
                try:
                    on_accepted(integration, ts)
                except Exception as e:
                    logger.debug("Recording the scrobble failed: %s", e)
        return accepted

    def _handle_action_keybind(self, info: dict) -> None:
        """
        Dispatch an action-type keybind (e.g., scrobble).

        Unlike playlist keybinds, action keybinds trigger standalone
        operations rather than an add-flow.
        """
        action = info.get("playlist_name", "")
        if action == "scrobble":
            self._scrobble_current_action()
        else:
            logger.warning("Unknown action keybind: %s", action)

    def _scrobble_current_action(self) -> None:
        """
        Scrobble the currently-playing song without adding to any playlist.

        Captures the current song via the first platform that reports one
        (via the shared ``scrobble_current_song`` service), then scrobbles
        it if Last.fm is available.  Runs on a worker thread to avoid
        blocking the UI during capture, and holds ``_flow_busy`` so its
        receiver does not run concurrently with an add-flow's receiver wait.
        """
        def work() -> None:
            # Non-blocking: if an add-flow is mid-capture, skip the scrobble
            # rather than queue behind it
            if not self._flow_busy.acquire(blocking=False):
                logger.warning("Flow already in progress, ignoring scrobble action")
                return
            try:
                scrobble_integ = self._scrobble_integration()
                if not scrobble_integ:
                    logger.info(
                        "No scrobble backend available (no scrobble-capable "
                        "integration, or every one has "
                        "`[<platform>] scrobbles = false`)"
                    )
                    return

                def flow_provider(platform_id: str):
                    if not self._ensure_initialized(platform_id, KeybindCallbacks()):
                        return None
                    return self._flows.get(platform_id)

                captured = scrobble_current_song(
                    self.integrations,
                    self.plugin_registry,
                    flow_provider,
                    scrobble_integ,
                )
                if captured is None:
                    logger.info("No currently-playing song found")
                    return
                song_data, ts = captured
                if ts:
                    logger.info("Scrobbled: %s", song_data.get("title", ""))
                else:
                    # A song WAS captured and no backend accepted it -
                    # distinct from the "no song found" branch above
                    logger.info(
                        "Failed to scrobble: %s - no scrobble backend "
                        "accepted the listen",
                        song_data.get("title", ""),
                    )
            except Exception as e:
                logger.error("Scrobble action failed: %s", e, exc_info=True)
            finally:
                self._flow_busy.release()

        threading.Thread(target=work, daemon=True).start()

    def _ensure_initialized(
        self, platform_id: str, callbacks: KeybindCallbacks
    ) -> bool:
        """
        Lazily initialise SongManager and the appropriate flow controller.

        Construction is data-driven from the plugin manifest: extension-type
        plugins get a URL receiver injected, api-type plugins talk to their
        platform directly.
        """
        if self.song_manager is None:
            try:
                self.song_manager = SongManager()
            except Exception as e:
                logger.error("Failed to create SongManager: %s", e)
                callbacks.on_status("error", C["status_error_bg"])
                callbacks.on_entry_state("readonly")
                return False

        if platform_id in self._flows:
            return True

        with self._init_lock:
            if platform_id in self._flows:
                return True
            return self._build_flow(platform_id, callbacks)

    def _build_flow(self, platform_id: str, callbacks: KeybindCallbacks) -> bool:
        """Build (or fetch) the flow for one platform.  Caller holds _init_lock."""
        integration = self.integrations.get(platform_id)
        if integration is None or not integration.is_authenticated():
            callbacks.on_status("error", C["status_error_bg"])
            callbacks.on_entry_state("readonly")
            logger.error("%s not authenticated.", platform_id)
            return False

        plugin = self.plugin_registry.get(platform_id)
        if plugin is None or not plugin.flow_class:
            callbacks.on_status("error", C["status_error_bg"])
            callbacks.on_entry_state("readonly")
            logger.error(
                "No keybind flow declared for platform '%s'", platform_id
            )
            return False

        # Stop any stale receiver that was parked by update_credentials()
        stale = self._stale_receivers.pop(platform_id, None)
        if stale is not None:
            self._stop_receiver_safe(platform_id, stale)

        try:
            flow_cls = plugin.import_flow()

            receiver = None
            if plugin.receiver_class:
                try:
                    receiver = plugin.build_receiver()
                except Exception as e:
                    logger.error(
                        "Failed to build receiver for %s: %s", platform_id, e
                    )

            if receiver is not None:
                flow = flow_cls(integration, self.song_manager, receiver)
            elif plugin.flow_type == "extension":
                logger.error(
                    "Extension-type plugin %s failed to build its "
                    "receiver (declared receiver_class: %s)",
                    platform_id,
                    plugin.receiver_class,
                )
                raise RuntimeError(
                    f"Extension-type plugin '{plugin.display_name}' "
                    f"failed to build its receiver "
                    f"({plugin.receiver_class})"
                )
            else:
                flow = flow_cls(integration, self.song_manager)

            # Register the receiver only after the flow has been built
            # successfully.
            if receiver is not None:
                self._receivers[platform_id] = receiver
            self._flows[platform_id] = flow
            logger.info(
                "Initialized %s flow (%s)", plugin.display_name, plugin.flow_type
            )
            return True
        except Exception as e:
            logger.error("Failed to initialize %s flow: %s", platform_id, e)
            callbacks.on_status("error", C["status_error_bg"])
            callbacks.on_entry_state("readonly")
            return False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def _stop_receiver_safe(pid: str, receiver) -> None:
        """
        Stop a URL receiver, logging errors but never raising.
        """
        try:
            receiver.stop()
        except Exception as e:
            logger.error("Error stopping URL receiver for %s: %s", pid, e)

    def _stop_all_receivers(self) -> None:
        """
        Stop every registered URL receiver and any pending stale ones.
        """
        for pid, receiver in list(self._receivers.items()):
            self._stop_receiver_safe(pid, receiver)
        for pid, receiver in list(self._stale_receivers.items()):
            self._stop_receiver_safe(pid, receiver)
        self._stale_receivers.clear()

    def stop_listener(self, wait: bool = True):
        if self._global_mode:
            self._stop_global_listener(wait=wait)
        else:
            self._unbind_local_keys()

    def cleanup(self):
        # Don't wait on the listener thread at quit
        self.stop_listener(wait=False)
        self._stop_all_receivers()
        self.song_manager = None
        self._flows.clear()
        self._receivers.clear()
        self._stale_receivers.clear()
        with self._pressed_keys_lock:
            self._pressed_keys.clear()
        self._recording = False
        self._last_recording_combo = ""
        self._recording_callback = None
        self._recording_stop_callback = None
