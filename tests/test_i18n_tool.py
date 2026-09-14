"""Tests for the i18n maintenance tooling (tools/i18n_report.py).

Covers:
* ``example.ini`` staying in sync with the code-side ``DEFAULT_STRINGS``
* the exporter being idempotent and atomic, handling % and multiline values
* ``available_languages()`` never exposing the template as a language
* the diff buckets (translated / missing / placeholder / identity) and the
  resolved-catalog overlay (bundled < user)
* the CLI subcommands incl. --strict and --write-missing

Headless-safe: no tkinter is imported anywhere here.
"""

from __future__ import annotations

import sys
from configparser import ConfigParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "app"))

import i18n_report as tool  # noqa: E402  (bare-import rule)
from utils import i18n  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect the bundled + user catalog dirs onto a temp tree."""
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    monkeypatch.setattr(i18n, "_BUNDLED_DIR", bundled)
    monkeypatch.setattr(i18n, "I18N_DIR", user)
    i18n.set_language("en")
    return bundled, user


def _rows(path: Path) -> dict[str, str]:
    cfg = ConfigParser()
    if path.exists() and path.is_file():
        cfg.read(str(path))
    return dict(cfg.items(tool.SECTION)) if cfg.has_section(tool.SECTION) else {}


def _write(dirpath: Path, name: str, rows: dict[str, str]) -> Path:
    """Write a catalog the same way the app does (%%-escaped on storage)."""
    path = dirpath / name
    cfg = ConfigParser()
    cfg.add_section(tool.SECTION)
    for key, value in rows.items():
        cfg.set(tool.SECTION, key, value.replace("%", "%%"))
    with path.open("w", encoding="utf-8") as fh:
        cfg.write(fh)
    return path


class TestExampleTemplate:
    def test_example_in_sync_with_default_strings(self):
        """The committed template must mirror DEFAULT_STRINGS, modulo the
        whitespace the INI format cannot express (see normalize_value)."""
        ref = tool._read_rows(tool._template_path())
        assert ref
        for key, value in i18n.DEFAULT_STRINGS.items():
            assert key in ref, f"missing from example.ini: {key} (run 'export')"
            assert tool.normalize_value(ref[key]) == tool.normalize_value(value), \
                f"drifted in example.ini: {key}"
        assert set(ref) == set(i18n.DEFAULT_STRINGS), "stale keys in example.ini"

    def test_example_escapes_read_back_unwrapped(self):
        ref = tool._read_rows(tool._template_path())
        for key, value in i18n.DEFAULT_STRINGS.items():
            if "%" in value or "\n" in value:
                assert tool.normalize_value(ref[key]) == tool.normalize_value(value), (
                    f"{key}: INI round-trip must preserve % and newlines")

    def test_export_idempotent_and_atomic(self, sandbox):
        bundled, user = sandbox
        first = tool.export()
        assert first == bundled / tool.TEMPLATE_NAME
        content = first.read_text(encoding="utf-8")
        assert "[strings]" in content
        assert tool.export() is None  # unchanged -> no rewrite
        assert first.read_text(encoding="utf-8") == content
        first.unlink()  # regenerate from scratch reproduces the same bytes
        assert tool.export() == first
        assert first.read_text(encoding="utf-8") == content
        # no temp litter left behind
        assert not list(bundled.glob("*.tmp"))

    def test_export_handles_multiline_and_percent(self, sandbox):
        bundled, user = sandbox
        path = tool.export()
        assert path is not None
        text = path.read_text(encoding="utf-8")
        for key, value in i18n.DEFAULT_STRINGS.items():
            if "%" in value or "\n" in value:
                assert tool.normalize_value(_rows(path)[key]) == \
                    tool.normalize_value(value), key
        assert "%%" in text


class TestAvailableLanguages:
    def test_example_not_a_language(self, sandbox):
        bundled, user = sandbox
        _write(bundled, "de.ini", {"common.ok": "OK"})
        assert i18n.available_languages() == ["de", "en"]

    def test_user_example_stem_ignored_too(self, sandbox):
        bundled, user = sandbox
        _write(user, "example.ini", {"common.ok": "ignore me"})
        assert i18n.available_languages() == ["en"]


class TestDiff:
    def test_buckets_and_overlay(self, sandbox):
        bundled, user = sandbox
        tool.export()
        template = _rows(bundled / tool.TEMPLATE_NAME)
        # bundled de: an identity row + one real translation
        _write(bundled, "de.ini", {
            "common.ok": "OK",
            "activity.trying_to_add": 'Versuche hinzuzufügen "{title}"',
        })
        # user de: overrides the bundled translation, placeholder-heals a gap
        _write(user, "de.ini", {
            "activity.trying_to_add": "USER-WINS",
            "cli.prompt_arl_cookie": i18n.DEFAULT_STRINGS["cli.prompt_arl_cookie"],
        })
        total, translated, missing, placeholders, identity = tool.diff("de")
        assert total == len(template)
        assert "common.ok" in identity
        assert "activity.trying_to_add" not in (missing + placeholders)
        assert translated == 2  # identity row + the user override (masks the bundled row)
        assert "cli.prompt_arl_cookie" in placeholders
        # without self-heal the other 410 keys have no row anywhere
        assert len(missing) == total - 3  # 3 distinct keys resolved: ok, activity, prompt_arl_cookie

    def test_placeholder_heal_scenario(self, sandbox):
        """What the app sees after self-heal: every key present as an English
        placeholder -> all gaps land in the placeholder bucket, none missing."""
        bundled, user = sandbox
        tool.export()
        template = _rows(bundled / tool.TEMPLATE_NAME)
        _write(user, "de.ini", template)  # self-healed placeholders
        total, translated, missing, placeholders, identity = tool.diff("de")
        assert total == len(i18n.DEFAULT_STRINGS)
        assert missing == []
        assert len(placeholders) == total - len(identity)
        assert translated == len(identity)

    def test_identity_rows_refer_to_real_keys(self):
        for key in tool.IDENTITY_ROWS:
            assert key in i18n.DEFAULT_STRINGS, f"stale identity row: {key}"


class TestCli:
    """argparse front-end, exercised in-process (subprocesses would miss the
    sandbox monkeypatches of i18n.I18N_DIR / _BUNDLED_DIR)."""

    def test_export_command(self, sandbox, capsys):
        bundled, user = sandbox
        assert tool.main(["export"]) == 0
        assert "wrote" in capsys.readouterr().out
        assert (bundled / tool.TEMPLATE_NAME).exists()
        assert tool.main(["export"]) == 0  # unchanged -> no rewrite
        assert "up to date" in capsys.readouterr().out

    def test_diff_command_reports(self, sandbox, capsys):
        bundled, user = sandbox
        _write(bundled, "de.ini", {"common.ok": "OK"})
        tool.export()
        assert tool.main(["diff", "de"]) == 0
        out = capsys.readouterr().out
        assert "de:" in out and "rows" in out and "still English" in out

    def test_diff_strict_exit_code(self, sandbox, capsys):
        bundled, user = sandbox
        tool.export()  # no translations anywhere
        assert tool.main(["diff", "de", "--strict"]) == 1
        assert "still English" in capsys.readouterr().out
        self._write_full_translation(bundled)
        assert tool.main(["diff", "de", "--strict"]) == 0

    def test_diff_write_missing_skeleton(self, sandbox):
        bundled, user = sandbox
        tool.export()
        out = sandbox[0] / "missing_de.ini"
        assert tool.main(["diff", "de", f"--write-missing={out}"]) == 0
        rows = _rows(out)  # must parse as INI, incl. multiline defaults
        assert len(rows) == len(i18n.DEFAULT_STRINGS)
        for key, value in rows.items():
            assert key in i18n.DEFAULT_STRINGS
            assert tool.normalize_value(value) == \
                tool.normalize_value(i18n.DEFAULT_STRINGS[key])

    @staticmethod
    def _write_full_translation(bundled):
        """Catalog covering every key with a non-English value (so the
        strict diff sees no gaps: translated + identity only)."""
        rows = {}
        for key, value in i18n.DEFAULT_STRINGS.items():
            if key in tool.IDENTITY_ROWS:
                rows[key] = value
            else:
                # suffix the value so it provably differs from the default
                rows[key] = value + " (DEO)"
        _write(bundled, "de.ini", rows)