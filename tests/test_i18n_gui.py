"""Widget-build smoke for the translated UI (skip when no display).

This is the only GUI-touching test file in the suite: it constructs the real
Settings dialog so every widget-creation-time ``tr()`` call in the appearance
section actually executes.  It auto-skips without a DISPLAY, so the normal
headless suite is unaffected; run it under a virtual display with:

    xvfb-run -a python -m pytest tests/test_i18n_gui.py

conftest.py has already redirected cfg/db paths to a temp tree.
"""
import os
import tkinter as tk

import pytest

from ui.settings_ui import show_settings_dialog
from utils.config import get_setting_value
from utils.i18n import set_language, tr

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"), reason="no DISPLAY (run under xvfb-run)"
)


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _build_dialog(lang: str):
    # Same wiring as App.__init__/app/main.py: bind the setting before widgets.
    set_language(lang)
    root = tk.Tk()
    root.withdraw()
    show_settings_dialog(root, on_restart_app=lambda: None)
    root.update_idletasks()
    dialog = next(w for w in _walk(root) if isinstance(w, tk.Toplevel))
    return root, dialog


@pytest.mark.parametrize(
    "lang,label,displays",
    [
        ("en", "Language:", {"English", "Deutsch"}),
        ("de", "Sprache:", {"Englisch", "Deutsch"}),
    ],
)
def test_language_row_builds(lang, label, displays):
    """The language row must render (label + native-name combo) per catalog."""
    root, dialog = _build_dialog(lang)
    try:
        labels = [w.cget("text") for w in _walk(dialog) if isinstance(w, tk.Label)]
        assert label in labels

        combos = [w for w in _walk(dialog) if w.__class__.__name__ == "Combobox"]
        language_values = next(
            (list(c.cget("values")) for c in combos
             if {"English", "Englisch"} & set(c.cget("values"))),
            None,
        )
        assert language_values is not None
        assert displays <= set(language_values)
    finally:
        set_language("en")
        root.destroy()


def test_all_catalog_rows_are_formattable():
    """Every DEFAULT_STRINGS row must survive a tr() with all its placeholders."""
    import re

    from utils import i18n

    for key, value in i18n.DEFAULT_STRINGS.items():
        placeholders = set(re.findall(r"\{(\w+)\}", value))
        if placeholders:
            rendered = tr(key, **{name: "x" for name in placeholders})
            assert "{" not in rendered, f"{key} left a placeholder: {rendered}"
