#!/usr/bin/env python3
"""i18n catalog maintenance: export the English template and diff languages.

This is a *maintainer* tool, not an app module -- run it from the repo root
with the interpreter directly::

    python tools/i18n_report.py export              # (re)generate app/i18n/example.ini
    python tools/i18n_report.py diff de             # gaps against the resolved catalog
    python tools/i18n_report.py diff de fr --list   # full gap key list, several languages
    python tools/i18n_report.py diff de --write-missing=missing_de.ini
    python tools/i18n_report.py diff de --strict    # exit 1 while gaps remain (CI)

``export`` rewrites the bundled English template from the code-side
``DEFAULT_STRINGS`` (only when the content changed).  ``diff`` compares that
template against a language's *runtime-resolved* catalog - the bundled
``app/i18n/<lang>.ini`` overlaid by the user catalog
``<profile cfg_dir>/i18n/<lang>.ini`` - so it reports exactly what a speaker
of that language sees on screen.

Gaps are split into three buckets:

* ``missing``     no row for the key in the resolved catalog at all
* ``placeholder`` a row exists but its value is identical to the English
                  default (self-healed filler, or a deliberate identity
                  translation listed below)
* ``identity``    deliberately kept identical to English (proper nouns,
                  loanwords, universal labels) - counted as translated

The identity set is report metadata (the runtime never needs it), so it
lives here rather than in ``utils/i18n.py``.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from configparser import ConfigParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from utils import i18n  # noqa: E402  (bare-import rule, see AGENTS.md)

SECTION = "strings"
TEMPLATE_NAME = "example.ini"
IDENTITY_ROWS = {
    # Proper nouns / loanwords / universal labels whose German text is
    # deliberately identical to the English default.
    "app.title",        # "PlaylistManager" - product name
    "common.ok",        # "OK"
    "card_status.ok",   # "OK"
    "card_status.sync",  # "Sync"
    "settings.lang_de",  # "Deutsch" - a language's own name
    "settings.support",  # "Support:" - loanword
    "settings.version",  # "Version: {version}" - loanword
}


def _template_path() -> Path:
    return Path(i18n._BUNDLED_DIR) / TEMPLATE_NAME


def _read_rows(path: Path) -> dict[str, str]:
    """Read a catalog file, unwrapping ``%%`` escapes (BasicInterpolation)."""
    cfg = ConfigParser()
    if path.exists() and path.is_file():
        cfg = i18n._safe_read_config(cfg, path)
    return dict(cfg.items(SECTION)) if cfg.has_section(SECTION) else {}


def _escape(value: str) -> str:
    """Escape a value for ConfigParser storage (a bare ``%`` must be
    doubled so ``BasicInterpolation`` doesn't trip at set() time)."""
    return value.replace("%", "%%")


def _sorted_rows() -> list[tuple[str, str]]:
    return sorted(i18n.DEFAULT_STRINGS.items())


def export() -> Path | None:
    """Regenerate ``app/i18n/example.ini`` from DEFAULT_STRINGS (atomic,
    only-when-changed, so an unchanged tree leaves no diff).

    Returns the template path when it was (re)written, ``None`` when the
    existing file already matches."""
    cfg = ConfigParser()
    cfg.add_section(SECTION)
    for key, value in _sorted_rows():
        cfg.set(SECTION, key, _escape(value))

    import io
    buf = io.StringIO()
    cfg.write(buf)
    header = ("; English translation template - copy to <lang>.ini "
              "and translate the values.\n;\n"
              "; Regenerate this file from the code-side defaults at any time with:\n"
              ";     python tools/i18n_report.py export\n"
              "; (tests/test_i18n_tool.py pins it to DEFAULT_STRINGS in "
              "app/utils/i18n.py).\n;\n"
              "; Values with a literal % are stored %% escaped "
              "(ConfigParser BasicInterpolation rule).\n")
    body = header + buf.getvalue()

    path = _template_path()
    if path.exists() and path.read_text(encoding="utf-8") == body:
        return None
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp) if os.path.exists(tmp) else None
        raise
    return path


