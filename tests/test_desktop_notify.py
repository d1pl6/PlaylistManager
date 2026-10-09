"""Unit tests for desktop notifications.

Two halves, both headless:

* the ``[desktop_notify]`` settings surface (utils/config.py) - the four
  opt-in flags, the fail-closed resolver, and the service/config
  category contract;
* the delivering sink (services/desktop_notify.py) - the pure body
  builder, the event-kind mapping, the settings gate, and the literal
  escaping for each OS backend (no subprocess is ever started).

File I/O runs against temp paths via ``config.SETTINGS_PATH``
(monkeypatched per test), matching tests/test_notification_settings.py.
"""

import subprocess

import pytest

from services import desktop_notify
from utils import config
from utils.i18n import DEFAULT_STRINGS, tr, tr_status


@pytest.fixture
def paths(monkeypatch, tmp_path):
    settings = tmp_path / "settings.ini"
    theme = tmp_path / "theme.ini"
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    monkeypatch.setattr(config, "THEME_PATH", theme)
    yield settings, theme


class TestDesktopNotifyDefaults:
    def test_defaults_are_all_off(self, paths):
        config.ensure_settings_file()
        for kind in config.DESKTOP_NOTIFY_KINDS:
            assert config.get_setting_value("desktop_notify", kind, "yes") == "no"

    def test_resolver_defaults_off(self, paths):
        for kind in config.DESKTOP_NOTIFY_KINDS:
            assert config.desktop_notify_enabled(kind) is False

    def test_round_trip(self, paths):
        config.set_setting_value("desktop_notify", "success", "yes")
        config.set_setting_value("desktop_notify", "errors", "yes")
        assert config.desktop_notify_enabled("success") is True
        assert config.desktop_notify_enabled("errors") is True
        assert config.desktop_notify_enabled("duplicate") is False
        config.set_setting_value("desktop_notify", "success", "no")
        assert config.desktop_notify_enabled("success") is False

    @pytest.mark.parametrize("bad", ["", "maybe", "true", "1", "Yes", "  "])
    def test_non_boolean_value_fails_closed(self, paths, bad):
        config.set_setting_value("desktop_notify", "success", bad)
        assert config.desktop_notify_enabled("success") is False

    @pytest.mark.parametrize("unknown", ["scan", "status", "ADDED", "nope"])
    def test_unknown_kind_never_notifies(self, paths, unknown):
        # Even with every real category on, a typo must not open a lane.
        for kind in config.DESKTOP_NOTIFY_KINDS:
            config.set_setting_value("desktop_notify", kind, "yes")
        assert config.desktop_notify_enabled(unknown) is False

    def test_preserves_unknown_sections(self, paths):
        settings, _ = paths
        settings.write_text("[legacy_section]\nfoo = bar\n", encoding="utf-8")
        config.set_setting_value("desktop_notify", "extra", "yes")
        assert config.desktop_notify_enabled("extra") is True
        assert "legacy_section" in settings.read_text(encoding="utf-8")


class TestCategoryContract:
    """The service's kind->category map and the settings keys must agree."""

    def test_every_mapped_category_is_a_setting(self):
        for category in desktop_notify.KIND_TO_CATEGORY.values():
            assert category in config.DESKTOP_NOTIFY_KINDS

    def test_every_setting_is_reachable_from_some_kind(self):
        assert set(desktop_notify.KIND_TO_CATEGORY.values()) == set(
            config.DESKTOP_NOTIFY_KINDS
        )

    def test_expected_kinds(self):
        assert desktop_notify.KIND_TO_CATEGORY == {
            "added": "success",
            "exists": "duplicate",
            "duplicate": "extra",
            "error": "errors",
        }


class TestSettingsLabels:
    """The four checkbox labels are looked up dynamically, so pin them."""

    def test_label_keys_exist(self):
        for kind in config.DESKTOP_NOTIFY_KINDS:
            assert f"settings.desktop_notify_{kind}" in DEFAULT_STRINGS
        assert "settings.desktop_notify" in DEFAULT_STRINGS
        assert "settings.desktop_notify_add" in DEFAULT_STRINGS
        assert "settings.desktop_notify_hint" in DEFAULT_STRINGS


class TestBuildBody:
    def test_success(self):
        assert desktop_notify.build_body(
            "success", artist="A", song="B", playlist="Chill"
        ) == tr("desktop_notify.added", song="A - B", playlist="Chill")

    def test_duplicate(self):
        assert desktop_notify.build_body(
            "duplicate", artist="A", song="B", playlist="Chill"
        ) == tr("desktop_notify.exists", song="A - B", playlist="Chill")

    def test_extra(self):
        assert desktop_notify.build_body(
            "extra", artist="A", song="B", playlist="Chill"
        ) == tr("desktop_notify.extra", song="A - B", playlist="Chill")

    def test_error_uses_status_code(self):
        assert desktop_notify.build_body(
            "errors", artist="A", song="B", status="error"
        ) == f"A - B: {tr_status('error')}"

    def test_error_unknown_status_passes_through(self):
        assert desktop_notify.build_body(
            "errors", artist="A", song="B", status="opaque msg"
        ) == "A - B: opaque msg"

    def test_error_without_identity_returns_the_message(self):
        assert desktop_notify.build_body("errors", status="error") == tr_status("error")

    def test_error_without_status_falls_back_to_generic(self):
        assert desktop_notify.build_body(
            "errors", artist="A", song="B"
        ) == f"A - B: {tr('desktop_notify.generic_error')}"

    @pytest.mark.parametrize("category", ["success", "duplicate", "extra"])
    def test_missing_identity_is_skipped(self, category):
        assert desktop_notify.build_body(category, playlist="Chill") == ""

    def test_single_field_identity_has_no_dangling_separator(self):
        assert desktop_notify.build_body(
            "success", artist="A", playlist="Chill"
        ) == tr("desktop_notify.added", song="A", playlist="Chill")

    def test_success_without_playlist_is_identity_only(self):
        # The playlist context vanished mid-flow; the identity alone still
        # says what happened, and must not end in a dangling "to".
        assert desktop_notify.build_body("success", artist="A", song="B") == "A - B"

    def test_duplicate_without_playlist_is_identity_only(self):
        assert desktop_notify.build_body(
            "duplicate", artist="A", song="B"
        ) == "A - B"

    def test_unknown_category_is_empty(self):
        assert desktop_notify.build_body("nonsense", artist="A", song="B") == ""


