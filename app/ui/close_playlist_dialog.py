"""Close-playlist confirmation dialog (Cancel / Keep DB / Confirm).

A modal Toplevel with three outcomes:

- **Cancel** - nothing happens (also Escape / window close).
- **Keep DB** - the card closes but the local SQLite database file is
  kept, so re-adding the playlist later reuses the cached songs.
- **Confirm** - the card closes and the database file is deleted (the
  previous unconditional behaviour).

Callback-based like the platform picker: every exit path invokes exactly
one callback, and the dialog destroys its window *first* (same
destroy-then-callback order as ``playlist_dialog._on_playlist_click``).
Colors come from the live palette at creation time, matching the other
dialogs (``settings_ui`` pattern) - a theme change before the next open
uses the new palette.
"""

import tkinter as tk

from utils.scaling import px, ui_font
from utils.theme import C, btn_colors
from utils.window import center_window


def show_close_playlist_dialog(
    parent,
    playlist_name,
    on_cancel=None,
    on_keep_db=None,
    on_confirm=None,
) -> None:
    """Ask how to close ``playlist_name``'s card.

    The dialog owns its window: it destroys itself on every exit path and
    then invokes the matching callback (when given).  ``on_cancel`` also
    fires on Escape and on the window-manager close button.
    """
    win_bg = C["frame_main_bg"]
    label_fg = C["label_def_fg"]

    win = tk.Toplevel(parent)
    win.withdraw()
    win.title("Close Playlist")
    win.configure(background=win_bg, padx=px(12), pady=px(10))
    win.transient(parent)

    def _exit(callback) -> None:
        win.destroy()
        if callback is not None:
            callback()

    tk.Label(
        win,
        text=f'Close playlist "{playlist_name}"?',
        background=win_bg,
        foreground=label_fg,
        font=ui_font(11),
        justify="left",
    ).pack(anchor="w", pady=(0, px(2)))

    tk.Label(
        win,
        text="Keep DB keeps the local song cache; Confirm deletes "
        "the playlist and its database.",
        background=win_bg,
        foreground=label_fg,
        font=ui_font(9),
        justify="left",
        wraplength=px(320),
    ).pack(anchor="w", pady=(0, px(10)))

    btn_row = tk.Frame(win, background=win_bg)
    btn_row.pack(anchor="center")

    cancel_btn = tk.Button(
        btn_row,
        text="Cancel",
        cursor="hand2",
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        font=ui_font(10),
        width=10,
        highlightthickness=0,
        relief="raised",
        command=lambda: _exit(on_cancel),
    )
    cancel_btn.pack(side="left", padx=(0, px(6)))

    tk.Button(
        btn_row,
        text="Keep DB",
        cursor="hand2",
        **btn_colors(C["button_save_bg"], C["button_save_fg"]),
        font=ui_font(10),
        width=10,
        highlightthickness=0,
        relief="raised",
        command=lambda: _exit(on_keep_db),
    ).pack(side="left", padx=(0, px(6)))

    tk.Button(
        btn_row,
        text="Confirm",
        cursor="hand2",
        **btn_colors(C["button_close_bg"], C["button_close_fg"]),
        font=ui_font(10),
        width=10,
        highlightthickness=0,
        relief="raised",
        command=lambda: _exit(on_confirm),
    ).pack(side="left")

    # Every exit path is Cancel-by-default: Escape, WM close, and
    # Enter (initial focus lands on Cancel).
    win.bind("<Escape>", lambda _e: _exit(on_cancel))
    win.protocol("WM_DELETE_WINDOW", lambda: _exit(on_cancel))

    center_window(win)
    win.deiconify()
    win.grab_set()
    cancel_btn.focus_set()
