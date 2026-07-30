import logging
import tkinter as tk
from configparser import ConfigParser

from utils.config import (
    ensure_settings_file,
    get_theme_value,
    SETTINGS_PATH as _settings_path,
)

logger = logging.getLogger(__name__)


from utils import center_window
from utils import resize_window
from ui.settings_theme_ui import show_theme_dialog


def _toggle_setting(section, var):
    ensure_settings_file()
    cfg = ConfigParser()
    cfg.read(str(_settings_path))
    cfg[section] = {"is_true": "yes" if var.get() else "no"}
    with open(_settings_path, "w", encoding="utf-8") as f:
        cfg.write(f)


def _theme_value(section: str, option: str, default: str) -> str:
    try:
        return get_theme_value(section, option, default)
    except Exception:
        return default


def show_settings_dialog(parent, keybind_controller=None, on_theme_change=None):
    theme_win_bg = _theme_value("frame_main", "background", "#181818")
    theme_header_bg = _theme_value("frame_header", "background", "#181818")
    theme_label_bg = _theme_value("label", "background", "#181818")
    theme_label_fg = _theme_value("label", "foreground", "#F2F2F2")
    theme_check_bg = _theme_value("checkbutton", "background", "#292929")
    theme_check_fg = _theme_value("checkbutton", "foreground", "#CBCBCB")
    theme_check_select = _theme_value("checkbutton", "selectcolor", "#000000")
    theme_check_abg = _theme_value("checkbutton", "activebackground", "#5C5C5C")
    theme_check_afg = _theme_value("checkbutton", "activeforeground", "#E4E4E4")
    theme_button_c_bg = _theme_value("button_close", "background", "#0A0000")
    theme_button_c_fg = _theme_value("button_close", "foreground", "white")
    theme_button_c_abg = _theme_value("button_close", "activebackground", "#320000")
    theme_button_c_afg = _theme_value("button_close", "activeforeground", "#FF0000")

    win = tk.Toplevel(parent)
    win.title("PlaylistManager")
    win.configure(background=theme_win_bg, padx=2, pady=2)
    win.transient(parent)
    win.grab_set()

    tk.Label(
        win,
        text="Settings",
        background=theme_header_bg,
        foreground=theme_label_fg,
        font=("Noto", 12),
    ).pack(fill="both")

    ensure_settings_file()
    cfg = ConfigParser()
    try:
        cfg.read(str(_settings_path))
        update_var_value = 1 if cfg.getboolean("update_check", "is_true", fallback=True) else 0
        center_var_value = 1 if cfg.getboolean("center_windows", "is_true", fallback=True) else 0
        resize_var_value = 1 if cfg.getboolean("auto_resize", "is_true", fallback=False) else 0
        global_var_value = 1 if cfg.getboolean("global_listener", "is_true", fallback=True) else 0
    except Exception as e:
        logger.error("Error loading config, defaulting to yes: %s", e)
        update_var_value = 1
        center_var_value = 1
        resize_var_value = 0
        global_var_value = 1

    update_var = tk.IntVar(value=update_var_value)
    center_var = tk.IntVar(value=center_var_value)
    resize_var = tk.IntVar(value=resize_var_value)
    global_var = tk.IntVar(value=global_var_value)

    tk.Checkbutton(
        win,
        text="Check for updates on startup?",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=("Noto", 10),
        command=lambda: _toggle_setting("update_check", update_var),
        variable=update_var,
    ).pack(fill="both")

    tk.Checkbutton(
        win,
        text="Center windows after launch?",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=("Noto", 10),
        command=lambda: (
            _toggle_setting("center_windows", center_var),
            center_var.get() and center_window(win),
        ),
        variable=center_var,
    ).pack(fill="both")

    tk.Checkbutton(
        win,
        text="Auto-resize main window?",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=("Noto", 10),
        command=lambda: (
            _toggle_setting("auto_resize", resize_var),
            resize_var.get() and resize_window(parent)
        ),
        variable=resize_var,
    ).pack(fill="both")

    def _on_global_toggle():
        _toggle_setting("global_listener", global_var)
        if keybind_controller is not None:
            keybind_controller.set_global_listener(global_var.get() == 1)

    tk.Checkbutton(
        win,
        text="Use global key listener?",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=("Noto", 10),
        command=_on_global_toggle,
        variable=global_var,
    ).pack(fill="both")

    tk.Button(
        win,
        text="Theme Settings",
        command=lambda: show_theme_dialog(win, on_theme_change=on_theme_change),
        background=_theme_value("button_main", "background", "#0A0000"),
        activebackground=_theme_value("button_main", "activebackground", "#320000"),
        foreground=_theme_value("button_main", "foreground", "white"),
        font=("Noto", 10),
        bd=0,
    ).pack(fill="both", padx=4, pady=4)

    tk.Button(
        win,
        text="Close",
        command=win.destroy,
        background=theme_button_c_bg,
        foreground=theme_button_c_fg,
        activebackground=theme_button_c_abg,
        activeforeground=theme_button_c_afg,
        bd=0,
    ).pack(fill="both")

    if center_var_value == 1:
        center_window(win)
