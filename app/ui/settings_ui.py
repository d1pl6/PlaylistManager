import logging
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser
from configparser import ConfigParser

from utils.config import (
    ensure_settings_file,
    ensure_theme_file,
    get_theme_value,
    set_theme_value,
    apply_theme_preset,
    restore_theme_defaults,
    SETTINGS_PATH as _settings_path,
)

logger = logging.getLogger(__name__)


from utils import center_window
from utils import resize_window


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
    theme_button_bg = _theme_value("button_main", "background", "#0A0000")
    theme_button_abg = _theme_value("button_main", "activebackground", "#320000")
    theme_button_fg = _theme_value("button_main", "foreground", "white")
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
        update_val = cfg.get("update_check", "is_true", fallback="yes").lower()
        update_var_value = 1 if update_val == "yes" else 0
        center_val = cfg.get("center_windows", "is_true", fallback="yes").lower()
        center_var_value = 1 if center_val == "yes" else 0
        resize_val = cfg.get("auto_resize", "is_true", fallback="yes").lower()
        resize_var_value = 1 if resize_val == "yes" else 0
        global_val = cfg.get("global_listener", "is_true", fallback="yes").lower()
        global_var_value = 1 if global_val == "yes" else 0
    except Exception as e:
        logger.error("Error loading config, defaulting to yes: %s", e)
        update_var_value = 1
        center_var_value = 1
        resize_var_value = 1
        global_var_value = 1

    update_var = tk.IntVar(value=update_var_value)
    center_var = tk.IntVar(value=center_var_value)
    resize_var = tk.IntVar(value=resize_var_value)
    global_var = tk.IntVar(value=global_var_value)

    tk.Checkbutton(
        win,
        text="Enable check for Updates?",
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

    tk.Label(
        win,
        text="Theme settings",
        background=theme_header_bg,
        foreground=theme_label_fg,
        font=("Noto", 12),
    ).pack(fill="both")

    ensure_theme_file()
    theme_cfg = ConfigParser()
    theme_cfg.read(str(Path(__file__).resolve().parents[2] / "cfg" / "theme.ini"))

    def _choose_color(section, option, button):
        current = theme_cfg.get(section, option, fallback=get_theme_value(section, option, "#000000"))
        _, color = colorchooser.askcolor(color=current, parent=win)
        if color:
            theme_cfg[section][option] = color
            set_theme_value(section, option, color)
            button.config(background=color)
            if callable(on_theme_change):
                on_theme_change()

    def _create_theme_button(label_text, section, option, default):
        frame = tk.Frame(win, background=theme_win_bg)
        frame.pack(fill="x", pady=2)

        tk.Label(
            frame,
            text=label_text,
            background=theme_label_bg,
            foreground=theme_label_fg,
            font=("Noto", 10),
            width=24,
            anchor="w",
        ).pack(side="left", padx=(4, 4))

        value = theme_cfg.get(section, option, fallback=default)
        btn = tk.Button(
            frame,
            text="Choose",
            command=lambda: _choose_color(section, option, btn),
            background=value,
            activebackground=value,
            foreground="#ffffff" if value.lower() != "#ffffff" else "#000000",
            bd=0,
        )
        btn.pack(side="right", padx=4)
        return btn

    def _apply_preset(preset):
        apply_theme_preset(preset)
        theme_cfg.read(str(Path(__file__).resolve().parents[2] / "cfg" / "theme.ini"))

    def _restore_defaults():
        restore_theme_defaults()
        theme_cfg.read(str(Path(__file__).resolve().parents[2] / "cfg" / "theme.ini"))

    _create_theme_button("Frame header background", "frame_header", "background", "#181818")
    _create_theme_button("Frame main background", "frame_main", "background", "#404040")
    _create_theme_button("Label background", "label", "background", "#404040")
    _create_theme_button("Label foreground", "label", "foreground", "#F2F2F2")
    _create_theme_button("Checkbutton background", "checkbutton", "background", "#292929")
    _create_theme_button("Checkbutton foreground", "checkbutton", "foreground", "#CBCBCB")
    _create_theme_button("Checkbutton selectcolor", "checkbutton", "selectcolor", "#000000")
    _create_theme_button("Checkbutton activebackground", "checkbutton", "activebackground", "#5C5C5C")
    _create_theme_button("Checkbutton activeforeground", "checkbutton", "activeforeground", "#E4E4E4")
    _create_theme_button("Button header background", "button_header", "background", "#0A0000")
    _create_theme_button("Button header activebackground", "button_header", "activebackground", "#320000")
    _create_theme_button("Button header foreground", "button_header", "foreground", "white")
    _create_theme_button("Button main background", "button_main", "background", "#9A9A9A")
    _create_theme_button("Button main activebackground", "button_main", "activebackground", "#868686")
    _create_theme_button("Button main foreground", "button_main", "foreground", "black")

    button_frame = tk.Frame(win, background=theme_win_bg)
    button_frame.pack(fill="x", pady=6)

    tk.Button(
        button_frame,
        text="White Theme",
        command=lambda: (_apply_preset("white"), on_theme_change() if callable(on_theme_change) else None),
        background=theme_button_bg,
        activebackground=theme_button_abg,
        foreground=theme_button_fg,
        bd=0,
    ).pack(side="left", expand=True, fill="x", padx=2)

    tk.Button(
        button_frame,
        text="Dark Theme",
        command=lambda: (_apply_preset("dark"), on_theme_change() if callable(on_theme_change) else None),
        background=theme_button_bg,
        activebackground=theme_button_abg,
        foreground=theme_button_fg,
        bd=0,
    ).pack(side="left", expand=True, fill="x", padx=2)

    tk.Button(
        button_frame,
        text="Restore Defaults",
        command=lambda: (_restore_defaults(), on_theme_change() if callable(on_theme_change) else None),
        background=theme_button_bg,
        activebackground=theme_button_abg,
        foreground=theme_button_fg,
        bd=0,
    ).pack(side="left", expand=True, fill="x", padx=2)

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
