"""Unit tests for the quit confirmation (``[confirm_on_quit]``).

The decision itself is pure — ``app_controller.should_confirm_quit`` maps
(confirm_on_quit, hide_to_tray, tray_available) onto "ask or not" — and
``AppController.quit_app`` funnels BOTH quit sources (the window ✕ via
WM_DELETE_WINDOW and the tray "Quit" item) through the same gate, so a
single controller-level test covers the behaviour.  The dialog widget
itself is covered by the shape of its contract (the three ``QUIT_*``
outcomes), not by driving Tk.
"""

import configparser

import pytest

import utils.config as config
from controllers.app_controller import AppController, should_confirm_quit
from ui import dialogs


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    settings = tmp_path / "settings.ini"
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    config._invalidate_settings_cache()
    yield settings
    config._invalidate_settings_cache()


class _FakeTray:
    def __init__(self, available=True):
        self.available = available


class _FakeWindow:
    def __init__(self, tray_available=True, hide_result=True):
        self.tray_service = _FakeTray(tray_available)
        self.hide_calls = 0
        self._hide_result = hide_result

    def hide_to_tray(self):
        self.hide_calls += 1
        return self._hide_result


class _FakeApp:
    """Minimal ``app.app.App`` stand-in for the quit path."""

    def __init__(self, window=None):
        self.root = object()
        self.main_window = window if window is not None else _FakeWindow()
        self.cleanup_calls = 0
        self.quit_calls = 0
        self.cleanup_error = None

    def cleanup(self):
        self.cleanup_calls += 1
        if self.cleanup_error is not None:
            raise self.cleanup_error

    def quit_app(self):
        self.quit_calls += 1


@pytest.fixture
def choice(monkeypatch):
    """Capture the dialog's answer and record that it was shown.

    Returns a mutable holder: set ``holder["answer"]`` to the choice the
    user makes (``None`` = the dialog is never shown at all).
    """

    def _install(answer):
        holder = {"answer": answer, "shown": 0}
        monkeypatch.setattr(
            dialogs,
            "confirm_quit",
            lambda root: (holder.__setitem__("shown", holder["shown"] + 1)
                          or holder["answer"]),
        )
        return holder

    return _install


class TestShouldConfirmQuit:
    def test_all_three_conditions_confirm(self):
        assert should_confirm_quit(True, True, True) is True

    @pytest.mark.parametrize(
        "args",
        [
            (False, True, True),   # setting off
            (True, False, True),   # hide-to-tray off (the default)
            (True, True, False),   # no tray backend started
            (False, False, False),
            (True, False, False),
            (False, True, False),
        ],
    )
    def test_any_missing_condition_skips_the_prompt(self, args):
        # No tray to hide into, or the user never opted into hide-to-tray:
        # quitting stays a single action.
        assert should_confirm_quit(*args) is False

    def test_truthy_non_bools_do_not_leak_a_raw_value(self):
        # Callers pass config-parsed booleans, but the predicate must
        # still return a real bool (a truthy dict would leak out).
        assert should_confirm_quit(1, "yes", 1) is True
        assert should_confirm_quit(1, 0, 1) is False


