"""Unit tests for utils/key_mapping.py - key normalization and parsing.

Pure string/enum functions - no listener needed, no display required.
"""

from pynput.keyboard import Key, KeyCode

from utils import key_mapping


class TestNormalizeKey:
    """pynput key objects -> normalised names."""

    def test_ctrl_both_sides_map_to_ctrl(self):
        assert key_mapping.normalize_key(Key.ctrl_l) == "ctrl"
        assert key_mapping.normalize_key(Key.ctrl_r) == "ctrl"

    def test_alt_both_sides_map_to_alt(self):
        assert key_mapping.normalize_key(Key.alt_l) == "alt"
        assert key_mapping.normalize_key(Key.alt_r) == "alt"

    def test_shift_variants(self):
        assert key_mapping.normalize_key(Key.shift) == "shift"
        assert key_mapping.normalize_key(Key.shift_l) == "shift"
        assert key_mapping.normalize_key(Key.shift_r) == "shift"

    def test_cmd_variants(self):
        assert key_mapping.normalize_key(Key.cmd) == "cmd"
        assert key_mapping.normalize_key(Key.cmd_l) == "cmd"
        assert key_mapping.normalize_key(Key.cmd_r) == "cmd"

    def test_keycode_char_lowercased(self):
        assert key_mapping.normalize_key(KeyCode.from_char("A")) == "a"
        assert key_mapping.normalize_key(KeyCode.from_char("z")) == "z"

    def test_special_key_by_name(self):
        assert key_mapping.normalize_key(Key.space) == "space"
        assert key_mapping.normalize_key(Key.f5) == "f5"

    def test_unknown_object_returns_none(self):
        assert key_mapping.normalize_key("not-a-key") is None


class TestNormalizeTkKey:
    """tkinter keysyms -> normalised names."""

    def test_modifier_keysyms(self):
        assert key_mapping.normalize_tk_key("Control_L") == "ctrl"
        assert key_mapping.normalize_tk_key("Control_R") == "ctrl"
        assert key_mapping.normalize_tk_key("Alt_L") == "alt"
        assert key_mapping.normalize_tk_key("Shift_R") == "shift"
        assert key_mapping.normalize_tk_key("Super_L") == "cmd"

    def test_single_char_lowercased(self):
        assert key_mapping.normalize_tk_key("A") == "a"
        assert key_mapping.normalize_tk_key("b") == "b"

    def test_function_keys_lowercased(self):
        assert key_mapping.normalize_tk_key("F5") == "f5"
        assert key_mapping.normalize_tk_key("F12") == "f12"

    def test_special_keys(self):
        assert key_mapping.normalize_tk_key("space") == "space"
        assert key_mapping.normalize_tk_key("Return") == "return"
        assert key_mapping.normalize_tk_key("Delete") == "delete"
        assert key_mapping.normalize_tk_key("Escape") == "escape"

    def test_empty_or_unknown_returns_none(self):
        assert key_mapping.normalize_tk_key("") is None
        assert key_mapping.normalize_tk_key("NonExistentKey") is None


class TestParseKeybind:
    def test_simple_combo(self):
        assert key_mapping.parse_keybind("ctrl+shift+a") == {"ctrl", "shift", "a"}

    def test_whitespace_tolerated(self):
        assert key_mapping.parse_keybind(" ctrl + alt + f4 ") == {"ctrl", "alt", "f4"}

    def test_single_key(self):
        assert key_mapping.parse_keybind("f5") == {"f5"}

    def test_case_insensitive(self):
        assert key_mapping.parse_keybind("CTRL+SHIFT+A") == {"ctrl", "shift", "a"}

    def test_empty_string(self):
        assert key_mapping.parse_keybind("") == set()


class TestModifierSet:
    def test_exact_set(self):
        assert key_mapping.MODIFIER_NAMES == {"ctrl", "alt", "shift", "cmd"}