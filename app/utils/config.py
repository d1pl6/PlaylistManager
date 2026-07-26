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
    "root_background": {"background": "#1A1A1A"},
    "frame_header": {"background": "#101010"},
    "frame_main": {"background": "#252525"},
    "frame_playlist": {"background": "#252525"},
    "label_default": {"background": "#252525", "foreground": "#EDEDED"},
    "label_playlist": {"background": "#2f2f2f", "foreground": "#EDEDED"},
    "label_playlist_name": {"background": "#2f2f2f", "foreground": "#DCDCDC"},
    "label_playlist_log": {"background": "#2f2f2f", "foreground": "#EDEDED"},
    "label_playlist_good": {"background": "#00C600", "foreground": "#EDEDED"},
    "label_playlist_warning": {"background": "#C68100", "foreground": "#EDEDED"},
    "label_playlist_error": {"background": "#C60000", "foreground": "#EDEDED"},
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
    "entry_default": {
        "background": "#404040",
        "foreground": "#FFFFFF",
        "readonlybackground": "#2A2A2A",
        "readonlyforeground": "#AEAEAE",
    },
    "entry_playlist": {
        "background": "#404040",
        "foreground": "#FFFFFF",
        "readonlybackground": "#2A2A2A",
        "readonlyforeground": "#AEAEAE",
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
        "root_background": {"background": "#F5F5F5"},
        "frame_header": {"background": "#EDEDED"},
        "frame_main": {"background": "#FFFFFF"},
        "frame_playlist": {"background": "#FFFFFF"},
        "label_default": {"background": "#FFFFFF", "foreground": "#1A1A1A"},
        "label_playlist": {"background": "#F2F2F2", "foreground": "#1A1A1A"},
        "label_playlist_name": {"background": "#F2F2F2", "foreground": "#333333"},
        "label_playlist_log": {"background": "#F2F2F2", "foreground": "#1A1A1A"},
        "label_playlist_good": {"background": "#00A800", "foreground": "#FFFFFF"},
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
        "entry_default": {
            "background": "#FFFFFF",
            "foreground": "#111111",
            "readonlybackground": "#F3F3F3",
            "readonlyforeground": "#666666",
        },
        "entry_playlist": {
            "background": "#FFFFFF",
            "foreground": "#111111",
            "readonlybackground": "#F3F3F3",
            "readonlyforeground": "#666666",
        },
    },
    "dark": {
        "root_background": {"background": "#1A1A1A"},
        "frame_header": {"background": "#101010"},
        "frame_main": {"background": "#252525"},
        "frame_playlist": {"background": "#252525"},
        "label_default": {"background": "#252525", "foreground": "#EDEDED"},
        "label_playlist": {"background": "#2f2f2f", "foreground": "#EDEDED"},
        "label_playlist_name": {"background": "#2f2f2f", "foreground": "#DCDCDC"},
        "label_playlist_log": {"background": "#2f2f2f", "foreground": "#EDEDED"},
        "label_playlist_good": {"background": "#00C600", "foreground": "#EDEDED"},
        "label_playlist_warning": {"background": "#C68100", "foreground": "#EDEDED"},
        "label_playlist_error": {"background": "#C60000", "foreground": "#EDEDED"},
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
        "entry_default": {
            "background": "#404040",
            "foreground": "#FFFFFF",
            "readonlybackground": "#2A2A2A",
            "readonlyforeground": "#AEAEAE",
        },
        "entry_playlist": {
            "background": "#404040",
            "foreground": "#FFFFFF",
            "readonlybackground": "#2A2A2A",
            "readonlyforeground": "#AEAEAE",
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
