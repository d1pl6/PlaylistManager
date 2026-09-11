import logging
import os
import re
import tempfile
from configparser import ConfigParser, Error as ConfigParseError
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Profile-aware paths: imported early to initialise the active profile
# before any module-level path constant is bound.  profile_store has no
# app imports (stdlib only), so this import is cheap and cycle-free.
# ---------------------------------------------------------------------------
from services import profile_store as _profile_store

_profile_store.initialize()

SETTINGS_PATH = _profile_store.cfg_dir() / "settings.ini"

DEFAULT_SETTINGS = {
    "update_check": {"is_true": "yes"},
    "center_windows": {"is_true": "yes"},
    "auto_resize": {"is_true": "no"},
    # Restore the main window's last size/position on launch (saved live
    # on <Configure> and on quit; settings value written by
    # utils/window.py save_window_geometry, read by restore_window_geometry).
    "remember_geometry": {"is_true": "yes"},
    # Start the main window fullscreen ("" --fullscreen flag overrides per
    # run; live-toggleable from Settings / F11 - never persisted via the
    # geometry save, which skips while fullscreen is active).
    "fullscreen": {"is_true": "no"},
    "window": {"geometry": ""},  # last main-window "WxH+X+Y"
    "global_listener": {"is_true": "yes"},
    "hide_to_tray": {"is_true": "no"},
    "start_in_tray": {"is_true": "no"},  # launch hidden in the system tray (tray must actually start)
    "ui_scale": {"value": "auto"},
    "font": {"family": ""},
    "showcase": {"count": "0"},          # int, 0 = off, N = show last N added songs
    "showcase_log": {"is_true": "yes"},  # show the log_artist/log_name/log_log row
    "playlist_stats": {"is_true": "yes"},  # show the song count / followers / duration row
    "layout": {"columns": "2"},          # int, playlist card grid columns, clamped 1-4
    # Extra duplicate check: near-duplicate detection on the add path
    # (same artist + similar title + close duration).  Knobs are read by
    # services/duplicate_check.py callers.
    "duplicate_check": {"is_true": "no", "title_threshold": "0.85", "duration_tolerance": "5"},
    # Remove/close confirmations:
    #   confirm_on_song_remove - ask before the showcase ✕ deletes a song
    #     (full removal is the only action; there is no "keep db" for songs)
    #   confirm_on_playlist_remove - ask before a playlist card is closed
    #     (the dialog offers Remove / Keep DB / Cancel)
    #   [remove_playlist] default - the behaviour when the playlist dialog
    #     is off: "remove" deletes the card + its database, "keep_db" keeps
    #     the local SQLite file (see ui.card_grid.resolve_playlist_removal_mode).
    "confirm_on_song_remove": {"is_true": "yes"},
    "confirm_on_playlist_remove": {"is_true": "yes"},
    "remove_playlist": {"default": "remove"},
    # Last.fm integration settings (service plugin, optional)
    "like_button": {"is_true": "no"},  # show the ♥/♡ button under "remove from playlist"
    # Scrobble a song when an add-flow succeeds.  Defaults to no like the
    # other Last.fm side effects: scrobbling is a privacy/audit-relevant
    # action and must be opted into, not silently switched on the moment
    # the plugin is configured.
    "scrobble_on_add": {"is_true": "no"},
    "scrobble_keybind": {"keybind": ""},  # standalone "scrobble current song, without adding it" combo
    # SoundCloud capture mode: how the add-flow acquires the current song.
    # "api" reads /me/recently-played/tracks[0]; "hybrid" prefers the browser
    # extension (exact URL + play/pause state) and falls back to the api path
    # on a receiver miss; "extension" is receiver-only.  Read by
    # integrations/soundcloud/flow.py.
    "soundcloud": {"capture_mode": "hybrid"},
    # Thumbnail data-saver modes (read by utils/thumbnail.py):
    #   "off"      - live fetches every run, no on-disk cache (default)
    #   "download" - persist every fetched playlist/song thumb to disk
    #   "dedupe"   - one song thumb per distinct song (duplicate-check
    #                matcher), reused across playlists/URLs/profiles
    #   "cache"    - persist covers + only currently visible song thumbs,
    #                pruned after each showcase refresh
    #   "max"      - no thumbnails at all (--data-saver forces this per run)
    "thumbnails": {"mode": "off"},
    # Grid sort / pin controls (see services/playlist_sort.py and
    # app/ui/main_window.py).  The header combobox writes these live;
    # "pinned" flags live per-playlist in the registry, not here.
    "grid_sort": {"key": "name", "direction": "asc"},
    "show_pin_buttons": {"is_true": "yes"},  # per-card pin/unpin buttons
}

# Valid [thumbnails] mode values; anything else falls back to "off".
THUMBNAIL_MODES = ("off", "download", "dedupe", "cache", "max")

