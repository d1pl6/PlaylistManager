"""Unit tests for utils/i18n.py - language catalogs and tr()/trn().

File I/O runs against tmp_path via the module's ``I18N_DIR`` /
``_BUNDLED_DIR`` constants, monkeypatched per test (same pattern as
tests/test_theme_store.py).  The active language/catalog state is reset
to English between tests.
"""

from configparser import ConfigParser
from pathlib import Path

import pytest

from utils import config as c
from utils import i18n


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    user_dir = tmp_path / "cfg" / "i18n"
    bundled_dir = tmp_path / "bundled"
    bundled_dir.mkdir(parents=True)
    monkeypatch.setattr(i18n, "I18N_DIR", user_dir)
    monkeypatch.setattr(i18n, "_BUNDLED_DIR", bundled_dir)
    i18n.set_language("en")
    yield user_dir, bundled_dir
    i18n.set_language("en")


def _write_catalog(directory, lang, mapping):
    """Write a catalog file ``<directory>/<lang>.ini`` with *mapping*."""
    directory.mkdir(parents=True, exist_ok=True)
    cfg = ConfigParser()
    cfg[i18n.STRING_SECTION] = mapping
    path = directory / f"{lang}.ini"
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)
    return path


def _read_catalog(path):
    cfg = ConfigParser()
    cfg.read(str(path))
    return {s: dict(cfg[s]) for s in cfg.sections()}


class TestTrBasics:
    def test_english_default_when_no_catalog(self, sandbox):
        i18n.set_language("fr")
        assert i18n.tr("common.ok") == "OK"

    def test_unknown_key_returns_key(self, sandbox):
        assert i18n.tr("no.such.key") == "no.such.key"

    def test_format_substitution(self, sandbox):
        _write_catalog(sandbox[0], "de", {"greeting": "Hallo {name}!"})
        i18n.set_language("de")
        assert i18n.tr("greeting", name="Welt") == "Hallo Welt!"

    def test_braces_not_mangled_without_fmt(self, sandbox):
        _write_catalog(sandbox[0], "de", {"template": "literal {x} stays"})
        i18n.set_language("de")
        assert i18n.tr("template") == "literal {x} stays"

    def test_missing_fmt_arg_does_not_raise(self, sandbox):
        _write_catalog(sandbox[0], "de", {"no_arg": "Value {missing}"})
        i18n.set_language("de")
        assert i18n.tr("no_arg") == "Value {missing}"


class TestCatalogSources:
    def test_user_translation_wins_over_bundled(self, sandbox):
        user_dir, bundled_dir = sandbox
        _write_catalog(bundled_dir, "de", {"common.ok": "bundled"})
        _write_catalog(user_dir, "de", {"common.ok": "user"})
        i18n.set_language("de")
        assert i18n.tr("common.ok") == "user"

    def test_bundled_serves_when_user_missing(self, sandbox):
        _, bundled_dir = sandbox
        _write_catalog(bundled_dir, "de", {"common.ok": "bundled"})
        i18n.set_language("de")
        assert i18n.tr("common.ok") == "bundled"

    def test_missing_key_falls_back_english(self, sandbox):
        user_dir, _ = sandbox
        _write_catalog(user_dir, "de", {"common.ok": "OK"})
        i18n.set_language("de")
        assert i18n.tr("common.delete") == "Delete"

    def test_en_resets_to_defaults(self, sandbox):
        user_dir, _ = sandbox
        _write_catalog(user_dir, "de", {"common.ok": "OK"})
        i18n.set_language("de")
        assert i18n.tr("common.ok") == "OK"
        i18n.set_language("en")
        assert i18n.current_language() == "en"
        # English never creates a user file.
        assert not (user_dir / "en.ini").exists()


class TestSetLanguageValidation:
    def test_empty_string_falls_back(self, sandbox):
        i18n.set_language("")
        assert i18n.current_language() == "en"

    def test_none_falls_back(self, sandbox):
        i18n.set_language(None)
        assert i18n.current_language() == "en"

    def test_garbage_with_special_chars_falls_back(self, sandbox):
        i18n.set_language("../../etc")
        assert i18n.current_language() == "en"

    def test_overlong_falls_back(self, sandbox):
        i18n.set_language("x" * 40)
        assert i18n.current_language() == "en"

    def test_case_normalized(self, sandbox):
        _write_catalog(sandbox[0], "pt-br", {"common.ok": "OK"})
        i18n.set_language("PT-BR")
        assert i18n.current_language() == "pt-br"
        assert i18n.tr("common.ok") == "OK"


