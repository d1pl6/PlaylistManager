"""Theme picker dialog.

A scrollable Toplevel opening from the Settings dialog.  Shows one swatch
row per palette colour and, at the top, a Themes bar managing named user
themes (save / create / delete), mirroring the Profiles section's
combo-plus-buttons pattern.  Selecting a theme applies it live.
"""

import tkinter as tk
from tkinter import colorchooser, messagebox, ttk
from configparser import ConfigParser

from utils.config import (
    DEFAULT_THEME,
    THEME_PATH,
    THEMES_DIR,
    THEME_PRESETS,
    ensure_theme_file,
    set_theme_value,
    apply_theme_preset,
    restore_theme_defaults,
    list_themes,
    save_theme,
    apply_theme,
    delete_theme,
)
from ui.scrollable import ScrollableFrame
from utils.scaling import px, ui_font
from utils.theme import C, readable_fg, btn_colors
from utils.window import center_window, fit_window_to_screen


#: Built-in pseudo-themes shown in the Themes combo (not saved files).
BUILTIN_DEFAULT = "Default theme"
BUILTIN_WHITE = "White Theme"
_BUILTINS = (BUILTIN_DEFAULT, BUILTIN_WHITE)


def _ok_button(parent, text, command):
    return tk.Button(
        parent,
        text=text,
        cursor="hand2",
        font=ui_font(10),
        command=command,
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        highlightthickness=0,
        relief="raised",
        bd=0,
    )


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

    header_label = tk.Label(
        win,
        text="Theme settings",
        background=header_bg,
        foreground=label_fg,
        font=ui_font(12),
    )
    header_label.pack(fill="x")

    # --- Themes bar: combo + save/create/delete (mirrors the Profiles
    # section in Settings).  Packed into win (not the scrollable content)
    # so it stays visible while the swatch list scrolls.
    themes_bar = tk.Frame(win, background=win_bg)
    themes_bar.pack(fill="x", padx=8, pady=(4, 2))

    theme_label = tk.Label(
        themes_bar,
        text="Theme:",
        background=win_bg,
        foreground=label_fg,
        font=ui_font(10),
    )
    theme_label.pack(side="left", padx=(0, 6))

    theme_var = tk.StringVar(value=BUILTIN_DEFAULT)
    theme_combo = ttk.Combobox(
        themes_bar,
        textvariable=theme_var,
        state="readonly",
        width=18,
        font=ui_font(10),
    )
    theme_combo.pack(side="left")

    # --- Scrollable swatch area.
    sf = ScrollableFrame(win, bg=C["scrollable_frame_bg"], show_scrollbar=True,
                         bind_all_mousewheel=True)
    sf.pack(side="left", fill="both", expand=True)
    inner = sf.content

    ensure_theme_file()
    theme_cfg = ConfigParser()
    theme_cfg.read(str(THEME_PATH))

    def _reload_theme_cfg() -> None:
        """Re-read theme.ini into a fresh ConfigParser (replaces, not merges)."""
        nonlocal theme_cfg
        theme_cfg = ConfigParser()
        theme_cfg.read(str(THEME_PATH))

    def _flatten_config(cfg) -> dict:
        return {(s, o): cfg[s][o] for s in cfg.sections() for o in cfg[s]}

    def _preselected_theme() -> str:
        """Name of the combo entry matching the current palette, else the default."""
        current = _flatten_config(theme_cfg)
        for name in list_themes():
            p = ConfigParser()
            path = THEMES_DIR / f"{name}.ini"
            try:
                p.read(str(path))
            except OSError:
                continue
            if _flatten_config(p) == current:
                return name
        white_sections = THEME_PRESETS.get("white", {})
        if all(
            s in theme_cfg.sections() and dict(theme_cfg[s]) == opts
            for s, opts in white_sections.items()
        ):
            return BUILTIN_WHITE
        return BUILTIN_DEFAULT

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
    option_labels: list = []

    def _style_button(btn, value) -> None:
        """Apply a color to a swatch button (bg + readable fg, hover included)."""
        fg = readable_fg(value)
        btn.config(**btn_colors(value, fg))

    def _theme_option_default(section, option):
        """Look up the fallback default from DEFAULT_THEME."""
        return DEFAULT_THEME.get(section, {}).get(option, "#000000")

    def _create_theme_button(label_text, section, option):
        default = _theme_option_default(section, option)
        frame = tk.Frame(inner, background=win_bg)
        frame.pack(fill="x", pady=2)

        lbl = tk.Label(
            frame,
            text=label_text,
            background=label_bg,
            foreground=label_fg,
            font=ui_font(10),
            width=26,
            anchor="w",
        )
        lbl.pack(side="left", padx=(4, 4))
        option_labels.append(lbl)

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

    # ------------------------------------------------------------------
    # Themes bar actions
    # ------------------------------------------------------------------

    def _sync_theme_buttons() -> None:
        """Save/Delete act on saved user themes only (built-ins are read-only)."""
        is_user = theme_var.get() not in _BUILTINS
        btn_save.config(state="normal" if is_user else "disabled")
        btn_delete.config(state="normal" if is_user else "disabled")

    def _refresh_themes_combo(select=None) -> None:
        """Rebuild the combo entries; *select* forces the new selection."""
        theme_combo["values"] = list(_BUILTINS) + list_themes()
        current = theme_var.get()
        if select is not None:
            theme_var.set(select)
        elif current not in theme_combo["values"]:
            theme_var.set(BUILTIN_DEFAULT)
        _sync_theme_buttons()

    def _apply_selected_theme() -> None:
        """Apply the combo's selection (built-in or saved) to theme.ini."""
        sel = theme_var.get()
        if not sel:
            return
        try:
            if sel == BUILTIN_DEFAULT:
                restore_theme_defaults()
            elif sel == BUILTIN_WHITE:
                apply_theme_preset("white")
            else:
                apply_theme(sel)
        except ValueError as e:
            messagebox.showerror("Apply Theme", str(e), parent=win)
            _refresh_themes_combo()
            return
        _reload_theme_cfg()
        # Update the global palette before re-theming the dialog so
        # _refresh_all reads the new colours from C, not stale ones.
        if callable(on_theme_change):
            on_theme_change()
        _refresh_all()
        _sync_theme_buttons()

    theme_combo.bind("<<ComboboxSelected>>", lambda _e: _apply_selected_theme())

    def _on_save_theme() -> None:
        sel = theme_var.get()
        if sel in _BUILTINS:
            return
        confirm = messagebox.askyesno(
            "Save Theme",
            f'Overwrite the theme "{sel}" with the current palette?',
            parent=win,
        )
        if not confirm:
            return
        try:
            save_theme(sel)
        except ValueError as e:
            messagebox.showerror("Save Theme", str(e), parent=win)

    def _on_create_theme() -> None:
        def _do_create(name):
            try:
                save_theme(name)
            except ValueError as e:
                return str(e)
            _refresh_themes_combo(select=name)
            return None

        _show_theme_name_dialog("Create Theme", "Theme name:", _do_create)

    def _on_delete_theme() -> None:
        sel = theme_var.get()
        if sel in _BUILTINS:
            return
        confirm = messagebox.askyesno(
            "Delete Theme",
            f'Delete the theme "{sel}"? The current palette is not affected.',
            parent=win,
        )
        if not confirm:
            return
        try:
            delete_theme(sel)
        except ValueError as e:
            messagebox.showerror("Delete Theme", str(e), parent=win)
            return
        _refresh_themes_combo(select=BUILTIN_DEFAULT)

    btn_save = tk.Button(
        themes_bar,
        text="Save",
        cursor="hand2",
        font=ui_font(10),
        command=_on_save_theme,
        **button_btn,
        highlightthickness=0,
        relief="raised",
        bd=0,
    )
    btn_save.pack(side="left", padx=(8, 2))

    btn_create = tk.Button(
        themes_bar,
        text="Create",
        cursor="hand2",
        font=ui_font(10),
        command=_on_create_theme,
        **button_btn,
        highlightthickness=0,
        relief="raised",
        bd=0,
    )
    btn_create.pack(side="left", padx=2)

    btn_delete = tk.Button(
        themes_bar,
        text="Delete",
        cursor="hand2",
        font=ui_font(10),
        command=_on_delete_theme,
        **button_btn,
        highlightthickness=0,
        relief="raised",
        bd=0,
    )
    btn_delete.pack(side="left", padx=2)

    def _show_theme_name_dialog(title, label_text, save_fn) -> None:
        """Single-entry dialog (create-style, name only).

        *save_fn* takes the name and returns ``None`` on success or an
        error string on failure.
        """

        def _dlg_close() -> None:
            dlg.destroy()
            if win.winfo_exists():
                win.grab_set()

        dlg = tk.Toplevel(win)
        dlg.title(title)
        dlg.configure(background=C["frame_main_bg"])
        dlg.transient(win)
        dlg.grab_set()
        dlg.minsize(px(320), px(160))
        dlg.protocol("WM_DELETE_WINDOW", _dlg_close)

        header = tk.Frame(dlg, background=C["frame_head_bg"])
        header.pack(fill="x")
        tk.Label(
            header,
            text=title,
            background=C["frame_head_bg"],
            foreground=C["label_def_fg"],
            font=ui_font(12),
        ).pack()

        content = tk.Frame(dlg, background=C["frame_main_bg"], padx=16, pady=12)
        content.pack(fill="both", expand=True)

        tk.Label(
            content,
            text=label_text,
            background=C["frame_main_bg"],
            foreground=C["label_def_fg"],
            font=ui_font(10),
            anchor="w",
        ).pack(fill="x")

        name_entry = tk.Entry(
            content,
            font=ui_font(11),
            background=C["entry_default_bg"],
            foreground=C["entry_default_fg"],
            insertbackground=C["entry_default_fg"],
            relief="flat",
        )
        name_entry.pack(fill="x", pady=(2, 2))
        name_entry.focus_set()

        error_label = tk.Label(
            content,
            text="",
            background=C["frame_main_bg"],
            foreground=C["label_playlist_error_fg"],
            font=ui_font(9),
            anchor="w",
        )
        error_label.pack(fill="x")

        def _save():
            err = save_fn(name_entry.get().strip())
            if err:
                error_label.config(text=err)
                return
            _dlg_close()

        btn_row = tk.Frame(content, background=C["frame_main_bg"])
        btn_row.pack(fill="x", pady=(12, 0))
        _ok_button(btn_row, "Save", _save).pack(
            side="left", expand=True, fill="x", padx=(0, 4)
        )
        _ok_button(btn_row, "Cancel", _dlg_close).pack(
            side="left", expand=True, fill="x", padx=(4, 0)
        )

        center_window(dlg)

    # ------------------------------------------------------------------
    # Re-theme of the dialog chrome after a theme switch
    # ------------------------------------------------------------------

    def _refresh_all() -> None:
        """Sync swatch buttons and dialog chrome after a theme switch."""
        # Swatch buttons: re-read from the freshly loaded theme_cfg.
        for section, option, default, btn in color_buttons:
            _style_button(btn, theme_cfg.get(section, option, fallback=default))
        # Option labels: re-theme background/foreground from the new palette.
        for lbl in option_labels:
            lbl.configure(
                background=C["label_def_bg"], foreground=C["label_def_fg"],
            )
        # Dialog chrome: header, themes bar, canvas, root bg.
        header_label.configure(
            background=C["frame_head_bg"], foreground=C["label_def_fg"],
        )
        new_win_bg = C["frame_main_bg"]
        win.configure(background=new_win_bg)
        sf.canvas.configure(background=C["scrollable_frame_bg"])
        inner.configure(background=new_win_bg)
        themes_bar.configure(background=new_win_bg)
        theme_label.configure(
            background=new_win_bg, foreground=C["label_def_fg"],
        )
        for b in (btn_save, btn_create, btn_delete):
            b.configure(**btn_colors(C["button_main_bg"], C["button_main_fg"]))

    # Initial combo selection: match the current palette where possible.
    theme_combo["values"] = list(_BUILTINS) + list_themes()
    theme_var.set(_preselected_theme())

    _create_theme_button("Root background", "root_background", "background")
    _create_theme_button("Frame header background", "frame_header", "background")
    _create_theme_button("Frame main background", "frame_main", "background")
    _create_theme_button("Frame playlist background", "frame_playlist", "background")

    _create_theme_button("Scrollable frame background", "scrollable_frame", "background")

    _create_theme_button("Label default background", "label_default", "background")
    _create_theme_button("Label default foreground", "label_default", "foreground")

    _create_theme_button("Label playlist background", "label_playlist", "background")
    _create_theme_button("Label playlist foreground", "label_playlist", "foreground")

    _create_theme_button("Playlist name background", "label_playlist_name", "background")
    _create_theme_button("Playlist name foreground", "label_playlist_name", "foreground")

    _create_theme_button("Playlist log background", "label_playlist_log", "background")
    _create_theme_button("Playlist log foreground", "label_playlist_log", "foreground")

    _create_theme_button("Playlist good background", "label_playlist_good", "background")
    _create_theme_button("Playlist good foreground", "label_playlist_good", "foreground")

    _create_theme_button("Playlist warning background", "label_playlist_warning", "background")
    _create_theme_button("Playlist warning foreground", "label_playlist_warning", "foreground")

    _create_theme_button("Playlist error background", "label_playlist_error", "background")
    _create_theme_button("Playlist error foreground", "label_playlist_error", "foreground")

    _create_theme_button("Checkbutton background", "checkbutton", "background")
    _create_theme_button("Checkbutton foreground", "checkbutton", "foreground")
    _create_theme_button("Checkbutton selectcolor", "checkbutton", "selectcolor")

    _create_theme_button("Button header background", "button_header", "background")
    _create_theme_button("Button header foreground", "button_header", "foreground")

    _create_theme_button("Button main background", "button_main", "background")
    _create_theme_button("Button main foreground", "button_main", "foreground")

    _create_theme_button("Button playlist background", "button_playlist", "background")
    _create_theme_button("Button playlist foreground", "button_playlist", "foreground")

    _create_theme_button("Button close background", "button_close", "background")
    _create_theme_button("Button close foreground", "button_close", "foreground")

    _create_theme_button("Button save background", "button_save", "background")
    _create_theme_button("Button save foreground", "button_save", "foreground")

    _create_theme_button("Entry default background", "entry_default", "background")
    _create_theme_button("Entry default foreground", "entry_default", "foreground")
    _create_theme_button("Entry default readonlybackground", "entry_default", "readonlybackground")

    _create_theme_button("Entry playlist background", "entry_playlist", "background")
    _create_theme_button("Entry playlist foreground", "entry_playlist", "foreground")
    _create_theme_button("Entry playlist readonlybackground", "entry_playlist", "readonlybackground")

    _create_theme_button("Playlist stats background", "label_playlist_stats", "background")
    _create_theme_button("Playlist stats foreground", "label_playlist_stats", "foreground")

    _create_theme_button("Search bar background", "search_bar", "background")
    _create_theme_button("Search bar foreground", "search_bar", "foreground")

    _create_theme_button("Search result background", "search_result", "background")
    _create_theme_button("Search result foreground", "search_result", "foreground")

    _sync_theme_buttons()
    # Cap the window at the screen size so the swatch list scrolls instead
    # of opening taller than the display with its bottom rows unreachable.
    sf.update_scrollregion()
    fit_window_to_screen(win, margin=60)
    center_window(win)