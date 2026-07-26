import tkinter as tk
from tkinter import ttk, colorchooser
from configparser import ConfigParser

from utils.config import (
    ensure_theme_file,
    get_theme_value,
    set_theme_value,
    apply_theme_preset,
    restore_theme_defaults,
    THEME_PATH,
)
from utils import center_window


def show_theme_dialog(parent, on_theme_change=None):
    """Open a separate scrollable theme-settings window."""

    win_bg = get_theme_value("frame_main", "background", "#181818")
    header_bg = get_theme_value("frame_header", "background", "#181818")
    label_bg = get_theme_value("label", "background", "#181818")
    label_fg = get_theme_value("label", "foreground", "#F2F2F2")
    button_bg = get_theme_value("button_main", "background", "#0A0000")
    button_abg = get_theme_value("button_main", "activebackground", "#320000")
    button_fg = get_theme_value("button_main", "foreground", "white")
    close_bg = get_theme_value("button_close", "background", "#0A0000")
    close_fg = get_theme_value("button_close", "foreground", "white")
    close_abg = get_theme_value("button_close", "activebackground", "#320000")
    close_afg = get_theme_value("button_close", "activeforeground", "#FF0000")

    win = tk.Toplevel(parent)
    win.title("Theme Settings")
    win.configure(background=win_bg, padx=2, pady=2)
    win.transient(parent)
    win.grab_set()
    win.minsize(350, 400)

    tk.Label(
        win,
        text="Theme settings",
        background=header_bg,
        foreground=label_fg,
        font=("Noto", 12),
    ).pack(fill="x")

    canvas = tk.Canvas(win, background=win_bg, highlightthickness=0)
    scrollbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, background=win_bg)

    inner.bind(
        "<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
    )

    canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.bind(
        "<Configure>",
        lambda e: canvas.itemconfig(canvas_window, width=e.width),
    )

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    inner.bind("<MouseWheel>", _on_mousewheel)
    inner.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
    inner.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

    ensure_theme_file()
    theme_cfg = ConfigParser()
    theme_cfg.read(str(THEME_PATH))

    def _choose_color(section, option, button):
        current = theme_cfg.get(
            section, option, fallback=get_theme_value(section, option, "#000000")
        )
        _, color = colorchooser.askcolor(color=current, parent=win)
        if color:
            if section not in theme_cfg:
                theme_cfg.add_section(section)
            theme_cfg[section][option] = color
            set_theme_value(section, option, color)
            button.config(background=color)
            if callable(on_theme_change):
                on_theme_change()

    def _create_theme_button(label_text, section, option, default):
        frame = tk.Frame(inner, background=win_bg)
        frame.pack(fill="x", pady=2)

        tk.Label(
            frame,
            text=label_text,
            background=label_bg,
            foreground=label_fg,
            font=("Noto", 10),
            width=26,
            anchor="w",
        ).pack(side="left", padx=(4, 4))

        value = theme_cfg.get(section, option, fallback=default)
        btn = tk.Button(
            frame,
            text="Change",
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
        theme_cfg.read(str(THEME_PATH))

    def _restore_defaults():
        restore_theme_defaults()
        theme_cfg.read(str(THEME_PATH))

    _create_theme_button("Root background", "root_background", "background", "#1A1A1A")
    _create_theme_button("Frame header background", "frame_header", "background", "#101010")
    _create_theme_button("Frame main background", "frame_main", "background", "#252525")
    _create_theme_button("Frame playlist background", "frame_playlist", "background", "#252525")

    _create_theme_button("Label default background", "label_default", "background", "#252525")
    _create_theme_button("Label default foreground", "label_default", "foreground", "#EDEDED")

    _create_theme_button("Label playlist background", "label_playlist", "background", "#2f2f2f")
    _create_theme_button("Label playlist foreground", "label_playlist", "foreground", "#EDEDED")

    _create_theme_button("Playlist name background", "label_playlist_name", "background", "#2f2f2f")
    _create_theme_button("Playlist name foreground", "label_playlist_name", "foreground", "#DCDCDC")

    _create_theme_button("Playlist log background", "label_playlist_log", "background", "#2f2f2f")
    _create_theme_button("Playlist log foreground", "label_playlist_log", "foreground", "#EDEDED")

    _create_theme_button("Playlist good background", "label_playlist_good", "background", "#00C600")
    _create_theme_button("Playlist good foreground", "label_playlist_good", "foreground", "#EDEDED")

    _create_theme_button("Playlist warning background", "label_playlist_warning", "background", "#C68100")
    _create_theme_button("Playlist warning foreground", "label_playlist_warning", "foreground", "#EDEDED")

    _create_theme_button("Playlist error background", "label_playlist_error", "background", "#C60000")
    _create_theme_button("Playlist error foreground", "label_playlist_error", "foreground", "#EDEDED")

    _create_theme_button("Checkbutton background", "checkbutton", "background", "#303030")
    _create_theme_button("Checkbutton foreground", "checkbutton", "foreground", "#DADADA")
    _create_theme_button("Checkbutton selectcolor", "checkbutton", "selectcolor", "#505050")
    _create_theme_button("Checkbutton activebackground", "checkbutton", "activebackground", "#404040")
    _create_theme_button("Checkbutton activeforeground", "checkbutton", "activeforeground", "#FFFFFF")

    _create_theme_button("Button header background", "button_header", "background", "#6C6C6C")
    _create_theme_button("Button header foreground", "button_header", "foreground", "#FFFFFF")
    _create_theme_button("Button header activebackground", "button_header", "activebackground", "#868686")

    _create_theme_button("Button main background", "button_main", "background", "#3A3A3A")
    _create_theme_button("Button main foreground", "button_main", "foreground", "#D7D7D7")
    _create_theme_button("Button main activebackground", "button_main", "activebackground", "#555555")
    _create_theme_button("Button main activeforeground", "button_main", "activeforeground", "#FFFFFF")

    _create_theme_button("Button playlist background", "button_playlist", "background", "#3A3A3A")
    _create_theme_button("Button playlist foreground", "button_playlist", "foreground", "#D7D7D7")
    _create_theme_button("Button playlist activebackground", "button_playlist", "activebackground", "#555555")
    _create_theme_button("Button playlist activeforeground", "button_playlist", "activeforeground", "#FFFFFF")

    _create_theme_button("Button close background", "button_close", "background", "#160000")
    _create_theme_button("Button close foreground", "button_close", "foreground", "#FFFFFF")
    _create_theme_button("Button close activebackground", "button_close", "activebackground", "#390000")
    _create_theme_button("Button close activeforeground", "button_close", "activeforeground", "#FFFFFF")

    _create_theme_button("Entry default background", "entry_default", "background", "#404040")
    _create_theme_button("Entry default foreground", "entry_default", "foreground", "#FFFFFF")
    _create_theme_button("Entry default readonlybackground", "entry_default", "readonlybackground", "#2A2A2A")
    _create_theme_button("Entry default readonlyforeground", "entry_default", "readonlyforeground", "#AEAEAE")

    _create_theme_button("Entry playlist background", "entry_playlist", "background", "#404040")
    _create_theme_button("Entry playlist foreground", "entry_playlist", "foreground", "#FFFFFF")
    _create_theme_button("Entry playlist readonlybackground", "entry_playlist", "readonlybackground", "#2A2A2A")
    _create_theme_button("Entry playlist readonlyforeground", "entry_playlist", "readonlyforeground", "#AEAEAE")

    button_frame = tk.Frame(inner, background=win_bg)
    button_frame.pack(fill="x", pady=6)

    def _fire_change():
        if callable(on_theme_change):
            on_theme_change()

    tk.Button(
        button_frame,
        text="White Theme",
        command=lambda: (_apply_preset("white"), _fire_change()),
        background="#EDEDED",
        activebackground="#D2D2D2",
        foreground="#1A1A1A",
        bd=0,
    ).pack(side="left", expand=True, fill="x", padx=2)

    tk.Button(
        button_frame,
        text="Dark Theme",
        command=lambda: (_apply_preset("dark"), _fire_change()),
        background="#101010",
        activebackground="#555555",
        foreground="#EDEDED",
        bd=0,
    ).pack(side="left", expand=True, fill="x", padx=2)

    tk.Button(
        button_frame,
        text="Restore Defaults",
        command=lambda: (_restore_defaults(), _fire_change()),
        background=button_bg,
        activebackground=button_abg,
        foreground=button_fg,
        bd=0,
    ).pack(side="left", expand=True, fill="x", padx=2)

    for child in inner.winfo_children():
        child.bind("<MouseWheel>", _on_mousewheel)
        child.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        child.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))
        for grandchild in child.winfo_children():
            grandchild.bind("<MouseWheel>", _on_mousewheel)
            grandchild.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
            grandchild.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

    center_window(win)