# Valid [grid_sort] values (see services/playlist_sort.py).  The header
# combobox/arrow in main_window writes these live; anything else falls
# back to name/asc.
GRID_SORT_KEYS = ("name", "platform", "added", "used")
GRID_SORT_KEY_LABELS = {
    "name": "Name",
    "platform": "Platform",
    "added": "Added",
    "used": "Last used",
}
GRID_SORT_DIRECTIONS = ("asc", "desc")
GRID_SORT_DEFAULT_KEY = "name"
GRID_SORT_DEFAULT_DIRECTION = "asc"

# Playlist-close mode choices (see DEFAULT_SETTINGS [remove_playlist]).
# The labels are shown in the Settings combobox.
REMOVE_PLAYLIST_MODES = ("remove", "keep_db")
REMOVE_PLAYLIST_MODE_LABELS = {
    "remove": "Remove",
    "keep_db": "Keep database",
}
REMOVE_PLAYLIST_DEFAULT_MODE = "remove"

THEME_PATH = _profile_store.cfg_dir() / "theme.ini"

DEFAULT_THEME = {
    "root_background": {"background": "#1A1A1A"},
    "frame_header": {"background": "#101010"},
    "frame_main": {"background": "#252525"},
    "frame_playlist": {"background": "#252525"},
    "label_default": {"background": "#252525", "foreground": "#EDEDED"},
    "label_playlist": {"background": "#2f2f2f", "foreground": "#EDEDED"},
    "label_playlist_name": {"background": "#2f2f2f", "foreground": "#DCDCDC"},
    "label_playlist_log": {"background": "#2f2f2f", "foreground": "#EDEDED"},
    "label_playlist_good": {"background": "#00c600", "foreground": "#EDEDED"},
    "label_playlist_warning": {"background": "#c68100", "foreground": "#EDEDED"},
    "label_playlist_error": {"background": "#c60000", "foreground": "#EDEDED"},
    "checkbutton": {
        "background": "#303030",
        "foreground": "#DADADA",
        "selectcolor": "#505050",
    },
    "button_header": {
        "background": "#6C6C6C",
        "foreground": "#000000",
    },
    "button_main": {
        "background": "#3A3A3A",
        "foreground": "#D7D7D7",
    },
    "button_playlist": {
        "background": "#3A3A3A",
        "foreground": "#D7D7D7",
    },
    "button_close": {
        "background": "#160000",
        "foreground": "#FFFFFF",
    },
    "button_save": {
        "background": "#004304",
        "foreground": "#D7D7D7",
    },
    "entry_default": {
        "background": "#404040",
        "foreground": "#FFFFFF",
        "readonlybackground": "#2A2A2A",
    },
    "entry_playlist": {
        "background": "#404040",
        "foreground": "#FFFFFF",
        "readonlybackground": "#2A2A2A",
    },
    "label_playlist_stats": {
        "background": "#2a2a2a",
        "foreground": "#B0B0B0",
    },
    "scrollable_frame": {
        "background": "#1A1A1A",
    },
    "search_bar": {
        "background": "#1E1E1E",
        "foreground": "#E0E0E0",
    },
    "search_result": {
        "background": "#2A2A2A",
        "foreground": "#C0C0C0",
    },
}


