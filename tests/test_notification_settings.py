"""Unit tests for the [notification_bar] settings (utils/config.py).

Covers the default values merged by ``ensure_settings_file``, the
``resolve_notification_*`` helpers, the boolean helper guards, and the
pure row-mapping function ``compute_notification_rows`` that drives the
root-grid layout in ui/main_window.py.

File I/O runs against temp paths via the module's ``SETTINGS_PATH``
constant (monkeypatched per test), matching tests/test_config.py.
"""

import configparser

import pytest

from utils import config


@pytest.fixture
def paths(monkeypatch, tmp_path):
    settings = tmp_path / "settings.ini"
    theme = tmp_path / "theme.ini"
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    monkeypatch.setattr(config, "THEME_PATH", theme)
    yield settings, theme


def _read(path):
    cp = configparser.ConfigParser()
    cp.read(str(path))
    return cp


class TestNotificationDefaults:
    def test_defaults_written(self, paths):
        settings, _ = paths
        config.ensure_settings_file()
        cp = _read(settings)
        assert cp.get("notification_bar", "enabled") == "yes"
        assert cp.get("notification_bar", "song_align") == "center"
        assert cp.get("notification_bar", "position") == "below_header"
        assert cp.get("notification_bar", "duplicates") == "yes"

    def test_round_trip(self, paths):
        config.set_setting_value("notification_bar", "position", "top")
        config.set_setting_value("notification_bar", "song_align", "left")
        config.set_setting_value("notification_bar", "enabled", "no")
        config.set_setting_value("notification_bar", "duplicates", "no")
        assert (
            config.get_setting_value("notification_bar", "position", "below_header")
            == "top"
        )
        assert (
            config.get_setting_value("notification_bar", "song_align", "center")
            == "left"
        )
        assert config.get_setting_value("notification_bar", "enabled", "yes") == "no"
        assert (
            config.get_setting_value("notification_bar", "duplicates", "yes") == "no"
        )

    def test_preserves_unknown_sections(self, paths):
        settings, _ = paths
        settings.write_text("[legacy_section]\nfoo = bar\n", encoding="utf-8")
        config.set_setting_value("notification_bar", "position", "bottom")
        cp = _read(settings)
        assert cp.get("legacy_section", "foo") == "bar"
        assert cp.get("notification_bar", "position") == "bottom"


class TestResolvePosition:
    def test_default(self, paths):
        assert config.resolve_notification_position() == "below_header"

    @pytest.mark.parametrize("column,value", [("top", "top"), ("bottom", "bottom"), ("below_header", "below_header")])
    def test_valid_values(self, paths, column, value):
        config.set_setting_value("notification_bar", "position", column)
        assert config.resolve_notification_position() == value

    @pytest.mark.parametrize("bad", ["", "left", "above", "Top", "  "])
    def test_invalid_falls_back(self, paths, bad):
        config.set_setting_value("notification_bar", "position", bad)
        assert config.resolve_notification_position() == "below_header"


class TestResolveAlign:
    def test_default(self, paths):
        assert config.resolve_notification_align() == "center"

    @pytest.mark.parametrize("value", ["left", "center"])
    def test_valid_values(self, paths, value):
        config.set_setting_value("notification_bar", "song_align", value)
        assert config.resolve_notification_align() == value

    @pytest.mark.parametrize("bad", ["", "right", "Center", "middle"])
    def test_invalid_falls_back(self, paths, bad):
        config.set_setting_value("notification_bar", "song_align", bad)
        assert config.resolve_notification_align() == "center"


class TestBooleanHelpers:
    def test_enabled_default_yes(self, paths):
        assert config.notification_bar_enabled() is True
        assert config.notification_duplicates_enabled() is True

    def test_enabled_off(self, paths):
        config.set_setting_value("notification_bar", "enabled", "no")
        assert config.notification_bar_enabled() is False

    def test_duplicates_off(self, paths):
        config.set_setting_value("notification_bar", "duplicates", "no")
        assert config.notification_duplicates_enabled() is False


class TestComputeRows:
    def test_below_header_is_default_mapping(self):
        assert config.compute_notification_rows("below_header") == {
            "header": 0, "notif": 1, "search": 2, "main": 3,
        }

    def test_top_puts_bar_above_header(self):
        assert config.compute_notification_rows("top") == {
            "header": 1, "notif": 0, "search": 2, "main": 3,
        }

    def test_bottom_puts_bar_under_cards(self):
        assert config.compute_notification_rows("bottom") == {
            "header": 0, "notif": 3, "search": 1, "main": 2,
        }

    @pytest.mark.parametrize("bad", ["", "middle", "Top", None])
    def test_unknown_falls_back_to_below_header(self, bad):
        assert config.compute_notification_rows(bad) == {
            "header": 0, "notif": 1, "search": 2, "main": 3,
        }

    def test_every_position_assigns_distinct_rows(self):
        for position in config.NOTIFICATION_POSITIONS:
            rows = config.compute_notification_rows(position)
            assert sorted(rows.values()) == [0, 1, 2, 3]


class TestPromptTakeoverRule:
    """Headless test of ui.notification_bar.prompt_may_takeover.

    Importing the module creates no tkinter widgets, so the pure
    decision rule is testable without a display.
    """

    def _takeover(self, kind, prompt_shown=False):
        from ui.notification_bar import prompt_may_takeover

        return prompt_may_takeover(kind, prompt_shown)

    def test_idle_bar_gives_way(self):
        assert self._takeover(None) is True

    @pytest.mark.parametrize("kind", ["duplicate", "scan"])
    def test_duplicate_and_scan_give_way(self, kind):
        assert self._takeover(kind) is True

    @pytest.mark.parametrize("kind", ["added", "exists", "error", "status"])
    def test_terminal_events_keep_the_bar(self, kind):
        assert self._takeover(kind) is False

    def test_shown_prompt_always_refreshable(self):
        assert self._takeover("added", prompt_shown=True) is True