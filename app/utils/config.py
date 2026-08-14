import logging
import os
import tempfile
from configparser import ConfigParser, Error as ConfigParseError
from pathlib import Path

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).resolve().parents[2] / "cfg" / "settings.ini"

DEFAULT_SETTINGS = {
    "update_check": {"is_true": "yes"},
    "center_windows": {"is_true": "yes"},
    "auto_resize": {"is_true": "no"},
    "global_listener": {"is_true": "yes"},
    "hide_to_tray": {"is_true": "no"},
}

THEME_PATH = Path(__file__).resolve().parents[2] / "cfg" / "theme.ini"

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
        "activebackground": "#404040",
        "activeforeground": "#FFFFFF",
    },
    "button_header": {
        "background": "#6C6C6C",
        "foreground": "#FFFFFF",
        "activebackground": "#868686",
    },
    "button_main": {
        "background": "#3A3A3A",
        "foreground": "#D7D7D7",
        "activebackground": "#555555",
        "activeforeground": "#FFFFFF",
    },
    "button_playlist": {
        "background": "#3A3A3A",
        "foreground": "#D7D7D7",
        "activebackground": "#555555",
        "activeforeground": "#FFFFFF",
    },
    "button_close": {
        "background": "#160000",
        "foreground": "#FFFFFF",
        "activebackground": "#390000",
        "activeforeground": "#FFFFFF"
    },
    "button_save": {
        "background": "#004304",
        "foreground": "#D7D7D7",
        "activebackground": "#006B07",
        "activeforeground": "#FFFFFF"
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
    drive glitch on the exFAT disk) must not take the app down - the
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
            "activebackground": "#E0E0E0",
            "activeforeground": "#000000",
        },
        "button_header": {
            "background": "#D0D0D0",
            "foreground": "#000000",
            "activebackground": "#BEBEBE",
        },
        "button_main": {
            "background": "#E6E6E6",
            "foreground": "#222222",
            "activebackground": "#D2D2D2",
            "activeforeground": "#000000",
        },
        "button_playlist": {
            "background": "#E6E6E6",
            "foreground": "#222222",
            "activebackground": "#D2D2D2",
            "activeforeground": "#000000",
        },
        "button_close": {
            "background": "#F4DADA",
            "foreground": "#000000",
            "activebackground": "#E7BABA",
            "activeforeground": "#000000",
        },
        "button_save": {
            "background": "#004304",
            "foreground": "#D7D7D7",
            "activebackground": "#006B07",
            "activeforeground": "#FFFFFF"
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
    cfg.read(str(SETTINGS_PATH))
    if section not in cfg:
        cfg[section] = {}
    cfg[section]["is_true"] = "yes" if enabled else "no"
    _write_ini_file(SETTINGS_PATH, cfg)