def _write_ini_file(path: Path, cfg: ConfigParser) -> None:
    """Atomically persist *cfg* to *path*.

    Writes to a temp file in the same directory, fsyncs, then
    ``os.replace()``s it over the target.  An in-place ``open(path, "w")``
    truncates the file first, so a crash or drive error mid-write leaves a
    truncated INI behind - and a truncated ``theme.ini`` raises at import
    time (``load_theme()`` runs at module import), taking the whole app down.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            cfg.write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _safe_read_config(cfg: ConfigParser, path: Path) -> ConfigParser:
    """Read *path* into *cfg*, returning a fresh empty parser on parse failure.

    ``ConfigParser.read()`` raises on malformed INIs (unterminated section
    headers, missing section headers) and - worse - keeps the sections it
    parsed *before* the error, so the caller must never reuse the partial
    parser.  A corrupt theme.ini/settings.ini (hand edit, external tool,
    drive glitch on the removable disk) must not take the app down - the
    settings are cosmetic and every ``ensure_*`` caller self-heals by
    merging defaults over the returned parser and rewriting the file.
    """
    try:
        cfg.read(str(path))
    except ConfigParseError as e:
        logger.warning(
            "Failed to parse %s (%s) - falling back to defaults", path, e
        )
        return ConfigParser()
    return cfg


def ensure_theme_file() -> None:
    cfg = ConfigParser()
    if THEME_PATH.exists():
        cfg = _safe_read_config(cfg, THEME_PATH)
    changed = False
    for section, values in DEFAULT_THEME.items():
        if section not in cfg:
            cfg[section] = values
            changed = True
        else:
            for key, value in values.items():
                if key not in cfg[section]:
                    cfg[section][key] = value
                    changed = True
    if not THEME_PATH.exists() or changed:
        _write_ini_file(THEME_PATH, cfg)


def set_theme_value(section: str, option: str, value: str) -> None:
    ensure_theme_file()  # heals a corrupt/missing file and merges defaults
    cfg = ConfigParser()
    cfg = _safe_read_config(cfg, THEME_PATH)
    if section not in cfg:
        cfg[section] = {}
    cfg[section][option] = value
    _write_ini_file(THEME_PATH, cfg)


THEME_PRESETS = {
    "white": {
        "root_background": {"background": "#F5F5F5"},
        "frame_header": {"background": "#EDEDED"},
        "frame_main": {"background": "#FFFFFF"},
        "frame_playlist": {"background": "#FFFFFF"},
        "label_default": {"background": "#FFFFFF", "foreground": "#1A1A1A"},
        "label_playlist": {"background": "#F2F2F2", "foreground": "#1A1A1A"},
        "label_playlist_name": {"background": "#F2F2F2", "foreground": "#333333"},
        "label_playlist_log": {"background": "#F2F2F2", "foreground": "#1A1A1A"},
        "label_playlist_good": {"background": "#252525", "foreground": "#FFFFFF"},
        "label_playlist_warning": {"background": "#C68100", "foreground": "#FFFFFF"},
        "label_playlist_error": {"background": "#C60000", "foreground": "#FFFFFF"},
        "checkbutton": {
            "background": "#F0F0F0",
            "foreground": "#222222",
            "selectcolor": "#D9D9D9",
        },
        "button_header": {
            "background": "#D0D0D0",
            "foreground": "#000000",
        },
        "button_main": {
            "background": "#E6E6E6",
            "foreground": "#222222",
        },
        "button_playlist": {
            "background": "#E6E6E6",
            "foreground": "#222222",
        },
        "button_close": {
            "background": "#F4DADA",
            "foreground": "#000000",
        },
        "button_save": {
            "background": "#004304",
            "foreground": "#D7D7D7",
        },
        "entry_default": {
            "background": "#FFFFFF",
            "foreground": "#111111",
            "readonlybackground": "#F3F3F3",
        },
        "entry_playlist": {
            "background": "#FFFFFF",
            "foreground": "#111111",
            "readonlybackground": "#F3F3F3",
        },
        "label_playlist_stats": {
            "background": "#EBEBEB",
            "foreground": "#555555",
        },
        "scrollable_frame": {
            "background": "#F5F5F5",
        },
        "search_bar": {
            "background": "#F0F0F0",
            "foreground": "#111111",
        },
        "search_result": {
            "background": "#F5F5F5",
            "foreground": "#333333",
        },
    },
}


def restore_theme_defaults() -> None:
    cfg = ConfigParser()
    for section, values in DEFAULT_THEME.items():
        cfg[section] = values
    _write_ini_file(THEME_PATH, cfg)


def apply_theme_preset(preset: str) -> None:
    preset = preset.lower()

    ensure_theme_file()
    values = THEME_PRESETS.get(preset)
    if not values:
        return
    cfg = ConfigParser()
    cfg = _safe_read_config(cfg, THEME_PATH)
    for section, options in values.items():
        cfg[section] = options
    _write_ini_file(THEME_PATH, cfg)


# ---------------------------------------------------------------------------
# Named user themes: full palette INI files under cfg/themes/<name>.ini
# (profile-aware via cfg_dir, same as theme.ini).  "Default theme" and
# "White Theme" are built-in pseudo-entries handled by the UI - they map
# onto restore_theme_defaults() / apply_theme_preset("white") and are
# reserved here so a saved theme can never shadow them.
# ---------------------------------------------------------------------------

THEMES_DIR = _profile_store.cfg_dir() / "themes"

_THEME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_RESERVED_THEME_NAMES = ("default theme", "white theme")


def _validate_theme_name(name: str) -> str:
    """Validate a theme name, returning the stripped form.

    Mirrors profile_store's rules: alphanumeric + underscore + hyphen,
    max 64 chars.  Raises ValueError on any violation.
    """
    name = name.strip()
    if not name:
        raise ValueError("Theme name cannot be empty")
    if len(name) > 64:
        raise ValueError("Theme name too long (max 64 characters)")
    if not _THEME_NAME_RE.match(name):
        raise ValueError(
            "Theme name must start with a letter or digit "
            "and contain only letters, digits, underscores, and hyphens"
        )
    if name.lower() in _RESERVED_THEME_NAMES:
        raise ValueError(f"{name!r} is a built-in theme and cannot be overwritten")
    return name


def _theme_file(name: str) -> Path:
    return THEMES_DIR / f"{name}.ini"


def list_themes() -> list[str]:
    """Sorted names of saved user themes (built-ins not included)."""
    try:
        return sorted(p.stem for p in THEMES_DIR.glob("*.ini"))
    except OSError:
        return []


def save_theme(name: str) -> None:
    """Save the current theme.ini palette as the named user theme.

    Overwrites an existing theme of the same name (that is the "Save"
    semantics; the caller confirms before overwriting).
    """
    name = _validate_theme_name(name)
    ensure_theme_file()
    cfg = ConfigParser()
    cfg = _safe_read_config(cfg, THEME_PATH)
    _write_ini_file(_theme_file(name), cfg)


def apply_theme(name: str) -> None:
    """Overwrite cfg/theme.ini from the named user theme (atomically).

    The caller then runs load_theme() + its on_theme_change callback for
    the live apply.  Raises ValueError if the theme does not exist.
    """
    name = name.strip()
    path = _theme_file(name)
    if not path.exists():
        raise ValueError(f"Theme {name!r} does not exist")
    cfg = ConfigParser()
    cfg = _safe_read_config(cfg, path)
    if not cfg.sections():
        # A corrupt/empty theme file must never clobber the live palette.
        raise ValueError(f"Theme {name!r} is corrupt (no theme sections)")
    _write_ini_file(THEME_PATH, cfg)


def delete_theme(name: str) -> None:
    """Delete a saved user theme.  Built-in themes cannot be deleted."""
    name = name.strip()
    if name.lower() in _RESERVED_THEME_NAMES:
        raise ValueError(f"Cannot delete the built-in theme {name!r}")
    path = _theme_file(name)
    if not path.exists():
        raise ValueError(f"Theme {name!r} does not exist")
    path.unlink()


def rename_theme(old: str, new: str) -> None:
    """Rename a saved user theme (atomic file move).

    Optional convenience mirroring profile rename; not wired into the UI.
    """
    old = old.strip()
    new = _validate_theme_name(new)
    old_path = _theme_file(old)
    if not old_path.exists():
        raise ValueError(f"Theme {old!r} does not exist")
    new_path = _theme_file(new)
    if new_path.exists():
        raise ValueError(f"Theme {new!r} already exists")
    new_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(old_path), str(new_path))


def ensure_settings_file() -> None:
    cfg = ConfigParser()
    if SETTINGS_PATH.exists():
        cfg = _safe_read_config(cfg, SETTINGS_PATH)
    changed = False
    for section, values in DEFAULT_SETTINGS.items():
        if section not in cfg:
            cfg[section] = values
            changed = True
        else:
            for key, value in values.items():
                if key not in cfg[section]:
                    cfg[section][key] = value
                    changed = True
    if not SETTINGS_PATH.exists() or changed:
        _write_ini_file(SETTINGS_PATH, cfg)


def get_setting(section: str, fallback: bool = True) -> bool:
    """Read one boolean settings section (default *fallback* on any error)."""
    ensure_settings_file()
    cfg = ConfigParser()
    try:
        cfg.read(str(SETTINGS_PATH))
        return cfg.getboolean(section, "is_true", fallback=fallback)
    except Exception:
        return fallback


def set_setting(section: str, enabled: bool) -> None:
    """Write one boolean settings section, preserving every other section.

    Unknown or legacy sections in settings.ini (e.g. ``toggle_frameless``)
    are left untouched.
    """
    ensure_settings_file()
    cfg = ConfigParser()
    cfg = _safe_read_config(cfg, SETTINGS_PATH)
    if section not in cfg:
        cfg[section] = {}
    cfg[section]["is_true"] = "yes" if enabled else "no"
    _write_ini_file(SETTINGS_PATH, cfg)


def get_setting_value(section: str, option: str, fallback: str = "") -> str:
    """Read one arbitrary (non-boolean) setting option.

    Like :func:`get_setting` but for value options (e.g. ``ui_scale``),
    which have no ``is_true`` key.  Returns *fallback* on any error.
    """
    ensure_settings_file()
    cfg = ConfigParser()
    try:
        cfg.read(str(SETTINGS_PATH))
        return cfg.get(section, option, fallback=fallback)
    except Exception:
        return fallback


def set_setting_value(section: str, option: str, value: str) -> None:
    """Write one arbitrary setting option, preserving every other section.

    Unknown or legacy sections in settings.ini are left untouched.
    """
    ensure_settings_file()
    cfg = ConfigParser()
    cfg = _safe_read_config(cfg, SETTINGS_PATH)
    if section not in cfg:
        cfg[section] = {}
    cfg[section][option] = value
    _write_ini_file(SETTINGS_PATH, cfg)
