"""Unit tests for utils/config.py - settings/theme INI management.

All file I/O runs against temp paths via the module's ``SETTINGS_PATH``
and ``THEME_PATH`` constants, monkeypatched per test.  utils.config's
import-time ``profile_store.initialize()`` side effect is sandboxed by
the conftest (which re-points profile_store's file constants to the temp
tree before importing any app module).
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


class TestEnsureSettingsFile:
    def test_creates_file_with_defaults(self, paths):
        settings, _ = paths
        config.ensure_settings_file()
        assert settings.exists()
        cp = _read(settings)
        # Boolean settings are per-section with an is_true key.
        assert cp.getboolean("update_check", "is_true", fallback=None) is True
        assert cp.getboolean("global_listener", "is_true", fallback=None) is True
        assert cp.getboolean("duplicate_check", "is_true", fallback=None) is False

    def test_value_sections_written(self, paths):
        settings, _ = paths
        config.ensure_settings_file()
        cp = _read(settings)
        assert cp.get("ui_scale", "value") == "auto"
        assert cp.get("layout", "columns") == "2"
        assert cp.get("duplicate_check", "title_threshold") == "0.85"

    def test_window_geometry_defaults_written(self, paths):
        settings, _ = paths
        config.ensure_settings_file()
        cp = _read(settings)
        assert cp.getboolean("remember_geometry", "is_true", fallback=None) is True
        assert cp.getboolean("fullscreen", "is_true", fallback=None) is False
        assert cp.get("window", "geometry") == ""

    def test_window_geometry_value_round_trip(self, paths):
        settings, _ = paths
        config.set_setting_value("window", "geometry", "1100x700+120+45")
        assert config.get_setting_value("window", "geometry", "") == "1100x700+120+45"
        assert config.get_setting("remember_geometry", fallback=True) is True
        config.set_setting("remember_geometry", False)
        assert config.get_setting("remember_geometry", fallback=True) is False
        config.set_setting("fullscreen", True)
        assert config.get_setting("fullscreen", fallback=False) is True

    def test_preserves_unknown_sections(self, paths):
        settings, _ = paths
        settings.write_text("[legacy_section]\nfoo = bar\n", encoding="utf-8")
        config.ensure_settings_file()
        cp = _read(settings)
        assert cp.has_section("legacy_section")
        assert cp.get("legacy_section", "foo") == "bar"
        # Defaults were merged in alongside the legacy section.
        assert cp.has_section("update_check")

    def test_merges_missing_keys_into_existing(self, paths):
        settings, _ = paths
        settings.write_text("[update_check]\nis_true = no\n", encoding="utf-8")
        config.ensure_settings_file()
        cp = _read(settings)
        # Existing user value survives.
        assert cp.getboolean("update_check", "is_true") is False
        # Other default sections were added.
        assert cp.has_section("center_windows")

    def test_corrupt_file_self_heals(self, paths):
        settings, _ = paths
        settings.write_text("[unterminated\nno_section_here", encoding="utf-8")
        config.ensure_settings_file()  # must not raise
        cp = _read(settings)
        assert cp.getboolean("update_check", "is_true", fallback=True) is True


class TestGetSetSetting:
    def test_default_false_for_missing(self, paths):
        assert config.get_setting("some_missing", fallback=False) is False

    def test_reads_default_value(self, paths):
        assert config.get_setting("duplicate_check", fallback=False) is False

    def test_set_then_get(self, paths):
        config.set_setting("test_section", True)
        assert config.get_setting("test_section", fallback=False) is True
        config.set_setting("test_section", False)
        assert config.get_setting("test_section", fallback=True) is False

    def test_set_preserves_unknown_sections(self, paths):
        settings, _ = paths
        settings.write_text("[legacy_section]\nfoo = bar\n", encoding="utf-8")
        config.set_setting("new_section", True)
        cp = _read(settings)
        assert cp.get("legacy_section", "foo") == "bar"
        assert cp.getboolean("new_section", "is_true") is True


class TestSettingValues:
    def test_value_read_write(self, paths):
        config.set_setting_value("ui_scale", "value", "2.5")
        assert config.get_setting_value("ui_scale", "value", "1.0") == "2.5"

    def test_value_default_when_missing(self, paths):
        assert config.get_setting_value("missing", "key", "fallback") == "fallback"

    def test_value_defaults_written(self, paths):
        assert config.get_setting_value("showcase", "count", "0") == "0"


class TestEnsureThemeFile:
    def test_creates_with_default_palette(self, paths):
        _, theme = paths
        config.ensure_theme_file()
        assert theme.exists()
        cp = _read(theme)
        assert cp.has_section("root_background")
        assert cp.get("root_background", "background") == "#1A1A1A"

    def test_merges_new_default_keys(self, paths):
        _, theme = paths
        theme.write_text("[root_background]\nbackground = #111111\n", encoding="utf-8")
        config.ensure_theme_file()
        cp = _read(theme)
        # Existing user value survives.
        assert cp.get("root_background", "background") == "#111111"
        # Other default sections were added.
        assert cp.has_section("frame_main")

    def test_corrupt_theme_self_heals(self, paths):
        _, theme = paths
        theme.write_text("garbage\n[unterminated", encoding="utf-8")
        config.ensure_theme_file()  # must not raise
        cp = _read(theme)
        assert cp.get("root_background", "background") == "#1A1A1A"


class TestThemeWrites:
    def test_set_theme_value(self, paths):
        _, theme = paths
        config.set_theme_value("root_background", "background", "#FFFFFF")
        cp = _read(theme)
        assert cp.get("root_background", "background") == "#FFFFFF"

    def test_restore_theme_defaults(self, paths):
        _, theme = paths
        theme.write_text("[root_background]\nbackground = #FFFFFF\n", encoding="utf-8")
        config.restore_theme_defaults()
        cp = _read(theme)
        assert cp.get("root_background", "background") == "#1A1A1A"

    def test_apply_theme_preset_white(self, paths):
        _, theme = paths
        config.apply_theme_preset("white")
        cp = _read(theme)
        assert cp.get("root_background", "background") == "#F5F5F5"

    def test_apply_unknown_preset_is_noop(self, paths):
        _, theme = paths
        config.ensure_theme_file()
        config.apply_theme_preset("nonexistent")
        cp = _read(theme)
        assert cp.get("root_background", "background") == "#1A1A1A"