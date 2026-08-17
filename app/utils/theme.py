"""
Central theme palette.

Loads :file:`cfg/theme.ini` once into a flat dict (``C``) keyed by the short
names listed in :file:`theme.txt`, so UI modules can do::

    from utils.theme import C
    win.configure(background=C["frame_main_bg"])

instead of re-reading the INI file (and re-checking/possibly rewriting it) on
every single widget creation.

The theme can change at runtime (Settings -> Theme), so two rules apply:

  * Read from ``C`` at *widget-creation time* - never freeze a colour into a
    module-level constant at import time.
  * Call :func:`load_theme` again before re-applying colours to already
    created widgets (it always re-reads the file; it is not a no-op).
"""

from configparser import ConfigParser

from utils.config import (
    DEFAULT_THEME,
    THEME_PATH,
    _safe_read_config,
    ensure_theme_file,
)

#: palette name -> (ini section, ini option).  Names must match ``theme.txt``
#: and every entry must resolve to an option that exists in ``cfg/theme.ini``.
THEME_MAP = {
    "root_bg": ("root_background", "background"),
    "frame_head_bg": ("frame_header", "background"),
    "frame_main_bg": ("frame_main", "background"),
    "frame_playlist_bg": ("frame_playlist", "background"),
    "label_def_bg": ("label_default", "background"),
    "label_def_fg": ("label_default", "foreground"),
    "label_playlist_bg": ("label_playlist", "background"),
    "label_playlist_fg": ("label_playlist", "foreground"),
    "label_playlist_name_bg": ("label_playlist_name", "background"),
    "label_playlist_name_fg": ("label_playlist_name", "foreground"),
    "label_playlist_log_bg": ("label_playlist_log", "background"),
    "label_playlist_log_fg": ("label_playlist_log", "foreground"),
    "label_playlist_good_bg": ("label_playlist_good", "background"),
    "label_playlist_good_fg": ("label_playlist_good", "foreground"),
    "label_playlist_warn_bg": ("label_playlist_warning", "background"),
    "label_playlist_warn_fg": ("label_playlist_warning", "foreground"),
    "label_playlist_error_bg": ("label_playlist_error", "background"),
    "label_playlist_error_fg": ("label_playlist_error", "foreground"),
    "checkbutton_bg": ("checkbutton", "background"),
    "checkbutton_fg": ("checkbutton", "foreground"),
    "checkbutton_selector": ("checkbutton", "selectcolor"),
    "button_head_bg": ("button_header", "background"),
    "button_head_fg": ("button_header", "foreground"),
    "button_main_bg": ("button_main", "background"),
    "button_main_fg": ("button_main", "foreground"),
    "button_playlist_bg": ("button_playlist", "background"),
    "button_playlist_fg": ("button_playlist", "foreground"),
    "button_close_bg": ("button_close", "background"),
    "button_close_fg": ("button_close", "foreground"),
    "button_save_bg": ("button_save", "background"),
    "button_save_fg": ("button_save", "foreground"),
    "entry_default_bg": ("entry_default", "background"),
    "entry_default_fg": ("entry_default", "foreground"),
    "entry_default_ro_bg": ("entry_default", "readonlybackground"),
    "entry_playlist_bg": ("entry_playlist", "background"),
    "entry_playlist_fg": ("entry_playlist", "foreground"),
    "entry_playlist_ro_bg": ("entry_playlist", "readonlybackground"),
    "label_playlist_stats_bg": ("label_playlist_stats", "background"),
    "label_playlist_stats_fg": ("label_playlist_stats", "foreground"),
}

#: Flat palette: palette name -> colour string.  Populated by :func:`load_theme`.
C: dict[str, str] = {}


def luminance(hex_color: str) -> float:
    """Relative luminance (0..1) of a ``#RRGGBB`` colour; 0.0 for named colours.

    Named colours (Tk accepts e.g. ``"red"``) are assumed dark, so callers
    pick white text on them.
    """
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 0.0
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def readable_fg(background: str) -> str:
    """Black or white foreground readable on *background*."""
    return "#000000" if luminance(background) >= 0.6 else "#ffffff"


def hover_bg(color: str) -> str:
    """Hover background for *color*: a shade of the colour itself.

    Light colours are darkened toward black, dark colours lightened toward
    white, so the hover state is always visibly different from the resting
    one.  A pure ``#ffffff``/``#000000`` (or near-pure) value would make an
    inverted hover colour identical (or nearly identical) to the swatch,
    giving no feedback.  Named colours fall back to no hover change - theme
    values from the picker and presets are always hex.
    """
    h = color.lstrip("#")
    if len(h) != 6:
        return color
    channels = [int(h[i : i + 2], 16) for i in (0, 2, 4)]
    if luminance(color) >= 0.6:
        shaded = [int(c * 0.75) for c in channels]  # light -> darker
    else:
        shaded = [int(c + (255 - c) * 0.25) for c in channels]  # dark -> lighter
    return "#{:02x}{:02x}{:02x}".format(*shaded)


def btn_colors(background: str, foreground: str) -> dict[str, str]:
    """Theme kwargs for a ``tk.Button``/``tk.Checkbutton``.

    The hover state (``activebackground``/``activeforeground``) is derived
    from the resting colours (:func:`hover_bg`, foreground unchanged), so
    the palette needs no separate ``*_a_bg``/``*_a_fg`` keys.  Use as::

        tk.Button(frame, text="Go", **btn_colors(C["button_main_bg"], C["button_main_fg"]))
    """
    return {
        "background": background,
        "activebackground": hover_bg(background),
        "foreground": foreground,
        "activeforeground": foreground,
    }


def load_theme() -> None:
    """(Re)load every theme colour from ``cfg/theme.ini`` into :data:`C`.

    Always re-reads the file, so calling it after a theme edit picks up the
    new colours.  Missing keys fall back to ``DEFAULT_THEME`` in
    :mod:`utils.config` (the single source of defaults).
    """
    ensure_theme_file()
    cfg = ConfigParser()
    cfg = _safe_read_config(cfg, THEME_PATH)
    for name, (section, option) in THEME_MAP.items():
        default = DEFAULT_THEME.get(section, {}).get(option, "#000000")
        C[name] = cfg.get(section, option, fallback=default)


# Load once so ``from utils.theme import C`` always yields a populated palette,
# even before App.__init__ runs.  Runtime theme changes re-read via load_theme().
load_theme()
