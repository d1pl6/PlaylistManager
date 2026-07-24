from configparser import ConfigParser
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parents[2] / "cfg" / "settings.ini"

DEFAULT_SETTINGS = {
    "update_check": {"is_true": "yes"},
    "center_windows": {"is_true": "yes"},
    "global_listener": {"is_true": "yes"},
}


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
