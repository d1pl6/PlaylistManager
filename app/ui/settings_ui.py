import logging
import tkinter as tk
from configparser import ConfigParser
from tkinter import ttk

from ui.settings_theme_ui import show_theme_dialog
from utils.config import (
    ensure_settings_file,
    get_setting_value,
    set_setting,
    set_setting_value,
    SETTINGS_PATH as _settings_path,
)
from utils.scaling import UI_SCALE_PRESETS, ui_font
from utils.theme import C
from utils.window import center_window

logger = logging.getLogger(__name__)


def _toggle_setting(section, var):
    try:
        set_setting(section, bool(var.get()))
    except Exception as e:
        logger.error("Failed to write settings file: %s", e)


def show_settings_dialog(parent, keybind_controller=None, on_theme_change=None, tray_available=None, on_tray_toggle=None, on_auto_resize_toggle=None, on_showcase_count_change=None, on_showcase_log_change=None):
    """Show the settings dialog.

    Args:
        parent: tkinter parent window.
        keybind_controller: optional KeybindController for the global
            listener toggle.
        on_theme_change: optional callback re-applying the theme.
        tray_available: optional TrayService (or any object with an
            ``available`` attribute); when falsy/absent the hide-to-tray
            checkbutton is disabled.
        on_tray_toggle: optional callback applied live with the new
            hide-to-tray state (bool) when the checkbox changes.
        on_auto_resize_toggle: optional callback applied live with the
            new auto-resize state (bool) when the checkbox changes -
            without it the toggle only takes effect after a restart.
        on_showcase_count_change: optional callback applied live with the
            new showcase count (int) when the combobox changes - without
            it the change only takes effect after a restart.
        on_showcase_log_change: optional callback applied live with the
            new show-log-row state (bool) when the checkbox changes.
    """
    theme_win_bg = C["frame_main_bg"]
    theme_header_bg = C["frame_head_bg"]
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
        font=ui_font(12),
    ).pack(fill="both", pady=5, padx=5)

    ensure_settings_file()
    cfg = ConfigParser()
    try:
        cfg.read(str(_settings_path))
        update_var_value = 1 if cfg.getboolean("update_check", "is_true", fallback=True) else 0
        center_var_value = 1 if cfg.getboolean("center_windows", "is_true", fallback=True) else 0
        resize_var_value = 1 if cfg.getboolean("auto_resize", "is_true", fallback=False) else 0
        global_var_value = 1 if cfg.getboolean("global_listener", "is_true", fallback=True) else 0
        tray_var_value = 1 if cfg.getboolean("hide_to_tray", "is_true", fallback=False) else 0
        showcase_count_value = cfg.getint("showcase", "count", fallback=0)
        showcase_log_value = 1 if cfg.getboolean("showcase_log", "is_true", fallback=True) else 0
    except Exception as e:
        logger.error("Error loading settings, using defaults: %s", e)
        update_var_value = 1
        center_var_value = 1
        resize_var_value = 0
        global_var_value = 1
        tray_var_value = 0
        showcase_count_value = 0
        showcase_log_value = 1

    update_var = tk.IntVar(value=update_var_value)
    center_var = tk.IntVar(value=center_var_value)
    resize_var = tk.IntVar(value=resize_var_value)
    global_var = tk.IntVar(value=global_var_value)
    tray_var = tk.IntVar(value=tray_var_value)

    tk.Checkbutton(
        win,
        text="Check for updates on startup?",
        cursor="hand2",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=ui_font(10),
        command=lambda: _toggle_setting("update_check", update_var),
        variable=update_var,
    ).pack(fill="both")

    tk.Checkbutton(
        win,
        text="Center windows after launch?",
        cursor="hand2",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=ui_font(10),
        command=lambda: (
            _toggle_setting("center_windows", center_var),
            center_var.get() and center_window(win),
        ),
        variable=center_var,
    ).pack(fill="both")

    def _on_auto_resize_toggle():
        _toggle_setting("auto_resize", resize_var)
        if on_auto_resize_toggle is not None:
            on_auto_resize_toggle(resize_var.get() == 1)

    tk.Checkbutton(
        win,
        text="Auto-resize main window?",
        cursor="hand2",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=ui_font(10),
        command=_on_auto_resize_toggle,
        variable=resize_var,
    ).pack(fill="both")

    def _on_global_toggle():
        _toggle_setting("global_listener", global_var)
        if keybind_controller is not None:
            keybind_controller.set_global_listener(global_var.get() == 1)

    tk.Checkbutton(
        win,
        text="Use global key listener?",
        cursor="hand2",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=ui_font(10),
        command=_on_global_toggle,
        variable=global_var,
    ).pack(fill="both")

    def _on_tray_toggle():
        _toggle_setting("hide_to_tray", tray_var)
        if on_tray_toggle is not None:
            on_tray_toggle(tray_var.get() == 1)

    tray_ck = tk.Checkbutton(
        win,
        text="Hide in tray on minimize?",
        cursor="hand2",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=ui_font(10),
        command=_on_tray_toggle,
        variable=tray_var,
    )
    tray_ck.pack(fill="both")
    # Disable when no tray backend is available.
    if not getattr(tray_available, "available", False):
        tray_ck.configure(state="disabled", cursor="arrow")

    # --- Showcase (last N added songs per card) -------------------------
    def _on_showcase_count_change(value: str) -> None:
        try:
            set_setting_value("showcase", "count", value)
        except Exception as e:
            logger.error("Failed to write showcase count setting: %s", e)
        if on_showcase_count_change is not None:
            try:
                on_showcase_count_change(int(value))
            except (ValueError, TypeError):
                pass

    def _on_showcase_log_toggle():
        _toggle_setting("showcase_log", showcase_log_var)
        if on_showcase_log_change is not None:
            on_showcase_log_change(showcase_log_var.get() == 1)

    showcase_row = tk.Frame(win, background=theme_check_bg)
    showcase_row.pack(fill="both", padx=4, pady=(2, 0))
    tk.Label(
        showcase_row,
        text="Show last N added songs:",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(10),
    ).pack(side="left", padx=(6, 4), pady=4)

    showcase_count_var = tk.StringVar(value=str(showcase_count_value))
    showcase_combo = ttk.Combobox(
        showcase_row,
        textvariable=showcase_count_var,
        cursor="hand2",
        values=("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"),
        state="readonly",
        width=4,
        font=ui_font(10),
    )
    showcase_combo.pack(side="left", padx=(0, 6), pady=4)
    showcase_combo.bind(
        "<<ComboboxSelected>>",
        lambda e: _on_showcase_count_change(showcase_count_var.get()),
    )
    tk.Label(
        showcase_row,
        text="(0 = off)",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(9),
    ).pack(side="left", padx=(0, 6))

    showcase_log_var = tk.IntVar(value=showcase_log_value)
    tk.Checkbutton(
        win,
        text="Show log row (artist / song / status)",
        cursor="hand2",
        background=theme_check_bg,
        foreground=theme_check_fg,
        selectcolor=theme_check_select,
        activebackground=theme_check_abg,
        activeforeground=theme_check_afg,
        font=ui_font(10),
        command=_on_showcase_log_toggle,
        variable=showcase_log_var,
    ).pack(fill="both")

    # --- UI scale (profile) ---------------------------------------------
    def _on_ui_scale_change(value: str) -> None:
        try:
            set_setting_value("ui_scale", "value", value)
        except Exception as e:
            logger.error("Failed to write ui_scale setting: %s", e)
        # V1: applies on next launch (see screen.md §7 "Apply timing").

    scale_row = tk.Frame(win, background=theme_check_bg)
    scale_row.pack(fill="both", padx=4, pady=(2, 0))
    tk.Label(
        scale_row,
        text="UI scale:",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(10),
    ).pack(side="left", padx=(6, 4), pady=4)

    scale_var = tk.StringVar(
        value=get_setting_value("ui_scale", "value", "auto")
    )
    scale_combo = ttk.Combobox(
        scale_row,
        textvariable=scale_var,
        cursor="hand2",
        values=UI_SCALE_PRESETS,
        state="readonly",
        width=6,
        font=ui_font(10),
    )
    scale_combo.pack(side="left", padx=(0, 6), pady=4)
    scale_combo.bind(
        "<<ComboboxSelected>>",
        lambda e: _on_ui_scale_change(scale_var.get()),
    )
    tk.Label(
        scale_row,
        text="(restart to apply; auto follows the display)",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(9),
    ).pack(side="left", padx=(0, 6))

    tk.Button(
        win,
        text="Theme Settings",
        cursor="hand2",
        background=C["button_main_bg"],
        activebackground=C["button_main_a_bg"],
        foreground=C["button_main_fg"],
        font=ui_font(10),
        highlightthickness=0,
        relief="raised",
        bd=0,
        command=lambda: show_theme_dialog(win, on_theme_change=on_theme_change),
    ).pack(fill="both", padx=4, pady=4)

    if center_var_value == 1:
        center_window(win)