class TestEnsureI18nFile:
    def test_creates_file_with_all_defaults(self, sandbox):
        user_dir, _ = sandbox
        i18n.ensure_i18n_file("de")
        saved = _read_catalog(user_dir / "de.ini")
        assert set(saved[i18n.STRING_SECTION]) == set(i18n.DEFAULT_STRINGS)

    def test_merges_new_keys_preserving_existing(self, sandbox):
        user_dir, _ = sandbox
        _write_catalog(user_dir, "de", {"common.ok": "In Ordnung"})
        i18n.ensure_i18n_file("de")
        saved = _read_catalog(user_dir / "de.ini")
        section = saved[i18n.STRING_SECTION]
        # Translator's value kept.
        assert section["common.ok"] == "In Ordnung"
        # Missing defaults appended as English placeholders.
        assert "common.cancel" in section

    def test_preserves_unknown_keys(self, sandbox):
        user_dir, _ = sandbox
        _write_catalog(user_dir, "de", {"legacy.old": "value"})
        i18n.ensure_i18n_file("de")
        saved = _read_catalog(user_dir / "de.ini")
        assert saved[i18n.STRING_SECTION]["legacy.old"] == "value"

    def test_english_needs_no_file(self, sandbox):
        user_dir, _ = sandbox
        i18n.ensure_i18n_file("en")
        assert not (user_dir / "en.ini").exists()

    def test_prunes_stale_placeholder_covered_by_bundle(self, sandbox):
        """A heal-artifact placeholder (byte-identical to the English
        default) must not shadow a bundled translation that appeared
        later - it is pruned and the bundle wins."""
        user_dir, bundled_dir = sandbox
        _write_catalog(bundled_dir, "de", {"settings.title": "Einstellungen"})
        _write_catalog(user_dir, "de", {"settings.title": "Settings"})
        i18n.set_language("de")  # heal runs, then the catalog reloads
        assert i18n.tr("settings.title") == "Einstellungen"
        saved = _read_catalog(user_dir / "de.ini")
        assert "settings.title" not in saved[i18n.STRING_SECTION]

    def test_keeps_user_override_covered_by_bundle(self, sandbox):
        """A real user edit (value != English default) is a deliberate
        override and survives even when the bundle translates the key."""
        user_dir, bundled_dir = sandbox
        _write_catalog(bundled_dir, "de", {"settings.title": "Einstellungen"})
        _write_catalog(user_dir, "de", {"settings.title": "Titel"})
        i18n.set_language("de")
        assert i18n.tr("settings.title") == "Titel"
        saved = _read_catalog(user_dir / "de.ini")
        assert saved[i18n.STRING_SECTION]["settings.title"] == "Titel"

    def test_keeps_placeholder_when_bundle_uncovered(self, sandbox):
        """Placeholders stay put while the bundle has no translation for
        them - they are the runtime fallback the translator fills in."""
        user_dir, bundled_dir = sandbox
        _write_catalog(bundled_dir, "de", {"settings.title": "Einstellungen"})
        _write_catalog(user_dir, "de", {"activity.match_pct": " · {pct}%% match"})
        i18n.set_language("de")
        # INI strips the value's leading space (documented drift) - the row
        # itself is kept because the bundle does not cover this key.
        assert i18n.tr("activity.match_pct") == "· {pct}% match"
        saved = _read_catalog(user_dir / "de.ini")
        assert saved[i18n.STRING_SECTION]["activity.match_pct"] == "· {pct}% match"

    def test_prunes_whitespace_padded_placeholder(self, sandbox):
        """Reader-normalized comparison: leading/trailing padding does not
        survive the INI round trip, so a padded placeholder is still
        recognized as a heal artifact."""
        user_dir, bundled_dir = sandbox
        _write_catalog(bundled_dir, "de", {"activity.newer": "neuer:"})
        _write_catalog(user_dir, "de", {"activity.newer": "newer:  "})
        i18n.set_language("de")
        assert i18n.tr("activity.newer") == "neuer:"
        saved = _read_catalog(user_dir / "de.ini")
        assert "activity.newer" not in saved[i18n.STRING_SECTION]


class TestTrn:
    def test_singular_and_plural(self, sandbox):
        user_dir, _ = sandbox
        _write_catalog(
            user_dir,
            "de",
            {"song.one": "{count} Lied", "song.other": "{count} Lieder"},
        )
        i18n.set_language("de")
        assert i18n.trn("song", 1) == "1 Lied"
        assert i18n.trn("song", 2) == "2 Lieder"

    def test_count_kwarg_wins_over_automatic(self, sandbox):
        user_dir, _ = sandbox
        _write_catalog(
            user_dir,
            "de",
            {"song.other": "{count} Lieder"},
        )
        i18n.set_language("de")
        assert i18n.trn("song", 5, count=7) == "7 Lieder"


