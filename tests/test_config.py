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
        # Value settings have their own keys per section.
        assert cp.get("update_check", "interval") == "launch"
        assert cp.get("update_check", "last_check") == "0"
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
        assert cp.getboolean("start_in_tray", "is_true", fallback=None) is False

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

    def test_legacy_update_check_boolean_migrated(self, paths):
        settings, _ = paths
        # The pre-interval era wrote [update_check] is_true = no.
        settings.write_text(
            "[update_check]\nis_true = no\n", encoding="utf-8"
        )
        config.ensure_settings_file()
        cp = _read(settings)
        # A disabled legacy flag must not become "launch" (which would
        # silently re-enable a check on every launch) - it maps to "off".
        assert cp.get("update_check", "interval") == "off"
        # Dead key preserved, defaults for the new keys added alongside.
        assert cp.getboolean("update_check", "is_true") is False
        assert cp.get("update_check", "last_check") == "0"
        # Other default sections were added.
        assert cp.has_section("center_windows")

    def test_legacy_update_check_enabled_maps_to_launch(self, paths):
        settings, _ = paths
        settings.write_text(
            "[update_check]\nis_true = yes\n", encoding="utf-8"
        )
        config.ensure_settings_file()
        cp = _read(settings)
        assert cp.get("update_check", "interval") == "launch"

    def test_legacy_migration_never_overwrites_existing_interval(self, paths):
        settings, _ = paths
        settings.write_text(
            "[update_check]\nis_true = no\ninterval = 24h\n",
            encoding="utf-8",
        )
        config.ensure_settings_file()
        cp = _read(settings)
        assert cp.get("update_check", "interval") == "24h"

    def test_corrupt_file_self_heals(self, paths):
        settings, _ = paths
        settings.write_text("[unterminated\nno_section_here", encoding="utf-8")
        config.ensure_settings_file()  # must not raise
        cp = _read(settings)
        assert cp.get("update_check", "interval") == "launch"


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

    def test_update_check_interval_round_trip(self, paths):
        config.set_setting_value("update_check", "interval", "24h")
        assert (
            config.get_setting_value("update_check", "interval", "launch")
            == "24h"
        )


class TestSectionScopedBool:
    """``[<platform>] scrobbles`` and friends: a boolean under a section.

    The section name is dynamic (a plugin id), so DEFAULT_SETTINGS cannot
    ship one - the reader therefore has to be right about a *missing* key
    and about a hand-edited one, since neither can be caught in review.
    """

    def test_missing_key_reads_as_the_fallback(self, paths):
        assert config.get_setting_bool("lastfm", "scrobbles", fallback=True) is True
        assert config.get_setting_bool("lastfm", "scrobbles") is False

    def test_round_trip(self, paths):
        config.set_setting_bool("lastfm", "scrobbles", False)
        assert config.get_setting_bool("lastfm", "scrobbles", fallback=True) is False
        config.set_setting_bool("lastfm", "scrobbles", True)
        assert config.get_setting_bool("lastfm", "scrobbles", fallback=False) is True

    @pytest.mark.parametrize(
        "stored,expected",
        [
            ("false", False), ("no", False), ("off", False), ("0", False),
            ("true", True), ("yes", True), ("on", True), ("1", True),
        ],
    )
    def test_every_spelling_configparser_accepts_is_understood(
        self, paths, stored, expected
    ):
        # A user hand-editing settings.ini writes whichever spelling they
        # know; only the documented one may be honored.
        settings, _ = paths
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(f"[lastfm]\nscrobbles = {stored}\n", encoding="utf-8")
        config._invalidate_settings_cache()
        assert config.get_setting_bool("lastfm", "scrobbles", fallback=True) is expected

    def test_an_unreadable_value_falls_back_instead_of_raising(self, paths):
        # The whole module's contract on a user file: never raise, and hand
        # the decision to the caller's fallback.
        settings, _ = paths
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("[lastfm]\nscrobbles = perhaps\n", encoding="utf-8")
        config._invalidate_settings_cache()
        assert config.get_setting_bool("lastfm", "scrobbles", fallback=True) is True
        assert config.get_setting_bool("lastfm", "scrobbles", fallback=False) is False

    def test_writing_creates_the_section_and_keeps_the_rest(self, paths):
        settings, _ = paths
        settings.write_text(
            "[legacy_section]\nfoo = bar\n[soundcloud]\ncapture_mode = hybrid\n",
            encoding="utf-8",
        )
        config.set_setting_bool("listenbrainz", "scrobbles", False)
        cp = _read(settings)
        assert cp.get("legacy_section", "foo") == "bar"
        assert cp.get("soundcloud", "capture_mode") == "hybrid"
        assert cp.getboolean("listenbrainz", "scrobbles") is False

    def test_the_option_name_is_the_documented_one(self):
        assert config.SCROBBLES_OPTION == "scrobbles"


class TestThumbnailSizeCap:
    def test_default_is_no_cap(self, paths):
        assert config.resolve_thumbnail_size_cap() == 0

    @pytest.mark.parametrize("cap", config.THUMBNAIL_SIZE_CAPS)
    def test_every_preset_resolves(self, paths, cap):
        config.set_setting_value("thumbnails", "max_size", str(cap))
        assert config.resolve_thumbnail_size_cap() == cap

    @pytest.mark.parametrize(
        "stored", ["999", "-5", "0.5", "abc", "", "  ", None, "12.5", "128.0"]
    )
    def test_unknown_values_fall_back_to_no_cap(self, paths, stored):
        # An arbitrary cap is either a no-op or a silent quality loss, so
        # anything outside the tuple is discarded rather than trusted.
        config.set_setting_value("thumbnails", "max_size", str(stored))
        assert config.resolve_thumbnail_size_cap() == 0

    def test_whitespace_is_tolerated(self, paths):
        config.set_setting_value("thumbnails", "max_size", " 256 ")
        assert config.resolve_thumbnail_size_cap() == 256

    def test_default_lands_in_a_fresh_settings_file(self, paths):
        # ensure_settings_file must ship the key, or the resolver would read
        # the fallback on every fetch until the user first touches Settings.
        settings, _ = paths
        config.ensure_settings_file()
        assert _read(settings).get("thumbnails", "max_size") == "0"

    def test_added_key_survives_the_merge_into_a_user_file(self, paths):
        # An existing user config keeps its own cap across an upgrade.
        settings, _ = paths
        settings.write_text("[thumbnails]\nmode = cache\nmax_size = 512\n", encoding="utf-8")
        config.ensure_settings_file()
        cp = _read(settings)
        assert cp.get("thumbnails", "mode") == "cache"
        assert cp.get("thumbnails", "max_size") == "512"


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