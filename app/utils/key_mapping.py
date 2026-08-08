"""
Key normalisation and hotkey parsing utilities.

Separated from the keybind controller so the mapping functions can be
tested and reused independently of the pynput listener loop.
"""

from configparser import ConfigParser
from typing import Optional, Set

from pynput import keyboard
from utils.config import ensure_settings_file, SETTINGS_PATH

# -- pynput key → normalised name -------------------------------------------------
_KEY_MAP = {
    keyboard.Key.ctrl_l: "ctrl",
    keyboard.Key.ctrl_r: "ctrl",
    keyboard.Key.alt_l: "alt",
    keyboard.Key.alt_r: "alt",
    # Note: AltGr deliberately omitted - it is a distinct modifier on European
    # layouts and should NOT be conflated with Alt.
    keyboard.Key.shift: "shift",
    keyboard.Key.shift_l: "shift",
    keyboard.Key.shift_r: "shift",
    keyboard.Key.cmd: "cmd",
    keyboard.Key.cmd_l: "cmd",
    keyboard.Key.cmd_r: "cmd",
}

# -- tkinter keysym → normalised name ---------------------------------------------
_TK_KEY_MAP = {
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Control": "ctrl",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Alt": "alt",
    "Alt_gr": "alt",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Shift": "shift",
    "Super_L": "cmd",
    "Super_R": "cmd",
    "Escape": "escape",
}

# Names treated as modifiers (no keybind can consist only of these).
MODIFIER_NAMES: Set[str] = {"ctrl", "alt", "shift", "cmd"}


def normalize_key(key) -> Optional[str]:
    if key in _KEY_MAP:
        return _KEY_MAP[key]
    if isinstance(key, keyboard.KeyCode) and key.char:
        return key.char.lower()
    if not isinstance(key, keyboard.KeyCode) and hasattr(key, "name"):
        return key.name
    return None


def normalize_tk_key(keysym: str) -> Optional[str]:
    if keysym in _TK_KEY_MAP:
        return _TK_KEY_MAP[keysym]
    if len(keysym) == 1:
        return keysym.lower()
    if keysym.startswith("F") and keysym[1:].isdigit():
        return keysym.lower()
    if keysym in (
        "space",
        "Return",
        "BackSpace",
        "Tab",
        "Delete",
        "Home",
        "End",
        "Left",
        "Right",
        "Up",
        "Down",
        "Prior",
        "Next",
    ):
        return keysym.lower()
    return None


def parse_hotkey(hotkey_str: str) -> Set[str]:
    """Split ``"ctrl+shift+a"`` into ``{'ctrl', 'shift', 'a'}``."""
    return {k.strip().lower() for k in hotkey_str.split("+") if k.strip()}


def read_global_listener_setting() -> bool:
    """Read the ``global_listener`` boolean from settings.ini (default: True)."""
    ensure_settings_file()
    cfg = ConfigParser()
    try:
        cfg.read(str(SETTINGS_PATH))
        return cfg.getboolean("global_listener", "is_true", fallback=True)
    except Exception:
        return True