class TestAvailableLanguages:
    def test_en_alone_when_no_files(self, sandbox):
        assert i18n.available_languages() == ["en"]

    def test_bundled_and_user_merged_sorted(self, sandbox):
        user_dir, bundled_dir = sandbox
        _write_catalog(bundled_dir, "de", {})
        _write_catalog(user_dir, "es", {})
        assert i18n.available_languages() == ["de", "en", "es"]

    def test_duplicates_deduped(self, sandbox):
        user_dir, bundled_dir = sandbox
        _write_catalog(bundled_dir, "de", {})
        _write_catalog(user_dir, "de", {})
        assert i18n.available_languages() == ["de", "en"]


class TestTrStatus:
    def test_known_code_translated(self, sandbox):
        assert i18n.tr_status("added") == "Added"
        assert i18n.tr_status("duplicate") == "Dup?"

    def test_unknown_message_passes_through(self, sandbox):
        assert i18n.tr_status("Nothing playing") == "Nothing playing"

    def test_every_code_has_a_catalog_row(self):
        for code in i18n.CARD_STATUS_KEYS:
            assert f"card_status.{code}" in i18n.DEFAULT_STRINGS

    def test_every_code_renders_to_display_text(self, sandbox):
        for code in i18n.CARD_STATUS_KEYS:
            rendered = i18n.tr_status(code)
            assert rendered != f"card_status.{code}"


class TestPlaylistSyncCodes:
    """Card-status codes are the wire format of PlaylistSyncService too."""

    def test_missing_playlist_id_yields_no_tracks(self):
        from services.playlist_sync import PlaylistSyncService

        calls = []
        sync = PlaylistSyncService({})
        sync.import_tracks(
            "name", "platform", "", lambda n, c, s: calls.append((n, c, s))
        )
        assert calls == [("name", 0, "no_tracks")]

    def test_missing_integration_yields_error(self):
        from services.playlist_sync import PlaylistSyncService

        calls = []
        sync = PlaylistSyncService({})
        sync.import_tracks(
            "name", "nowhere", "id", lambda n, c, s: calls.append((n, c, s))
        )
        assert calls == [("name", 0, "error")]


class TestCatalogConsistency:
    """Every tr()/trn() literal key used in app/ must exist in DEFAULT_STRINGS.

    Static scan: catches a call site whose key was never added to the
    catalog (the i18n equivalent of "every C[...] access must exist in
    THEME_MAP").  Keys built dynamically (e.g. tr(f"card_status.{code}"))
    don't match the plain-literal regex and are exempt by design.
    """

    def test_all_tr_keys_exist_in_defaults(self):
        import re
        from pathlib import Path

        from utils import i18n

        root = Path(__file__).resolve().parent.parent / "app"
        pattern = re.compile(r'\btrn?\(\s*"([^"]+)"')
        missing = []
        for path in sorted(root.rglob("*.py")):
            for key in pattern.findall(path.read_text(encoding="utf-8")):
                # trn(base) resolves base.one/base.other, so a base key is
                # satisfied by either its own row or its plural rows.
                if (
                    key not in i18n.DEFAULT_STRINGS
                    and f"{key}.one" not in i18n.DEFAULT_STRINGS
                ):
                    missing.append(f"{path.name}:{key}")
        assert not missing, (
            "tr() keys missing from DEFAULT_STRINGS: " + ", ".join(missing)
        )


class TestSettingsDefault:
    @pytest.fixture
    def paths(self, monkeypatch, tmp_path):
        settings = tmp_path / "settings.ini"
        monkeypatch.setattr(c, "SETTINGS_PATH", settings)
        return settings

    def test_language_default_written(self, paths):
        c.ensure_settings_file()
        cp = ConfigParser()
        cp.read(str(paths))
        assert cp.get("language", "lang", fallback=None) == "en"

@pytest.fixture
def real_bundled(monkeypatch, tmp_path):
    """Sandboxed i18n with the repo-bundled ``app/i18n`` catalogs."""
    user_dir = tmp_path / "cfg" / "i18n"
    bundled_dir = Path(__file__).resolve().parent.parent / "app" / "i18n"
    monkeypatch.setattr(i18n, "I18N_DIR", user_dir)
    monkeypatch.setattr(i18n, "_BUNDLED_DIR", bundled_dir)
    i18n.set_language("en")
    yield user_dir, bundled_dir
    i18n.set_language("en")