class TestNotificationFor:
    def test_returns_title_and_body(self):
        payload = desktop_notify.notification_for(
            "success", artist="A", song="B", playlist="Chill"
        )
        assert payload == (
            tr("app.title"),
            tr("desktop_notify.added", song="A - B", playlist="Chill"),
        )

    def test_none_when_nothing_to_say(self):
        assert desktop_notify.notification_for("success", playlist="Chill") is None
        assert desktop_notify.notification_for("nonsense", artist="A") is None


class TestNotifyEventGate:
    def _spy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            desktop_notify,
            "notify",
            lambda title, body, **kw: calls.append((title, body)) or True,
        )
        return calls

    def test_disabled_is_a_no_op(self, paths, monkeypatch):
        calls = self._spy(monkeypatch)
        assert desktop_notify.notify_event("added", artist="A", song="B") is False
        assert calls == []

    def test_mapped_kind_is_skipped_when_its_category_is_off(self, paths, monkeypatch):
        calls = self._spy(monkeypatch)
        config.set_setting_value("desktop_notify", "duplicate", "yes")
        # "added" -> success, which is still off.
        assert desktop_notify.notify_event("added", artist="A", song="B") is False
        assert calls == []

    def test_enabled_category_delivers(self, paths, monkeypatch):
        calls = self._spy(monkeypatch)
        config.set_setting_value("desktop_notify", "success", "yes")
        assert (
            desktop_notify.notify_event(
                "added", artist="A", song="B", playlist="Chill"
            )
            is True
        )
        assert calls == [
            (tr("app.title"), tr("desktop_notify.added", song="A - B", playlist="Chill"))
        ]

    @pytest.mark.parametrize("kind", ["scan", "status"])
    def test_unmapped_kinds_never_deliver(self, paths, monkeypatch, kind):
        calls = self._spy(monkeypatch)
        for category in config.DESKTOP_NOTIFY_KINDS:
            config.set_setting_value("desktop_notify", category, "yes")
        assert desktop_notify.notify_event(kind, status="x") is False
        assert calls == []

    def test_enabled_but_no_identity_is_not_delivered(self, paths, monkeypatch):
        calls = self._spy(monkeypatch)
        config.set_setting_value("desktop_notify", "success", "yes")
        assert desktop_notify.notify_event("added", playlist="Chill") is False
        assert calls == []


class TestAvailabilityProbe:
    def test_available_returns_bool(self):
        assert isinstance(desktop_notify.available(), bool)

    def test_probe_never_raises(self):
        assert isinstance(desktop_notify._probe_available(), bool)


class TestSpawnReaps:
    """``_spawn`` keeps a handle and reaps it on the next call."""

    @pytest.fixture
    def live(self):
        saved = desktop_notify._LIVE[:]
        desktop_notify._LIVE[:] = []
        yield desktop_notify._LIVE
        desktop_notify._LIVE[:] = saved

    def test_finished_children_are_reaped(self, monkeypatch, live):
        class _Exited:
            def poll(self):
                return 0

        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **k: _Exited()
        )
        desktop_notify._spawn(["x"])
        desktop_notify._spawn(["y"])
        assert live == []

    def test_running_children_are_kept(self, monkeypatch, live):
        class _Running:
            def poll(self):
                return None

        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **k: _Running()
        )
        desktop_notify._spawn(["x"])
        desktop_notify._spawn(["y"])
        assert len(live) == 2


class TestLiteralEscaping:
    """The per-OS string literals must not let a song title break out."""

    def test_apple_literal_escapes_quotes_and_backslashes(self):
        assert desktop_notify._apple_literal('a"b') == '"a\\"b"'
        assert desktop_notify._apple_literal("a\\b") == '"a\\\\b"'
        assert desktop_notify._apple_literal("a\nb") == '"a b"'

    def test_ps_literal_doubles_single_quotes(self):
        assert desktop_notify._ps_literal("it's") == "'it''s'"

    def test_windows_script_placeholders_are_replaced(self):
        script = desktop_notify._WINDOWS_TOAST_SCRIPT.replace(
            "__TITLE__", desktop_notify._ps_literal("T")
        ).replace("__BODY__", desktop_notify._ps_literal("B"))
        assert "__TITLE__" not in script
        assert "__BODY__" not in script
        assert "'T'" in script and "'B'" in script
