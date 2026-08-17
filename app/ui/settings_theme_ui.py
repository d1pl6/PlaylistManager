import tkinter as tk
from tkinter import ttk, colorchooser
from configparser import ConfigParser

from utils.config import (
    ensure_theme_file,
    set_theme_value,
    apply_theme_preset,
    restore_theme_defaults,
    THEME_PATH,
)
from utils.scaling import px, ui_font
from utils.theme import C, readable_fg, btn_colors
from utils.window import center_window


def show_theme_dialog(parent, on_theme_change=None):
    """Open a separate scrollable theme-settings window."""

    win_bg = C["frame_main_bg"]
    header_bg = C["frame_head_bg"]
    label_bg = C["label_def_bg"]
    label_fg = C["label_def_fg"]
    button_bg = C["button_main_bg"]
    button_fg = C["button_main_fg"]
    button_btn = btn_colors(button_bg, button_fg)

    win = tk.Toplevel(parent)
    win.title("Theme Settings")
    win.configure(background=win_bg, padx=2, pady=2)
    win.transient(parent)
    win.grab_set()
    win.minsize(px(350), px(400))

    # Restore the Settings dialog's grab when this Toplevel closes - the
    # theme picker steals the grab at open, and destroying it otherwise
    # releases the grab globally, leaving Settings non-modal.
    def _on_close() -> None:
        win.destroy()
        if parent.winfo_exists():
            parent.grab_set()

    win.protocol("WM_DELETE_WINDOW", _on_close)

    tk.Label(
        win,
        text="Theme settings",
        background=header_bg,
        foreground=label_fg,
        font=ui_font(12),
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

    def _choose_color(section, option, default, button):
        # theme_cfg is loaded after ensure_theme_file(), so every
        # DEFAULT_THEME option is guaranteed present - the fallback only
        # guards against a hand-edited file.
        current = theme_cfg.get(section, option, fallback=default)
        _, color = colorchooser.askcolor(color=current, parent=win)
        if color:
            if section not in theme_cfg:
                theme_cfg.add_section(section)
            theme_cfg[section][option] = color
            set_theme_value(section, option, color)
            # Re-style the whole swatch (bg + readable fg, incl. the hover
            # state) - config(background=...) alone leaves the creation-time
            # activebackground/activeforeground behind, so hovering showed
            # the old color / black text after a pick.
            _style_button(button, color)
            if callable(on_theme_change):
                on_theme_change()

    color_buttons: list = []

    def _style_button(btn, value) -> None:
        """Apply a color to a swatch button (bg + readable fg, hover included)."""
        fg = readable_fg(value)
        btn.config(**btn_colors(value, fg))

    def _create_theme_button(label_text, section, option, default):
        frame = tk.Frame(inner, background=win_bg)
        frame.pack(fill="x", pady=2)

        tk.Label(
            frame,
            text=label_text,
            background=label_bg,
            foreground=label_fg,
            font=ui_font(10),
            width=26,
            anchor="w",
        ).pack(side="left", padx=(4, 4))

        value = theme_cfg.get(section, option, fallback=default)
        btn = tk.Button(
            frame,
            text="Change",
            cursor="hand2",
            font=ui_font(10),
            command=lambda: _choose_color(section, option, default, btn),
            highlightthickness=0,
            relief="raised",
            bd=0,
        )
        _style_button(btn, value)
        btn.pack(side="right", padx=4)
        color_buttons.append((section, option, default, btn))
        return btn

    def _apply_preset(preset):
        apply_theme_preset(preset)
        theme_cfg.read(str(THEME_PATH))
        _refresh_buttons()

    def _restore_defaults():
        restore_theme_defaults()
        theme_cfg.read(str(THEME_PATH))
        _refresh_buttons()

    _restore_btn = None  # populated below; referenced by _refresh_buttons

    def _refresh_buttons() -> None:
        """Sync the swatch buttons after a preset or defaults restore."""
        for section, option, default, btn in color_buttons:
            _style_button(btn, theme_cfg.get(section, option, fallback=default))
        # Re-theme footer buttons whose colours were captured from C at
        # creation time and would otherwise lag behind a preset change.
        if _restore_btn is not None:
            _restore_btn.configure(
                **btn_colors(C["button_main_bg"], C["button_main_fg"])
            )

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

    _create_theme_button("Button header background", "button_header", "background", "#6C6C6C")
    _create_theme_button("Button header foreground", "button_header", "foreground", "#FFFFFF")

    _create_theme_button("Button main background", "button_main", "background", "#3A3A3A")
    _create_theme_button("Button main foreground", "button_main", "foreground", "#D7D7D7")

    _create_theme_button("Button playlist background", "button_playlist", "background", "#3A3A3A")
    _create_theme_button("Button playlist foreground", "button_playlist", "foreground", "#D7D7D7")

    _create_theme_button("Button close background", "button_close", "background", "#160000")
    _create_theme_button("Button close foreground", "button_close", "foreground", "#FFFFFF")

    _create_theme_button("Button save background", "button_save", "background", "#004304")
    _create_theme_button("Button save foreground", "button_save", "foreground", "#D7D7D7")

    _create_theme_button("Entry default background", "entry_default", "background", "#404040")
    _create_theme_button("Entry default foreground", "entry_default", "foreground", "#FFFFFF")
    _create_theme_button("Entry default readonlybackground", "entry_default", "readonlybackground", "#2A2A2A")

    _create_theme_button("Entry playlist background", "entry_playlist", "background", "#404040")
    _create_theme_button("Entry playlist foreground", "entry_playlist", "foreground", "#FFFFFF")
    _create_theme_button("Entry playlist readonlybackground", "entry_playlist", "readonlybackground", "#2A2A2A")

    button_frame = tk.Frame(inner, background=win_bg)
    button_frame.pack(fill="x", pady=6)

    def _fire_change():
        if callable(on_theme_change):
            on_theme_change()

    tk.Button(
        button_frame,
        text="White Theme",
        cursor="hand2",
        font=ui_font(10),
        command=lambda: (_apply_preset("white"), _fire_change()),
        **btn_colors("#EDEDED", "#1A1A1A"),
        highlightthickness=0,
        relief="raised",
        bd=0,
    ).pack(side="left", expand=True, fill="x", padx=2)

    _restore_btn = tk.Button(
        button_frame,
        text="Restore Defaults",
        cursor="hand2",
        font=ui_font(10),
        command=lambda: (_restore_defaults(), _fire_change()),
        **button_btn,
        highlightthickness=0,
        relief="raised",
        bd=0,
    )
    _restore_btn.pack(side="left", expand=True, fill="x", padx=2)

    for child in inner.winfo_children():
        child.bind("<MouseWheel>", _on_mousewheel)
        child.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        child.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))
        for grandchild in child.winfo_children():
            grandchild.bind("<MouseWheel>", _on_mousewheel)
            grandchild.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
            grandchild.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

    # Also scroll when the wheel is over the bare canvas (a short theme
    # doesn't cover the full scroll area) - mirrors playlist_dialog.py.
    canvas.bind("<MouseWheel>", _on_mousewheel)
    canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
    canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

    center_window(win)
