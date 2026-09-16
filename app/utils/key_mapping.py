"""
Key normalisation and keybind parsing utilities.

Separated from the keybind controller so the mapping functions can be
tested and reused independently of the pynput listener loop.
"""

from pynput import keyboard

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
_TK_KEY_MAP: dict[str, str] = {
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Control": "ctrl",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Alt": "alt",
    # AltGr deliberately omitted - matches the pynput map: it is a distinct
    # modifier on European layouts and must not be conflated with Alt.
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Shift": "shift",
    "Super_L": "cmd",
    "Super_R": "cmd",
    "Escape": "escape",
}

#: Punctuation keysyms: Tk reports named keysyms ("," is "comma", "/" is
#: "slash"), while pynput reports the literal character.  Normalising both
#: sides to the literal char keeps local (Tk) and global (pynput) keybinds
#: interchangeable, e.g. "ctrl+," matches in both modes.
_PUNCT_KEYSYM: dict[str, str] = {
    "comma": ",",
    "period": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "colon": ":",
    "apostrophe": "'",
    "quotedbl": '"',
    "minus": "-",
    "equal": "=",
    "plus": "+",
    "asterisk": "*",
    "numbersign": "#",
    "percent": "%",
    "question": "?",
    "exclam": "!",
    "at": "@",
    "dollar": "$",
    "asciicircum": "^",
    "ampersand": "&",
    "parenleft": "(",
    "parenright": ")",
    "underscore": "_",
    "bracketleft": "[",
    "bracketright": "]",
    "braceleft": "{",
    "braceright": "}",
    "less": "<",
    "greater": ">",
    "bar": "|",
    "grave": "`",
    "asciitilde": "~",
}

# Names treated as modifiers (no keybind can consist only of these).
MODIFIER_NAMES: set[str] = {"ctrl", "alt", "shift", "cmd"}


def normalize_key(key) -> str | None:
    if key in _KEY_MAP:
        return _KEY_MAP[key]
    if isinstance(key, keyboard.KeyCode) and key.char:
        return key.char.lower()
    if not isinstance(key, keyboard.KeyCode) and hasattr(key, "name"):
        return key.name
    return None


def normalize_tk_key(keysym: str) -> str | None:
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
    return _PUNCT_KEYSYM.get(keysym)


def parse_keybind(keybind_str: str) -> set[str]:
    """Split ``"ctrl+shift+a"`` into ``{'ctrl', 'shift', 'a'}``.

    The literal ``+`` key is spelled ``plus`` (both in recordings and when
    typed by hand): the separator would otherwise swallow it - ``"ctrl++"``
    reads back as ``{'ctrl'}``.  ``"ctrl+plus"`` round-trips to the same
    live key set as the physical plus key (``'+'``).
    """
    tokens = {k.strip().lower() for k in keybind_str.split("+") if k.strip()}
    if "plus" in tokens:
        tokens.remove("plus")
        tokens.add("+")
    return tokens