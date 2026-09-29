"""URL-receiver port diagnostics (services/receiver_port.py).

The probes run against real loopback sockets: bind a listener on an
ephemeral port, then ask the module whether that port is in use.  That is
the only way to prove the ``EADDRINUSE`` path end to end, and it costs
microseconds - no network, no display, no plugin checkout required.

The holder-lookup helpers read ``/proc`` (Linux only), so their tests skip
everywhere else and assert the *degrade* behaviour instead: an
unidentifiable holder must never be reported as a free port.

``TestKeybindPreflight`` covers the caller: the collision has to abort
``KeybindController.handle_keybind`` - and release its busy lock, which
would otherwise wedge every later keybind - while a free port still runs
the flow.
"""

import errno
import os
import socket
from types import SimpleNamespace

import pytest

from services import receiver_port


def _free_port() -> int:
    """An ephemeral port nothing is listening on.

    The socket is closed before returning, which leaves the port in the
    bindable pool (a bare close leaves no TIME_WAIT without a connection).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def occupied_port():
    """A port with a real listener on it, for the duration of a test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _plugin(port, display_name="Test Platform", flow_type="extension"):
    return SimpleNamespace(
        receiver_port=port, display_name=display_name, id="tp", flow_type=flow_type
    )


def _receiver(running=False):
    return SimpleNamespace(is_running=lambda: running)


# ---------------------------------------------------------------------------
# port_in_use
# ---------------------------------------------------------------------------


class TestPortInUse:
    def test_free_port_reports_free(self):
        assert receiver_port.port_in_use(_free_port()) is False

    def test_listening_port_reports_busy(self, occupied_port):
        assert receiver_port.port_in_use(occupied_port) is True

    def test_out_of_range_is_not_a_conflict(self):
        # Nonsense ports are the caller's bug, not a collision - reporting
        # them as busy would abort every add for a platform with a bad
        # manifest.
        assert receiver_port.port_in_use(0) is False
        assert receiver_port.port_in_use(70000) is False
        assert receiver_port.port_in_use(-1) is False

    @pytest.mark.parametrize("bad", [None, "5000", 5000.0, True])
    def test_non_int_is_not_a_conflict(self, bad):
        assert receiver_port.port_in_use(bad) is False

    def test_privileged_port_permission_error_is_not_a_conflict(self, monkeypatch):
        # EACCES (a privileged port as a non-root user) is a different
        # failure: reporting it as a collision would point the user at the
        # wrong process.  A real bind of port 1 is not attempted.
        def _eacces(sock, address):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(socket.socket, "bind", _eacces)
        assert receiver_port.port_in_use(1) is False

    def test_socket_creation_failure_is_not_a_conflict(self, monkeypatch):
        # EMFILE and friends: the probe must never be the reason an add
        # fails, and it must not raise into the main-thread caller.
        def _nomem(*args, **kwargs):
            raise OSError(errno.EMFILE, "too many open files")

        monkeypatch.setattr(receiver_port.socket, "socket", _nomem)
        assert receiver_port.port_in_use(5000) is False

    def test_bind_failure_still_closes_the_socket(self, monkeypatch):
        # Leak check: the probe binds a socket per keybind, so an
        # unclosed one would exhaust the fd table over a long session.
        port = _free_port()  # before patching, so the helper's own socket
        closed = []         # is not counted below
        real_socket = socket.socket

        class _Tracked(real_socket):
            def close(self):
                closed.append(self)
                super().close()

        monkeypatch.setattr(
            receiver_port.socket, "socket", lambda *a, **kw: _Tracked(*a, **kw)
        )
        receiver_port.port_in_use(port)
        assert len(closed) == 1

    def test_reuseaddr_matches_werkzeug(self, monkeypatch):
        # Without SO_REUSEADDR the probe would fail on a socket lingering
        # in TIME_WAIT from the app's own previous run and abort an add
        # that the real server would have accepted.
        seen = {}
        real_setsockopt = socket.socket.setsockopt

        def _record(self, level, option, value=None):
            seen[option] = value
            return real_setsockopt(self, level, option, value) if value is not None else None

        monkeypatch.setattr(socket.socket, "setsockopt", _record)
        receiver_port.port_in_use(_free_port())
        assert seen[socket.SO_REUSEADDR] == 1


# ---------------------------------------------------------------------------
# describe_port_holder
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.isdir("/proc"), reason="holder lookup needs /proc"
)
class TestDescribePortHolder:
    def test_finds_this_process(self, occupied_port):
        holder = receiver_port.describe_port_holder(occupied_port)
        if holder is None:
            pytest.skip("holder lookup unavailable (restricted /proc)")
        assert "pid" in holder
        assert str(os.getpid()) in holder

    def test_free_port_has_no_holder(self):
        assert receiver_port.describe_port_holder(_free_port()) is None

    def test_unresolvable_holder_is_none_not_free(self, monkeypatch):
        # The contract that matters: an unidentifiable holder is reported
        # as "unknown", never as a free port.
        monkeypatch.setattr(receiver_port, "_pid_holding", lambda inodes: None)
        assert receiver_port.describe_port_holder(5000) is None

    def test_no_inodes_means_no_pid(self, monkeypatch):
        monkeypatch.setattr(receiver_port, "_listening_inodes", lambda port: set())
        assert receiver_port._pid_holding(set()) is None

    def test_pid_lookup_handles_missing_proc(self, monkeypatch):
        monkeypatch.setattr(
            receiver_port.os, "listdir", lambda path: (_ for _ in ()).throw(OSError())
        )
        assert receiver_port._pid_holding({"1"}) is None


