"""Unit tests for named-theme CRUD in utils/config.py.

The test run's global temp redirect (conftest) points every profile-rooted
path at a throwaway tree; each test further re-points ``THEMES_DIR`` and
``THEME_PATH`` at a fresh tmp_path so saved themes never leak between tests
or touch the real ``cfg/theme.ini``.
"""

import pytest

from configparser import ConfigParser

from utils import config as c


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    themes = tmp_path / "cfg" / "themes"
    theme_ini = tmp_path / "cfg" / "theme.ini"
    monkeypatch.setattr(c, "THEMES_DIR", themes)
    monkeypatch.setattr(c, "THEME_PATH", theme_ini)
    return themes, theme_ini


def _read_ini(path):
    cfg = ConfigParser()
    cfg.read(str(path))
    return {s: dict(cfg[s]) for s in cfg.sections()}


def _theme_palette(theme_file):
    return _read_ini(theme_file)


def _seed_current_theme(theme_ini, tag):
    cfg = ConfigParser()
    cfg["root_background"] = {"background": tag}
    from utils.config import _write_ini_file
    _write_ini_file(theme_ini, cfg)


class TestListThemes:
    def test_empty_when_dir_missing(self, sandbox):
        themes, _ = sandbox
        assert c.list_themes() == []

    def test_sorted_and_only_ini(self, sandbox):
        themes, _ = sandbox
        themes.mkdir(parents=True)
        (themes / "zeta.ini").write_text("", encoding="utf-8")
        (themes / "alpha.ini").write_text("", encoding="utf-8")
        (themes / "notes.txt").write_text("", encoding="utf-8")
        assert c.list_themes() == ["alpha", "zeta"]


class TestSaveTheme:
    def test_saves_current_palette(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("work")
        saved = _theme_palette(themes / "work.ini")
        assert saved["root_background"]["background"] == "#111111"

    def test_creates_themes_dir(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("work")
        assert (themes / "work.ini").exists()

    def test_overwrite_allowed(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("work")
        _seed_current_theme(theme_ini, "#222222")
        c.save_theme("work")
        saved = _theme_palette(themes / "work.ini")
        assert saved["root_background"]["background"] == "#222222"

    def test_empty_name_raises(self, sandbox):
        with pytest.raises(ValueError):
            c.save_theme("  ")

    def test_illegal_name_raises(self, sandbox):
        for bad in ("space name", "slash/name", ".hidden", "?"):
            with pytest.raises(ValueError):
                c.save_theme(bad)

    def test_reserved_names_raises(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        for reserved in ("Default Theme", "default theme", "WHITE THEME", "White Theme"):
            with pytest.raises(ValueError):
                c.save_theme(reserved)


class TestApplyTheme:
    def test_round_trip(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("work")
        # Change the active palette; applying must restore the saved one.
        _seed_current_theme(theme_ini, "#222222")
        c.apply_theme("work")
        active = _read_ini(theme_ini)
        assert active["root_background"]["background"] == "#111111"

    def test_missing_theme_raises(self, sandbox):
        with pytest.raises(ValueError):
            c.apply_theme("nope")

    def test_corrupt_theme_raises(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("broken")
        # An empty/corrupt theme file must never clobber the live palette.
        (themes / "broken.ini").write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="corrupt"):
            c.apply_theme("broken")
        assert _read_ini(theme_ini)["root_background"]["background"] == "#111111"

    def test_apply_is_full_palette_copy(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("work")
        saved = _theme_palette(themes / "work.ini")
        c.apply_theme("work")
        assert _read_ini(theme_ini) == saved


class TestDeleteTheme:
    def test_delete_removes_file(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("work")
        c.delete_theme("work")
        assert not (themes / "work.ini").exists()
        assert c.list_themes() == []

    def test_delete_missing_raises(self, sandbox):
        with pytest.raises(ValueError):
            c.delete_theme("nope")

    def test_delete_builtin_raises(self, sandbox):
        with pytest.raises(ValueError):
            c.delete_theme("Default Theme")


class TestRenameTheme:
    def test_rename_moves_file(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("work")
        c.rename_theme("work", "playlist")
        assert not (themes / "work.ini").exists()
        assert (themes / "playlist.ini").exists()
        assert c.list_themes() == ["playlist"]

    def test_rename_collision_raises(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("work")
        c.save_theme("other")
        with pytest.raises(ValueError):
            c.rename_theme("work", "other")

    def test_rename_missing_raises(self, sandbox):
        with pytest.raises(ValueError):
            c.rename_theme("nope", "other")

    def test_rename_rejects_reserved_name(self, sandbox):
        themes, theme_ini = sandbox
        _seed_current_theme(theme_ini, "#111111")
        c.save_theme("work")
        with pytest.raises(ValueError):
            c.rename_theme("work", "White Theme")