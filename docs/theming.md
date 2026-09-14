# Theming

Runtime colors are centralized in `app/utils/theme.py`, not re-read from the INI per widget:

- `THEME_MAP` maps short palette keys → `(ini section, option)` pairs; `C` is the flat `key -> color` dict; `load_theme()` re-reads `cfg/theme.ini` into `C`.
- UI modules do `from utils.theme import C` and read `C[...]` at **widget-creation time**. Never freeze a color into a module-level constant at import — runtime theme changes would not propagate.
- Call `load_theme()` again before re-applying colors to existing widgets (the theme picker's `on_theme_change` does this in `main_window.apply_theme`).
- **Adding a color touches four places**: `THEME_MAP` (`utils/theme.py`), `DEFAULT_THEME` (`utils/config.py`), `theme.txt`, and — unless `ensure_theme_file()` has run since — `cfg/theme.ini` (`ensure_theme_file` merges new `DEFAULT_THEME` keys in on every call; stale keys are never removed — the old `_strip_legacy_active_keys` migration in `utils/config.py`, which deleted the dead `activebackground`/`activeforeground` options from user INIs, was already removed).
- **Every `C["..."]` access must exist in `THEME_MAP`** — a missing key raises `KeyError` at widget creation, not at import. Verify with a quick grep/scan after adding keys.
- **Hover colors are derived, not themed**: since 0.2.x the palette has no `*_a_bg`/`*_a_fg` keys. Style every `tk.Button`/`tk.Checkbutton` with `**btn_colors(bg, fg)` (`utils/theme.py`) — it derives `activebackground` via `hover_bg()` (a shade of the resting bg: light colors darkened, dark colors lightened) and reuses the resting fg as `activeforeground`. Never pass `activebackground`/`activeforeground` explicitly; a leftover `C["..._a_bg"]` read raises `KeyError` at widget creation.
- `cfg/theme.ini` is the source of truth when files disagree — correct the other files, not the ini.