class TestBundledCatalog:
    """The repo-shipped example catalog must load and translate real rows."""

    def test_about_section_translated(self, real_bundled):
        i18n.set_language("de")
        assert i18n.tr("settings.about") == "Über"
        assert i18n.tr("settings.author") == "Autor: d1pl"
        assert i18n.tr("settings.repo", url="https://x") == "Repository: https://x"
        assert i18n.tr("settings.monero") == "monero: bald"

    def test_common_and_plurals(self, real_bundled):
        i18n.set_language("de")
        assert i18n.tr("common.cancel") == "Abbrechen"
        assert i18n.trn("showcase.stats.songs", 1) == "1 Song"
        assert i18n.trn("showcase.stats.songs", 5) == "5 Songs"
        assert i18n.trn("cli.removed_registry", 1) == "1 Playlist aus der Registrierung entfernt"
        assert i18n.trn("cli.removed_registry", 3) == "3 Playlists aus der Registrierung entfernt"

    def test_untranslated_key_falls_back_to_english(self, real_bundled):
        i18n.set_language("de")
        assert i18n.tr("settings.duration_value") == i18n.DEFAULT_STRINGS["settings.duration_value"]

    def test_bundled_keys_exist_in_defaults(self, real_bundled):
        cfg = ConfigParser()
        cfg.read(str(real_bundled[1] / "de.ini"))
        rows = dict(cfg.items(i18n.STRING_SECTION))
        for key in rows:
            assert key in i18n.DEFAULT_STRINGS or f"{key}.one" in i18n.DEFAULT_STRINGS, key
        # the file itself must be BasicInterpolation-safe (no bare '%')
        assert all("%%" not in v and "%" not in v for v in rows.values())

    def test_de_in_available_languages(self, real_bundled):
        assert "de" in i18n.available_languages()


class TestPhase4Rows:
    """trn() count rows and user_log message rows added in Phase 4."""

    def test_cli_registry_removal_rows(self):
        i18n.set_language("en")
        assert i18n.trn("cli.removed_registry", 1) == "removed 1 playlist from the registry"
        assert i18n.trn("cli.removed_registry", 2) == "removed 2 playlists from the registry"

    def test_cli_imported_tracks_plural(self):
        i18n.set_language("en")
        assert i18n.trn("cli.imported_tracks", 1) == "imported 1 track"
        assert i18n.trn("cli.imported_tracks", 7) == "imported 7 tracks"

    def test_manage_footer_items_join(self):
        parts = [
            i18n.trn("manage.footer_credentials", 1),
            i18n.trn("manage.footer_playlists", 2),
            i18n.trn("manage.footer_errors", 0),
        ]
        assert ", ".join(parts) == "1 credential file, 2 playlists, 0 errors"

    def test_user_log_message_rows(self):
        i18n.set_language("en")
        assert (
            i18n.tr("service.integration_unavailable", name="X", error="e")
            == "X integration unavailable (e)"
        )
        assert (
            i18n.tr("app.update_available", version="1.2.3", url="http://u")
            == "Update v1.2.3 available at http://u"
        )
        assert i18n.tr("main.uninstalling_cards_failed", platform="sp", reason="r") == (
            "Uninstalling sp: r - their keybinds/databases may remain"
        )

    def test_percent_value_survives_catalog_roundtrip(self, sandbox):
        key = "activity.match_pct"
        value = i18n.DEFAULT_STRINGS[key]
        assert "%" in value  # the row really carries a literal %
        i18n.ensure_i18n_file("de")
        cfg = ConfigParser()
        cfg.read(str(sandbox[0] / "de.ini"))
        stored = cfg.get(i18n.STRING_SECTION, key, raw=True)
        assert "%%" in stored
        # BasicInterpolation unwraps "%%" back to "%"; INI strips the value's
        # surrounding whitespace (configparser contract, cosmetic only).
        assert cfg.get(i18n.STRING_SECTION, key) == value.strip()


class TestLanguagePicker:
    """Phase 5: language picker display names and the code reverse-map."""

    def test_english_default_names(self):
        i18n.set_language("en")
        assert i18n.language_display("en") == "English"
        assert i18n.language_display("de") == "Deutsch"

    def test_names_adapt_to_active_catalog(self, real_bundled):
        i18n.set_language("de")
        # "English" is the row each catalog translates; a language's own
        # name ("Deutsch") stays the same in any catalog.
        assert i18n.language_display("en") == "Englisch"
        assert i18n.language_display("de") == "Deutsch"

    def test_unknown_code_falls_back_to_code(self):
        i18n.set_language("en")
        assert i18n.language_display("fr") == "fr"

    def test_reverse_map_roundtrip(self, real_bundled):
        i18n.set_language("de")
        for code in ("en", "de"):
            assert i18n.language_code_for(i18n.language_display(code)) == code

    def test_reverse_map_unknown_display_keeps_active(self, real_bundled):
        i18n.set_language("de")
        assert i18n.language_code_for("Klingon") == "de"

    def test_picker_combo_values_are_deduplicated_displays(self, real_bundled):
        i18n.set_language("de")
        codes = i18n.available_languages()
        displays = [i18n.language_display(c) for c in codes]
        assert len(displays) == len(set(displays))
        assert i18n.language_display("en") in displays
