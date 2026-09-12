"""Per-platform scrobble gate (services/scrobble.py)."""

from __future__ import annotations

import pytest

from services.scrobble import SCROBBLE_NONE, scrobble_enabled_for, split_platforms


# -- split_platforms ----------------------------------------------------------

class TestSplitPlatforms:
    """Normalisation, dedup, and empties."""

    def test_basic(self):
        assert split_platforms("youtube_music,spotify") == ["youtube_music", "spotify"]

    def test_whitespace(self):
        assert split_platforms(" yt , spotify  ") == ["yt", "spotify"]

    def test_empty_string(self):
        assert split_platforms("") == []

    def test_none_value(self):
        assert split_platforms(None) == []

    def test_dedup_preserves_first_occurrence(self):
        assert split_platforms("a,b,a,c") == ["a", "b", "c"]

    def test_single_platform(self):
        assert split_platforms("spotify") == ["spotify"]

    def test_trailing_comma(self):
        assert split_platforms("a,b,") == ["a", "b"]

    def test_only_commas(self):
        assert split_platforms(",,") == []


# -- scrobble_enabled_for ----------------------------------------------------

class TestScrobbleEnabledFor:
    """Decision truth table against a minimal settings fixture."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, monkeypatch):
        """Minimal settings state — no real INI touched."""
        store = {"scrobble_on_add": True, "scrobble_platforms": ""}

        from services import scrobble as _mod

        def fake_get_setting(section, fallback=True):
            if section == "scrobble_on_add":
                return bool(store["scrobble_on_add"])
            return fallback

        def fake_get_setting_value(section, option, fallback=""):
            if section == "scrobble" and option == "platforms":
                return store["scrobble_platforms"]
            return fallback

        monkeypatch.setattr(_mod, "get_setting", fake_get_setting)
        monkeypatch.setattr(_mod, "get_setting_value", fake_get_setting_value)

        self._store = store  # type: ignore[attr-defined]

    # Master gate ----------------------------------------------------------------

    def test_master_off_returns_false(self):
        self._store["scrobble_on_add"] = False
        assert scrobble_enabled_for("youtube_music") is False

    def test_master_off_returns_false_even_when_platform_listed(self):
        self._store["scrobble_on_add"] = False
        self._store["scrobble_platforms"] = "youtube_music"
        assert scrobble_enabled_for("youtube_music") is False

    # Empty/absent storage = all -------------------------------------------------

    def test_empty_storage_returns_true(self):
        self._store["scrobble_platforms"] = ""
        assert scrobble_enabled_for("youtube_music") is True
        assert scrobble_enabled_for("spotify") is True

    def test_absent_storage_returns_true(self):
        """Settings key missing from the INI returns '' (fallback)."""
        assert scrobble_enabled_for("spotify") is True

    # Explicit platform list ------------------------------------------------------

    def test_listed_platform_returns_true(self):
        self._store["scrobble_platforms"] = "youtube_music"
        assert scrobble_enabled_for("youtube_music") is True

    def test_unlisted_platform_returns_false(self):
        self._store["scrobble_platforms"] = "youtube_music"
        assert scrobble_enabled_for("spotify") is False

    def test_multi_platform(self):
        self._store["scrobble_platforms"] = "youtube_music,spotify"
        assert scrobble_enabled_for("youtube_music") is True
        assert scrobble_enabled_for("spotify") is True
        assert scrobble_enabled_for("soundcloud") is False

    # SCROBBLE_NONE sentinel ------------------------------------------------------

    def test_none_sentinel_returns_false(self):
        self._store["scrobble_platforms"] = SCROBBLE_NONE
        assert scrobble_enabled_for("youtube_music") is False
        assert scrobble_enabled_for("spotify") is False

    def test_none_sentinel_master_off_stays_false(self):
        self._store["scrobble_on_add"] = False
        self._store["scrobble_platforms"] = SCROBBLE_NONE
        assert scrobble_enabled_for("youtube_music") is False
