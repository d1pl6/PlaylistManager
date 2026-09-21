"""Unit tests for services/tray.py - the backend-selection predicates.

``_is_gtk_backend`` / ``_is_x11_tray_backend`` and the Wayland guard in
``TrayService.__init__`` are pure Python: a fake pystray module with a
controlled ``Icon.__module__`` stands in for the real backend, so no
display, no pystray installation and no GLib/DBus are needed.
``is_wayland_session()`` (the guard's other input) is exercised through
its environment variables.  The guard's consumer (``start()`` branching
between a daemon-thread ``run()`` and ``run_detached()``) is pinned too.
"""

from types import SimpleNamespace

import pytest

from services import tray
from utils import platform as platform_utils


class _SyncThread:
    """Runs the target inline - keeps ``start()`` fully synchronous."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _set_backend(monkeypatch, backend_module):
    """Install a fake pystray whose Icon reports *backend_module*."""

    def _init(self, *args, **kwargs):
        # TrayService.__init__ constructs pystray.Icon(name, image,
        # title, menu=...) - accept any call shape.
        pass

    icon_cls = type("Icon", (), {"__module__": backend_module, "__init__": _init})
    monkeypatch.setattr(tray, "_PYSTRAY_OK", True)
    monkeypatch.setattr(tray, "pystray", SimpleNamespace(Icon=icon_cls))
    return icon_cls


def _missing_pystray(monkeypatch):
    monkeypatch.setattr(tray, "_PYSTRAY_OK", False)


class TestIsGtkBackend:
    """The Gtk family (appindicator/gtk) needs its own GLib mainloop -
    ``run_detached()`` never starts one, so it must run in a daemon thread."""

    def test_missing_pystray_is_false(self, monkeypatch):
        _missing_pystray(monkeypatch)
        assert tray._is_gtk_backend() is False

    @pytest.mark.parametrize(
        "module, expected",
        [
            ("pystray._appindicator", True),
            ("pystray._gtk", True),
            ("pystray._xorg", False),
            ("pystray._darwin", False),
            ("pystray._win32", False),
        ],
    )
    def test_backend_module_classification(self, monkeypatch, module, expected):
        _set_backend(monkeypatch, module)
        assert tray._is_gtk_backend() is expected


class TestIsX11TrayBackend:
    """Whether the backend docks into an X11 system tray (xembed) - the
    gtk StatusIcon and xorg backends do; appindicator (StatusNotifierItem
    over DBus) does not, and is the only one that works under Wayland."""

    def test_missing_pystray_is_false(self, monkeypatch):
        _missing_pystray(monkeypatch)
        assert tray._is_x11_tray_backend() is False

    @pytest.mark.parametrize(
        "module, expected",
        [
            ("pystray._gtk", True),
            ("pystray._xorg", True),
            ("pystray._appindicator", False),
            ("pystray._darwin", False),
            ("pystray._win32", False),
        ],
    )
    def test_backend_module_classification(self, monkeypatch, module, expected):
        _set_backend(monkeypatch, module)
        assert tray._is_x11_tray_backend() is expected


class TestWaylandGuard:
    """An X11-system-tray backend on a Wayland session has no tray host -
    the icon would silently never appear while ``available`` stayed True.
    ``TrayService`` must degrade to unavailable instead."""

    def _init(self, monkeypatch, backend_module, wayland):
        _set_backend(monkeypatch, backend_module)
        monkeypatch.setattr(tray, "is_wayland_session", lambda: wayland)
        return tray.TrayService(image=object())

    def test_x11_backend_on_wayland_unavailable(self, monkeypatch):
        service = self._init(monkeypatch, "pystray._xorg", True)
        assert service.available is False
        assert service._icon is None

    def test_gtk_backend_on_wayland_unavailable(self, monkeypatch):
        service = self._init(monkeypatch, "pystray._gtk", True)
        assert service.available is False

    def test_appindicator_backend_on_wayland_available(self, monkeypatch):
        # StatusNotifierItem over DBus - the one mechanism that works on
        # Wayland - must NOT be degraded.
        service = self._init(monkeypatch, "pystray._appindicator", True)
        assert service.available is True

    def test_x11_backend_off_wayland_available(self, monkeypatch):
        service = self._init(monkeypatch, "pystray._xorg", False)
        assert service.available is True

    def test_missing_pystray_unavailable(self, monkeypatch):
        monkeypatch.setattr(tray, "is_wayland_session", lambda: True)
        _missing_pystray(monkeypatch)
        service = tray.TrayService(image=object())
        assert service.available is False


class TestIsWaylandSession:
    """The guard's session input: ``WAYLAND_DISPLAY`` is the reliable
    signal; ``XDG_SESSION_TYPE`` can be absent or stale."""

    @pytest.fixture
    def clean_env(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)

    def test_no_signals_false(self, clean_env):
        assert platform_utils.is_wayland_session() is False

    def test_wayland_display_set_true(self, clean_env, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert platform_utils.is_wayland_session() is True

    def test_session_type_wayland_true(self, clean_env, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert platform_utils.is_wayland_session() is True

    def test_session_type_case_insensitive(self, clean_env, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "Wayland")
        assert platform_utils.is_wayland_session() is True

    def test_session_type_x11_false(self, clean_env, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert platform_utils.is_wayland_session() is False

    def test_display_wins_over_stale_session_type(self, clean_env, monkeypatch):
        # An X11 app launched from a Wayland session still has
        # XDG_SESSION_TYPE=wayland - and so does a stale one; the display
        # env var is what actually proves a compositor is running.
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert platform_utils.is_wayland_session() is True


class TestStartBackendSelection:
    """The gtk predicate drives the run path: daemon-thread ``run()`` for
    the Gtk family (GLib mainloop), ``run_detached()`` otherwise."""

    class _FakeIcon:
        def __init__(self):
            self.ran = 0
            self.detached = False
            self.run_error = None

        def run(self):
            self.ran += 1
            if self.run_error is not None:
                raise self.run_error

        def run_detached(self):
            self.detached = True

    def _start(self, monkeypatch, backend_module, icon):
        class _Icon:
            __module__ = backend_module
            HAS_MENU = True
            HAS_DEFAULT_ACTION = True

            def __new__(cls, *args, **kwargs):
                return icon

        monkeypatch.setattr(tray, "_PYSTRAY_OK", True)
        monkeypatch.setattr(tray, "pystray", SimpleNamespace(Icon=_Icon))
        monkeypatch.setattr(tray, "is_wayland_session", lambda: False)
        monkeypatch.setattr(tray, "_Menu", lambda *items: items)
        monkeypatch.setattr(tray, "_MenuItem", lambda *args, **kwargs: args)
        monkeypatch.setattr(tray, "threading", SimpleNamespace(Thread=_SyncThread))
        service = tray.TrayService(image=object())
        service.start(on_open=lambda: None, on_quit=lambda: None)
        return service, icon

    def test_gtk_backend_runs_inline_guarded_thread(self, monkeypatch):
        icon = self._FakeIcon()
        service, icon = self._start(monkeypatch, "pystray._gtk", icon)
        assert icon.ran == 1
        assert icon.detached is False
        assert service._started is True

    def test_non_gtk_backend_uses_run_detached(self, monkeypatch):
        icon = self._FakeIcon()
        service, icon = self._start(monkeypatch, "pystray._xorg", icon)
        assert icon.detached is True
        assert icon.ran == 0
        assert service._started is True

    def test_gtk_runtime_failure_disables_icon(self, monkeypatch):
        # A backend that dies at runtime (e.g. no StatusNotifierWatcher on
        # DBus) must log and disable, not abort startup or leave a
        # stopped icon marked as started.
        icon = self._FakeIcon()
        icon.run_error = RuntimeError("no StatusNotifierWatcher")
        service, icon = self._start(monkeypatch, "pystray._gtk", icon)
        assert service.available is False
        assert service._started is False