class TestQuitAppConfirmation:
    def _enable(self, monkeypatch, *, confirm=True, hide=True, tray=True):
        def fake_get_setting(section, fallback=True):
            values = {
                "confirm_on_quit": confirm,
                "hide_to_tray": hide,
            }
            if section in values:
                return values[section]
            return fallback

        monkeypatch.setattr(config, "get_setting", fake_get_setting)

    def test_quit_proceeds_when_prompt_does_not_apply(self, monkeypatch, choice):
        self._enable(monkeypatch, hide=False)
        shown = choice(None)
        app = _FakeApp()
        AppController(app).quit_app()
        assert shown["shown"] == 0
        assert (app.cleanup_calls, app.quit_calls) == (1, 1)

    def test_quit_proceeds_without_a_tray_backend(self, monkeypatch, choice):
        self._enable(monkeypatch, tray=False)
        shown = choice(None)
        app = _FakeApp(window=_FakeWindow(tray_available=False))
        AppController(app).quit_app()
        assert shown["shown"] == 0
        assert (app.cleanup_calls, app.quit_calls) == (1, 1)

    def test_quit_button_tears_down(self, monkeypatch, choice):
        self._enable(monkeypatch)
        shown = choice(dialogs.QUIT_CONFIRMED)
        app = _FakeApp()
        AppController(app).quit_app()
        assert shown["shown"] == 1
        assert app.main_window.hide_calls == 0
        assert (app.cleanup_calls, app.quit_calls) == (1, 1)

    def test_cancel_aborts_the_quit_entirely(self, monkeypatch, choice):
        self._enable(monkeypatch)
        shown = choice(dialogs.QUIT_CANCELLED)
        app = _FakeApp()
        AppController(app).quit_app()
        assert shown["shown"] == 1
        assert app.main_window.hide_calls == 0
        # Nothing was torn down: no listener stopped, no DB closed.
        assert (app.cleanup_calls, app.quit_calls) == (0, 0)

    def test_hide_to_tray_button_withdraws_without_quitting(
        self, monkeypatch, choice
    ):
        self._enable(monkeypatch)
        shown = choice(dialogs.QUIT_HIDE)
        app = _FakeApp()
        AppController(app).quit_app()
        assert shown["shown"] == 1
        assert app.main_window.hide_calls == 1
        assert (app.cleanup_calls, app.quit_calls) == (0, 0)

    def test_hide_failure_falls_back_to_quitting(self, monkeypatch, choice):
        # A window withdrawn with no tray icon would be unreachable, so a
        # failed hide must not strand the app.
        self._enable(monkeypatch)
        choice(dialogs.QUIT_HIDE)
        app = _FakeApp(window=_FakeWindow(hide_result=False))
        AppController(app).quit_app()
        assert app.main_window.hide_calls == 1
        assert (app.cleanup_calls, app.quit_calls) == (1, 1)

    def test_cancel_releases_the_guard_for_a_later_request(
        self, monkeypatch, choice
    ):
        self._enable(monkeypatch)
        answer = {"value": dialogs.QUIT_CANCELLED}
        monkeypatch.setattr(
            dialogs,
            "confirm_quit",
            lambda root: answer["value"],
        )
        app = _FakeApp()
        controller = AppController(app)
        controller.quit_app()
        assert (app.cleanup_calls, app.quit_calls) == (0, 0)
        # The user quits for real on the second try.
        answer["value"] = dialogs.QUIT_CONFIRMED
        controller.quit_app()
        assert (app.cleanup_calls, app.quit_calls) == (1, 1)

    def test_duplicate_request_while_the_dialog_is_open_is_ignored(
        self, monkeypatch
    ):
        # wait_window() re-enters the main loop, so a tray click landing
        # while the modal is up would otherwise stack a second dialog.
        self._enable(monkeypatch)
        app = _FakeApp()
        controller = AppController(app)
        shown = {"n": 0}

        def reentrant(root):
            shown["n"] += 1
            controller.quit_app()  # a second request, mid-dialog
            return dialogs.QUIT_CANCELLED

        monkeypatch.setattr(dialogs, "confirm_quit", reentrant)
        controller.quit_app()
        assert shown["n"] == 1
        assert (app.cleanup_calls, app.quit_calls) == (0, 0)

    def test_cleanup_failure_still_offers_force_quit(
        self, monkeypatch, choice
    ):
        # The confirmation gate must not swallow the pre-existing
        # Force-quit / Cancel recovery path.
        self._enable(monkeypatch, hide=False)
        choice(None)
        monkeypatch.setattr(
            AppController, "_confirm_force_quit", staticmethod(lambda app, err: False)
        )
        app = _FakeApp()
        app.cleanup_error = RuntimeError("boom")
        AppController(app).quit_app()
        assert app.cleanup_calls == 1
        assert app.quit_calls == 0

    def test_missing_main_window_does_not_crash_the_gate(
        self, monkeypatch, choice
    ):
        # Startup ordering: a quit request before MainWindow exists must
        # not AttributeError out of the controller.
        self._enable(monkeypatch)
        choice(None)
        app = _FakeApp()
        app.main_window = None
        AppController(app).quit_app()
        assert app.quit_calls == 1


class TestConfirmOnQuitSetting:
    def test_default_is_on(self, settings_path):
        config.ensure_settings_file()
        cp = configparser.ConfigParser()
        cp.read(settings_path)
        assert cp.getboolean("confirm_on_quit", "is_true", fallback=None) is True

    def test_round_trip(self, settings_path):
        config.ensure_settings_file()
        config.set_setting("confirm_on_quit", False)
        cp = configparser.ConfigParser()
        cp.read(settings_path)
        assert cp.getboolean("confirm_on_quit", "is_true") is False


class TestDialogOutcomes:
    def test_outcome_constants_are_distinct(self):
        outcomes = {
            dialogs.QUIT_CANCELLED,
            dialogs.QUIT_HIDE,
            dialogs.QUIT_CONFIRMED,
        }
        assert len(outcomes) == 3

    def test_confirm_quit_without_a_live_root_returns_quit(self):
        # No window left to host a dialog (or to cancel) - quitting is the
        # only meaningful outcome; the app must never get stuck.
        class _DestroyedRoot:
            def winfo_exists(self):
                return False

        assert dialogs.confirm_quit(_DestroyedRoot()) == dialogs.QUIT_CONFIRMED
