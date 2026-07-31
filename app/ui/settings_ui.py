import logging
import tkinter as tk
from configparser import ConfigParser

from ui.settings_theme_ui import show_theme_dialog
from utils.config import (
    ensure_settings_file,
    SETTINGS_PATH as _settings_path,
)
from utils.theme import C
from utils.window import center_window, resize_window

logger = logging.getLogger(__name__)


def _toggle_setting(section, var):
    ensure_settings_file()
    cfg = ConfigParser()
    cfg.read(str(_settings_path))
    if section not in cfg:
        # Only create the section if missing — don't wipe the other
        # sections (a bare section assignment replaces them).
        cfg[section] = {}
    cfg[section]["is_true"] = "yes" if var.get() else "no"
    with open(_settings_path, "w", encoding="utf-8") as f:
        cfg.write(f)


def show_settings_dialog(parent, keybind_controller=None, on_theme_change=None):
    theme_win_bg = C["frame_main_bg"]
    theme_header_bg = C["frame_head_bg"]
    theme_label_bg = C["label_def_bg"]
    theme_label_fg = C["label_def_fg"]
    theme_check_bg = C["checkbutton_bg"]
    theme_check_fg = C["checkbutton_fg"]
    theme_check_select = C["checkbutton_selector"]
    theme_check_abg = C["checkbutton_a_bg"]
    theme_check_afg = C["checkbutton_a_fg"]

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
    ).pack(fill="both", pady=5, padx=5)

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
        background=C["button_main_bg"],
        activebackground=C["button_main_a_bg"],
        foreground=C["button_main_fg"],
        font=("Noto", 10),
        bd=0,
    ).pack(fill="both", padx=4, pady=4)

    if center_var_value == 1:
        center_window(win)
