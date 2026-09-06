"""
Profile management dialogs: create / rename / edit-buckets.

Opens as a separate scrollable ``Toplevel`` (matching
``settings_theme_ui.py`` grab-handling: steal the grab, restore the
parent dialog's grab on close).

The same window shape is reused for:

* Create -- a Name entry plus three bucket checkboxes.
* Rename -- a Name entry only.
* Edit buckets -- three bucket checkboxes only.
* Delete -- confirmation handled by the caller via ``messagebox``.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox

from services import profile_store
from ui.scrollable import ScrollableFrame
from utils.scaling import px, ui_font
from utils.theme import C, btn_colors
from utils.window import center_window

logger = __import__("logging").getLogger(__name__)


def _on_close(win, parent):  # pragma: no cover - tkinter-only close handler
    """Restore the parent dialog's grab when this Toplevel closes."""
    win.destroy()
    if parent.winfo_exists():
        parent.grab_set()


def _ok_button(parent, text, command, **extra):
    return tk.Button(
        parent,
        text=text,
        cursor="hand2",
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        font=ui_font(11),
        highlightthickness=0,
        relief="raised",
        bd=0,
        command=command,
        **extra,
    )


def show_create_profile_dialog(parent, on_created=None):
    """Create a new profile.

    *parent* is the dialog to restore the grab on close.  *on_created* is
    an optional ``callable(name)`` invoked with the created profile's name
    after a successful save; the caller decides whether to activate it
    (and usually prompts to restart first).
    """
    win = tk.Toplevel(parent)
    win.title("Add profile")
    win.configure(background=C["frame_main_bg"])
    win.transient(parent)
    win.grab_set()
    win.minsize(px(360), px(280))
    win.protocol("WM_DELETE_WINDOW", lambda: _on_close(win, parent))

    header = tk.Frame(win, background=C["frame_head_bg"])
    header.pack(fill="x")
    tk.Label(
        header,
        text="Add profile",
        background=C["frame_head_bg"],
        foreground=C["label_def_fg"],
        font=ui_font(12),
    ).pack()

    content = tk.Frame(win, background=C["frame_main_bg"], padx=16, pady=12)
    content.pack(fill="both", expand=True)

    tk.Label(
        content,
        text="Profile name",
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
    name_entry.pack(fill="x", pady=(2, 8))
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

    tk.Label(
        content,
        text="Capture (what this profile keeps separate):",
        background=C["frame_main_bg"],
        foreground=C["label_def_fg"],
        font=ui_font(10),
        anchor="w",
    ).pack(fill="x", pady=(8, 2))

    var_logins = tk.BooleanVar(value=True)
    var_playlists = tk.BooleanVar(value=True)
    var_settings = tk.BooleanVar(value=True)

    checkbutton_style = {
        **btn_colors(C["checkbutton_bg"], C["checkbutton_fg"]),
        "highlightthickness": 0,
        "highlightbackground": C["checkbutton_bg"],
        "highlightcolor": C["checkbutton_bg"],
        "takefocus": False,
    }

    def _add_check(text, var):
        tk.Checkbutton(
            content,
            text=text,
            variable=var,
            cursor="hand2",
            selectcolor=C["checkbutton_selector"],
            **checkbutton_style,
            font=ui_font(11),
        ).pack(anchor="w", padx=8, pady=1)

    _add_check("Logins (login credentials)", var_logins)
    _add_check("Playlists (playlists.json, extra.json, databases)", var_playlists)
    _add_check("Settings (settings.ini, theme.ini)", var_settings)

    def _save():
        name = name_entry.get().strip()
        try:
            profile_store.create(
                name,
                logins=bool(var_logins.get()),
                playlists=bool(var_playlists.get()),
                settings=bool(var_settings.get()),
            )
        except ValueError as e:
            error_label.config(text=str(e))
            return
        _on_close(win, parent)
        if callable(on_created):
            on_created(name)

    btn_row = tk.Frame(content, background=C["frame_main_bg"])
    btn_row.pack(fill="x", pady=(14, 0))
    _ok_button(btn_row, "Save", _save).pack(side="left", expand=True, fill="x", padx=(0, 4))
    _ok_button(btn_row, "Cancel", lambda: _on_close(win, parent)).pack(
        side="left", expand=True, fill="x", padx=(4, 0)
    )

    center_window(win)


def show_rename_profile_dialog(parent, profile_name, on_restart=None):
    """Rename *profile_name*.

    The ``default`` profile cannot be renamed.
    """
    if profile_name == "default":
        messagebox.showinfo(
            "Rename profile",
            "The default profile cannot be renamed.",
            parent=parent,
        )
        return

    def _save(new_name):
        try:
            profile_store.rename(profile_name, new_name)
        except ValueError as e:
            return str(e)
        return None

    _show_name_dialog(
        parent,
        "Rename profile",
        "New name",
        profile_name,
        _save,
        on_restart,
    )


def show_edit_buckets_dialog(parent, profile_name, on_restart=None):
    """Edit the bucket capture flags for *profile_name*."""
    win = tk.Toplevel(parent)
    win.title(f"Edit profile: {profile_name}")
    win.configure(background=C["frame_main_bg"])
    win.transient(parent)
    win.grab_set()
    win.minsize(px(320), px(240))
    win.protocol("WM_DELETE_WINDOW", lambda: _on_close(win, parent))

    header = tk.Frame(win, background=C["frame_head_bg"])
    header.pack(fill="x")
    tk.Label(
        header,
        text=f"Edit profile: {profile_name}",
        background=C["frame_head_bg"],
        foreground=C["label_def_fg"],
        font=ui_font(12),
    ).pack()

    content = tk.Frame(win, background=C["frame_main_bg"], padx=16, pady=12)
    content.pack(fill="both", expand=True)

    tk.Label(
        content,
        text="What does this profile keep separate?",
        background=C["frame_main_bg"],
        foreground=C["label_def_fg"],
        font=ui_font(10),
        anchor="w",
    ).pack(fill="x", pady=(0, 4))

    var_logins = tk.BooleanVar(value=profile_store.get_bucket(profile_name, "logins"))
    var_playlists = tk.BooleanVar(value=profile_store.get_bucket(profile_name, "playlists"))
    var_settings = tk.BooleanVar(value=profile_store.get_bucket(profile_name, "settings"))

    checkbutton_style = {
        **btn_colors(C["checkbutton_bg"], C["checkbutton_fg"]),
        "highlightthickness": 0,
        "highlightbackground": C["checkbutton_bg"],
        "highlightcolor": C["checkbutton_bg"],
        "takefocus": False,
    }

    def _add_check(text, var):
        tk.Checkbutton(
            content,
            text=text,
            variable=var,
            cursor="hand2",
            selectcolor=C["checkbutton_selector"],
            **checkbutton_style,
            font=ui_font(11),
        ).pack(anchor="w", padx=8, pady=2)

    _add_check("Logins (login credentials)", var_logins)
    _add_check("Playlists (playlists.json, extra.json, databases)", var_playlists)
    _add_check("Settings (settings.ini, theme.ini)", var_settings)

    tk.Label(
        content,
        text="Changes take effect after the app restarts.",
        background=C["frame_main_bg"],
        foreground=C["label_playlist_warn_fg"],
        font=ui_font(9),
        anchor="w",
    ).pack(fill="x", pady=(6, 0))

    def _save():
        profile_store.set_bucket(profile_name, "logins", bool(var_logins.get()))
        profile_store.set_bucket(profile_name, "playlists", bool(var_playlists.get()))
        profile_store.set_bucket(profile_name, "settings", bool(var_settings.get()))
        _on_close(win, parent)
        if callable(on_restart):
            on_restart()

    btn_row = tk.Frame(content, background=C["frame_main_bg"])
    btn_row.pack(fill="x", pady=(14, 0))
    _ok_button(btn_row, "Save", _save).pack(side="left", expand=True, fill="x", padx=(0, 4))
    _ok_button(btn_row, "Cancel", lambda: _on_close(win, parent)).pack(
        side="left", expand=True, fill="x", padx=(4, 0)
    )

    center_window(win)


def _show_name_dialog(parent, title, label, initial, save_fn, on_restart):
    """Shared single-entry dialog (create-style, name only).

    *save_fn* takes the new name and returns ``None`` on success or an
    error string on failure.
    """
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(background=C["frame_main_bg"])
    win.transient(parent)
    win.grab_set()
    win.minsize(px(320), px(160))
    win.protocol("WM_DELETE_WINDOW", lambda: _on_close(win, parent))

    header = tk.Frame(win, background=C["frame_head_bg"])
    header.pack(fill="x")
    tk.Label(
        header,
        text=title,
        background=C["frame_head_bg"],
        foreground=C["label_def_fg"],
        font=ui_font(12),
    ).pack()

    content = tk.Frame(win, background=C["frame_main_bg"], padx=16, pady=12)
    content.pack(fill="both", expand=True)

    tk.Label(
        content,
        text=label,
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
    name_entry.insert(0, initial)
    name_entry.pack(fill="x", pady=(2, 2))
    name_entry.focus_set()
    name_entry.select_range(0, "end")

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
        new_name = name_entry.get().strip()
        err = save_fn(new_name)
        if err:
            error_label.config(text=err)
            return
        _on_close(win, parent)
        if callable(on_restart):
            on_restart()

    btn_row = tk.Frame(content, background=C["frame_main_bg"])
    btn_row.pack(fill="x", pady=(12, 0))
    _ok_button(btn_row, "Save", _save).pack(side="left", expand=True, fill="x", padx=(0, 4))
    _ok_button(btn_row, "Cancel", lambda: _on_close(win, parent)).pack(
        side="left", expand=True, fill="x", padx=(4, 0)
    )

    center_window(win)
