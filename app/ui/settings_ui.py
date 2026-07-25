import logging
import tkinter as tk
from configparser import ConfigParser

from utils.config import ensure_settings_file, SETTINGS_PATH as _settings_path

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


def show_settings_dialog(parent, keybind_controller=None):
    win = tk.Toplevel(parent)
    win.title("PlaylistManager")
    win.configure(background="#181818")
    win.transient(parent)
    win.grab_set()

    tk.Label(
        win,
        text="Settings",
        background="#181818",
        foreground="#F2F2F2",
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
        background="#292929",
        foreground="#CBCBCB",
        selectcolor="#000000",
        activebackground="#5C5C5C",
        activeforeground="#E4E4E4",
        font=("Noto", 10),
        command=lambda: _toggle_setting("update_check", update_var),
        variable=update_var,
    ).pack(fill="both")

    tk.Checkbutton(
        win,
        text="Center windows after launch?",
        background="#292929",
        foreground="#CBCBCB",
        selectcolor="#000000",
        activebackground="#5C5C5C",
        activeforeground="#E4E4E4",
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
        background="#292929",
        foreground="#CBCBCB",
        selectcolor="#000000",
        activebackground="#5C5C5C",
        activeforeground="#E4E4E4",
        font=("Noto", 10),
        command=lambda: (_toggle_setting("auto_resize", resize_var), resize_var.get() and resize_window(parent)),
        variable=resize_var,
    ).pack(fill="both")

    def _on_global_toggle():
        _toggle_setting("global_listener", global_var)
        if keybind_controller is not None:
            keybind_controller.set_global_listener(global_var.get() == 1)

    tk.Checkbutton(
        win,
        text="Use global key listener?",
        background="#292929",
        foreground="#CBCBCB",
        selectcolor="#000000",
        activebackground="#5C5C5C",
        activeforeground="#E4E4E4",
        font=("Noto", 10),
        command=_on_global_toggle,
        variable=global_var,
    ).pack(fill="both")

    tk.Button(
        win,
        text="Close",
        command=win.destroy,
        background="#0A0000",
        activebackground="#320000",
        activeforeground="#ff0000",
        fg="white",
        bd=0,
    ).pack(fill="both")

    if center_var_value == 1:
        center_window(win)