# ---------------------------------------------------------------------------
# conflict_for - the decision the keybind controller acts on
# ---------------------------------------------------------------------------


class TestConflictFor:
    def test_occupied_port_reports_the_conflict(self, occupied_port):
        message = receiver_port.conflict_for(_plugin(occupied_port), _receiver())
        assert message is not None
        assert str(occupied_port) in message
        assert "Test Platform" in message

    def test_free_port_allows_the_add(self):
        assert receiver_port.conflict_for(_plugin(_free_port()), _receiver()) is None

    def test_our_own_running_receiver_is_exempt(self, occupied_port):
        # The receiver we built and already started holds the port on our
        # behalf.  The flow skips start() when is_running(), so reporting
        # a conflict here would abort an add that works.
        assert (
            receiver_port.conflict_for(_plugin(occupied_port), _receiver(running=True))
            is None
        )

    def test_no_receiver_means_nothing_to_collide(self, occupied_port):
        # api-type platforms (and a flow that never consults a receiver)
        # must not be blocked by a busy port.
        assert receiver_port.conflict_for(_plugin(occupied_port), None) is None

    @pytest.mark.parametrize("flow_type", ["api", "hybrid", None, "", "weird"])
    def test_non_extension_flow_is_not_preflighted(self, occupied_port, flow_type):
        # The real SoundCloud shape: flow_type "api" with a declared
        # receiver on port 5001, which under [soundcloud] capture_mode
        # "api" never starts it.  A busy port must not abort an add that
        # was never going to bind, so core only pre-flights "extension",
        # where _build_flow already guarantees a receiver exists.
        plugin = _plugin(occupied_port, flow_type=flow_type)
        assert receiver_port.conflict_for(plugin, _receiver()) is None

    def test_platform_without_a_port_is_ignored(self):
        assert receiver_port.conflict_for(_plugin(None), _receiver()) is None
        assert receiver_port.conflict_for(_plugin(0), _receiver()) is None

    def test_missing_plugin_is_ignored(self, occupied_port):
        assert receiver_port.conflict_for(None, _receiver()) is None

    def test_receiver_without_is_running_defaults_to_probe(self, occupied_port):
        # No is_running() method: treat as not running, so a real collision
        # is still caught.  Costs one probe in the normal case, never a
        # wrong verdict.
        receiver = SimpleNamespace()
        assert receiver_port.conflict_for(_plugin(occupied_port), receiver) is not None

    def test_is_running_raising_defaults_to_probe(self, occupied_port):
        receiver = SimpleNamespace(
            is_running=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert receiver_port.conflict_for(_plugin(occupied_port), receiver) is not None

    def test_message_names_the_holder_when_identifiable(self, occupied_port, monkeypatch):
        monkeypatch.setattr(
            receiver_port, "describe_port_holder", lambda port: "pid 4242 (firefox)"
        )
        message = receiver_port.conflict_for(_plugin(occupied_port), _receiver())
        assert "pid 4242 (firefox)" in message
        # The sentence must stay grammatical with no holder at all.
        monkeypatch.setattr(receiver_port, "describe_port_holder", lambda port: None)
        without = receiver_port.conflict_for(_plugin(occupied_port), _receiver())
        assert without is not None
        assert "already in use," in without or "already in use." in without
        assert "()" not in without

    def test_message_explains_the_four_site_pin(self, occupied_port):
        # The actionable part: the port cannot be changed in one place.
        message = receiver_port.conflict_for(_plugin(occupied_port), _receiver())
        assert "receiver_port" in message
        assert "host permissions" in message

    def test_falls_back_to_plugin_id_without_a_display_name(self, occupied_port):
        plugin = SimpleNamespace(
            receiver_port=occupied_port,
            display_name="",
            id="yt",
            flow_type="extension",
        )
        message = receiver_port.conflict_for(plugin, _receiver())
        assert "yt" in message

    def test_never_raises_into_the_main_thread_caller(self, occupied_port, monkeypatch):
        # handle_keybind runs this on the tkinter main thread, where an
        # exception would escape past the dispatch and leave _flow_busy
        # held.  A diagnostic that cannot be produced must read as "no
        # conflict" so the flow still gets its chance.
        monkeypatch.setattr(
            receiver_port,
            "describe_port_holder",
            lambda port: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert receiver_port.conflict_for(_plugin(occupied_port), _receiver()) is None

    def test_plugin_attribute_error_degrades_to_no_conflict(self, occupied_port):
        # A PluginInfo missing flow_type must not blow up the dispatch.
        plugin = SimpleNamespace(receiver_port=occupied_port, display_name="X")
        assert receiver_port.conflict_for(plugin, _receiver()) is None


# ---------------------------------------------------------------------------
# The caller: KeybindController.handle_keybind
# ---------------------------------------------------------------------------


class _Harness:
    """A controller wired to a fake plugin, with the flow and status calls recorded."""

    def __init__(self, port, receiver_running=False):
        import threading

        from controllers.callbacks import KeybindCallbacks
        from controllers.keybind_controller import KeybindController

        self.statuses = []
        self.resets = []
        self.flow_ran = threading.Event()

        def _execute_flow(*args, **kwargs):
            self.flow_ran.set()

        class _Flow:
            def __init__(self, integration, song_manager, receiver=None):
                self.receiver = receiver

            execute_flow = staticmethod(_execute_flow)

        self.receiver = SimpleNamespace(is_running=lambda: receiver_running)
        plugin = SimpleNamespace(
            id="tp",
            display_name="Test Platform",
            receiver_port=port,
            receiver_class=object,
            flow_class=object,
            flow_type="extension",
            import_flow=lambda: _Flow,
            build_receiver=lambda: self.receiver,
        )
        self.controller = KeybindController(
            SimpleNamespace(get=lambda pid: plugin),
            {"tp": SimpleNamespace(is_authenticated=lambda: True)},
        )
        # Skip SongManager construction (it talks to the platform API).
        self.controller.song_manager = SimpleNamespace()

        self.callbacks = KeybindCallbacks(
            on_status=lambda text, bg: self.statuses.append(text),
            on_reset=lambda state: self.resets.append(state),
        )
        self.controller.registry = SimpleNamespace(
            find=lambda *a, **kw: {"callbacks": self.callbacks}
        )

    def keybind(self):
        self.controller.handle_keybind("My Playlist", self.callbacks, "tp")

    def busy_is_free(self) -> bool:
        acquired = self.controller._flow_busy.acquire(blocking=False)
        if acquired:
            self.controller._flow_busy.release()
        return acquired


class TestKeybindPreflight:
    def test_collision_aborts_the_flow_with_the_status_code(self, occupied_port):
        h = _Harness(occupied_port)
        h.keybind()
        # The point of the pre-flight: no 30 s capture wait and no
        # misleading "the extension never sent anything".
        assert not h.flow_ran.is_set()
        assert h.statuses[-1] == "receiver_port_busy"
        assert h.statuses.count("loading") == 1  # not left spinning

    def test_collision_releases_the_busy_lock(self, occupied_port):
        # A leaked _flow_busy wedges the controller permanently: every
        # later keybind answers "busy" until restart.
        h = _Harness(occupied_port)
        h.keybind()
        assert h.busy_is_free()

    def test_collision_resets_before_reporting(self, occupied_port):
        # on_reset clears the card's status label, so the order matters:
        # the reset must come first or the message is wiped.
        h = _Harness(occupied_port)
        h.keybind()
        assert h.resets == ["readonly"]

    def test_collision_is_recorded_in_the_errors_tab(self, occupied_port, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            "controllers.keybind_controller.duplicate_queue.record_error",
            lambda *args: recorded.append(args),
        )
        h = _Harness(occupied_port)
        h.keybind()
        assert len(recorded) == 1
        # The status row has room for the code, not the paragraph.
        assert str(occupied_port) in recorded[0][2]

    def test_free_port_runs_the_flow(self):
        h = _Harness(_free_port())
        h.keybind()
        assert h.flow_ran.wait(timeout=5) is True
        assert h.statuses[-1] == "loading"  # no port_busy, flow owns the status

    def test_our_own_running_receiver_still_runs_the_flow(self, occupied_port):
        # Regression guard: the receiver we built and started holds the
        # port, and the flow reuses it (it skips start()).
        h = _Harness(occupied_port, receiver_running=True)
        h.keybind()
        assert h.flow_ran.wait(timeout=5) is True
        assert "receiver_port_busy" not in h.statuses

    def test_api_platform_without_a_receiver_is_unaffected(self, occupied_port):
        # No receiver instance => nothing will bind the port => no abort.
        h = _Harness(occupied_port)
        h.controller._flows["tp"] = SimpleNamespace(
            execute_flow=lambda *a, **kw: h.flow_ran.set()
        )
        h.keybind()
        assert h.flow_ran.wait(timeout=5) is True
        assert "receiver_port_busy" not in h.statuses

    def test_api_platform_with_a_receiver_is_unaffected(self, occupied_port):
        # SoundCloud declares receiver_class + port 5001 but is flow_type
        # "api": with capture_mode "api" the flow reads the platform
        # directly and never binds.  A busy 5001 must not abort it.
        h = _Harness(occupied_port)
        h.controller.plugin_registry.get("tp").flow_type = "api"
        h.keybind()
        assert h.flow_ran.wait(timeout=5) is True
        assert "receiver_port_busy" not in h.statuses

