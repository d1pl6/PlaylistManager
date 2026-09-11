"""Unit tests for the removal confirmations.

Playlist-removal decision (pure, no Tk): ``resolve_playlist_removal_mode``
maps the two settings (ask on/off, [remove_playlist] default) onto the
action taken by the playlist card ✕: full removal ("remove"), keep the
local SQLite DB ("keep_db"), or the deferred dialog path (None when
asking is on).  Song removal has no default-setting anymore -- the ask
is a plain yes/no (full removal is the only action).
"""

import configparser

import pytest

from utils.config import (
    REMOVE_PLAYLIST_DEFAULT_MODE,
    REMOVE_PLAYLIST_MODE_LABELS,
    ensure_settings_file,
    set_setting,
    set_setting_value,
)
from ui.card_grid import resolve_playlist_removal_mode

import utils.config as config


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    settings = tmp_path / "settings.ini"
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    yield settings


class TestResolvePlaylistRemovalMode:
    def test_ask_on_defers_to_dialog(self):
        # Asking enabled: the close dialog decides - the default is not
        # consulted.
        assert resolve_playlist_removal_mode(True, "remove") is None
        assert resolve_playlist_removal_mode(True, "keep_db") is None

    def test_ask_off_uses_default(self):
        assert resolve_playlist_removal_mode(False, "remove") == "remove"
        assert resolve_playlist_removal_mode(False, "keep_db") == "keep_db"

    def test_unknown_default_falls_back_to_full_removal(self):
        # A corrupted/stale INI value must not change behaviour: fall
        # back to the full removal.
        assert (
            resolve_playlist_removal_mode(False, "explode_the_disk")
            == REMOVE_PLAYLIST_DEFAULT_MODE
            == "remove"
        )

    def test_modes_cover_both_actions(self):
        assert set(REMOVE_PLAYLIST_MODE_LABELS) == {"remove", "keep_db"}
        assert REMOVE_PLAYLIST_DEFAULT_MODE == "remove"


class TestRemovalSettings:
    def test_defaults_merged_and_written(self, settings_path):
        ensure_settings_file()
        cp = configparser.ConfigParser()
        cp.read(settings_path)
        # Both confirmations default ON (destructive actions); the
        # playlist default is the full removal (old behaviour when the
        # dialog is off).  There is no [remove_song] default anymore.
        assert cp.getboolean("confirm_on_song_remove", "is_true", fallback=None) is True
        assert cp.getboolean("confirm_on_playlist_remove", "is_true", fallback=None) is True
        assert cp.get("remove_playlist", "default", fallback=None) == "remove"
        assert not cp.has_section("remove_song")

    def test_setting_round_trip(self, settings_path):
        ensure_settings_file()
        set_setting("confirm_on_playlist_remove", False)
        set_setting_value("remove_playlist", "default", "keep_db")
        cp = configparser.ConfigParser()
        cp.read(settings_path)
        assert cp.getboolean("confirm_on_playlist_remove", "is_true") is False
        assert cp.get("remove_playlist", "default") == "keep_db"