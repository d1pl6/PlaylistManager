from configparser import ConfigParser
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parents[2] / "cfg" / "settings.ini"

DEFAULT_SETTINGS = {
    "update_check": {"is_true": "yes"},
    "center_windows": {"is_true": "yes"},
    "auto_resize": {"is_true": "no"},
    "global_listener": {"is_true": "yes"},
}

THEME_PATH = Path(__file__).resolve().parents[2] / "cfg" / "theme.ini"

DEFAULT_THEME = {
    "frame_header": {"background": "#181818"},
    "frame_main": {"background": "#404040"},
    "label": {"background": "#404040", "foreground": "#F2F2F2"},
    "checkbutton": {
        "background": "#292929",
        "foreground": "#CBCBCB",
        "selectcolor": "#000000",
        "activebackground": "#5C5C5C",
        "activeforeground": "#E4E4E4",
    },
    "button_header": {
        "background": "#0A0000",
        "activebackground": "#320000",
        "foreground": "white",
    },
    "button_close": {
        "background": "#0A0000",
        "activebackground": "#320000",
        "foreground": "white",
    },
    "button_main": {
        "background": "#9A9A9A",
        "activebackground": "#868686",
        "foreground": "black",
    },
}


def ensure_theme_file() -> None:
    THEME_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg = ConfigParser()
    if THEME_PATH.exists():
        cfg.read(str(THEME_PATH))
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
        with open(THEME_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)


def get_theme_value(section: str, option: str, fallback: str) -> str:
    ensure_theme_file()
    cfg = ConfigParser()
    cfg.read(str(THEME_PATH))
    return cfg.get(section, option, fallback=fallback)


def set_theme_value(section: str, option: str, value: str) -> None:
    ensure_theme_file()
    cfg = ConfigParser()
    cfg.read(str(THEME_PATH))
    if section not in cfg:
        cfg[section] = {}
    cfg[section][option] = value
    with open(THEME_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)


THEME_PRESETS = {
    "white": {
        "frame_header": {"background": "#F2F2F2"},
        "frame_main": {"background": "#FFFFFF"},
        "label": {"background": "#FFFFFF", "foreground": "#000000"},
        "checkbutton": {
            "background": "#F0F0F0",
            "foreground": "#000000",
            "selectcolor": "#000000",
            "activebackground": "#D0D0D0",
            "activeforeground": "#000000",
        },
        "button_header": {
            "background": "#E0E0E0",
            "activebackground": "#C0C0C0",
            "foreground": "#000000",
        },
        "button_close": {
            "background": "#E0E0E0",
            "activebackground": "#C0C0C0",
            "foreground": "#000000",
        },
        "button_main": {
            "background": "#F0F0F0",
            "activebackground": "#D0D0D0",
            "foreground": "#000000",
        },
    },
    "dark": {
        "frame_header": {"background": "#101010"},
        "frame_main": {"background": "#252525"},
        "label": {"background": "#252525", "foreground": "#EDEDED"},
        "checkbutton": {
            "background": "#303030",
            "foreground": "#DADADA",
            "selectcolor": "#505050",
            "activebackground": "#404040",
            "activeforeground": "#FFFFFF",
        },
        "button_header": {
            "background": "#1A1A1A",
            "activebackground": "#333333",
            "foreground": "#FFFFFF",
        },
        "button_close": {
            "background": "#1A1A1A",
            "activebackground": "#333333",
            "foreground": "#FFFFFF",
        },
        "button_main": {
            "background": "#3A3A3A",
            "activebackground": "#555555",
            "foreground": "#FFFFFF",
        },
    },
}


def restore_theme_defaults() -> None:
    ensure_theme_file()
    cfg = ConfigParser()
    for section, values in DEFAULT_THEME.items():
        cfg[section] = values
    with open(THEME_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)


def apply_theme_preset(preset: str) -> None:
    ensure_theme_file()
    values = THEME_PRESETS.get(preset.lower())
    if not values:
        return
    cfg = ConfigParser()
    cfg.read(str(THEME_PATH))
    for section, options in values.items():
        cfg[section] = options
    with open(THEME_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)


def ensure_settings_file() -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg = ConfigParser()
    if SETTINGS_PATH.exists():
        cfg.read(str(SETTINGS_PATH))
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
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)