def diff(lang: str) -> tuple[int, int, list[str], list[str], list[str]]:
    """Template vs resolved-catalog comparison for one language.

    Returns ``(total, translated, missing, placeholders, identity_keys)``.
    """
    ref = _read_rows(_template_path())
    if not ref:
        sys.exit(f"error: {_template_path()} missing or empty - run 'export' first")
    target = _read_rows(Path(i18n._BUNDLED_DIR) / f"{lang}.ini")
    target.update(_read_rows(Path(i18n.I18N_DIR) / f"{lang}.ini"))  # user wins

    missing: list[str] = []
    placeholders: list[str] = []
    identity: list[str] = []
    translated = 0
    for key in sorted(ref):
        if key not in target:
            missing.append(key)
            continue
        if key in IDENTITY_ROWS:
            identity.append(key)
            translated += 1
        elif target[key] == ref[key]:
            placeholders.append(key)
        else:
            translated += 1
    return len(ref), translated, missing, placeholders, identity


def _print_report(lang: str, total: int, translated: int, missing: list[str],
                  placeholders: list[str], identity: list[str], *,
                  show_list: bool, write_missing: str | None) -> int:
    still = len(missing) + len(placeholders)
    print(f"{lang}: {total} rows, {translated} translated "
          f"({len(identity)} identity), {still} still English "
          f"(missing {len(missing)}, placeholder {len(placeholders)})")
    if show_list:
        for key in sorted(missing + placeholders):
            print(f"  {key}")
    if write_missing:
        import io
        cfg = ConfigParser()
        cfg.add_section(SECTION)
        for key in sorted(missing + placeholders):
            cfg.set(SECTION, key, _escape(_template_rows().get(key, "")))
        buf = io.StringIO()
        cfg.write(buf)
        header = (f"; Missing/placeholder rows for '{lang}'. Paste under a "
                  f"[{SECTION}] section into\n"
                  f"; cfg/i18n/{lang}.ini (or app/i18n/{lang}.ini) and "
                  f"translate the values.\n")
        Path(write_missing).write_text(header + buf.getvalue(), encoding="utf-8")
        print(f"  skeleton written to {write_missing}")
    return 1 if still else 0


def _template_rows() -> dict[str, str]:
    return _read_rows(_template_path())


def normalize_value(value: str) -> str:
    """The equality view a reader actually has of a stored value.

    ConfigParser strips each continuation line's leading whitespace and the
    value's surrounding whitespace, so English defaults carrying per-line
    indent (``cli.ym_no_terminal``) or padding (``' · {pct}% match'``) can
    never round-trip byte-exact.  Comparing through this normalizer keeps
    the sync/pinned tests honest instead of fighting the format."""
    return "\n".join(line.lstrip() for line in value.splitlines()).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def run_export(args: argparse.Namespace) -> int:
        if export():
            print(f"wrote {_template_path()}")
        else:
            print(f"{_template_path()} up to date")
        return 0

    ex = sub.add_parser("export", help="regenerate app/i18n/example.ini")
    ex.set_defaults(func=run_export)

    df = sub.add_parser("diff", help="report still-English rows for language(s)")
    df.add_argument("langs", nargs="+", metavar="LANG",
                    help="language code(s), e.g. de ; 'all' checks every bundled+user catalog")
    df.add_argument("--list", action="store_true", help="print every gap key")
    df.add_argument("--write-missing", metavar="FILE",
                    help="write a paste-ready [strings] skeleton of gap keys")
    df.add_argument("--strict", action="store_true",
                    help="exit 1 while any language has gaps (CI-friendly)")

    def run_diff(args: argparse.Namespace) -> int:
        langs = args.langs
        if "all" in langs:
            langs = [l for l in i18n.available_languages() if l != i18n.DEFAULT_LANGUAGE]
        codes: dict[str, int] = {}
        for lang in sorted(set(langs)):
            codes[lang] = _print_report(
                lang, *diff(lang),
                show_list=args.list, write_missing=args.write_missing,
            )
        return 1 if args.strict and any(codes.values()) else 0

    df.set_defaults(func=run_diff)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())