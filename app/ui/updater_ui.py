import tkinter as tk
import webbrowser
from tkinter import messagebox

from _version import __version__
from utils.scaling import ui_font
from utils.window import center_window
from utils.theme import C, btn_colors


def show_update_dialog(parent, latest_version, download_url, body):
    bg = C["frame_main_bg"]
    fg = C["label_def_fg"]

    win = tk.Toplevel(parent)
    win.title("Update Available")
    win.configure(background=bg)
    win.resizable(True, True)
    win.transient(parent)
    win.grab_set()
    win.maxsize(3333, 3333)

    # Restore the parent's grab when this Toplevel closes - the dialog
    # steals the grab at open, and destroying it otherwise releases the
    # grab globally, leaving the parent non-modal.
    def _on_close() -> None:
        win.destroy()
        if parent.winfo_exists():
            parent.grab_set()

    win.protocol("WM_DELETE_WINDOW", _on_close)

    frame = tk.Frame(win, background=bg, padx=20, pady=20)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text=f"PlaylistManager v{latest_version} is available!",
        font=ui_font(14, "bold"),
        background=bg,
        foreground=fg,
    ).pack(anchor="w")

    tk.Label(
        frame,
        text=f"Current version: v{__version__}",
        font=ui_font(10),
        background=bg,
        foreground=fg,
    ).pack(anchor="w", pady=(0, 10))

    if body:
        text_frame = tk.Frame(frame, background=bg)
        text_frame.pack(fill="both", expand=True, pady=(0, 10))
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)

        text_widget = tk.Text(
            text_frame,
            wrap="word",
            font=ui_font(10),
            background=bg,
            foreground=fg,
            relief="flat",
            borderwidth=0,
            height=10,
            width=60,
        )
        text_widget.insert("1.0", body.strip())
        text_widget.configure(state="disabled", cursor="hand2")
        text_widget.grid(row=0, column=0, sticky="nsew")

        scrollbar = tk.Scrollbar(text_frame, command=text_widget.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text_widget.configure(yscrollcommand=scrollbar.set)

    btn_frame = tk.Frame(frame, background=bg)
    btn_frame.pack(fill="x")

    def _download():
        try:
            webbrowser.open(download_url)
        except Exception:
            messagebox.showerror(
                "PlaylistManager",
                "Could not open a browser. Download manually from:\n"
                + download_url,
                parent=win,
            )
            return
        win.destroy()

    tk.Button(
        btn_frame,
        text="Download",
        font=ui_font(10),
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        cursor="hand2",
        padx=16,
        highlightthickness=0,
        bd=0,
        relief="raised",
        command=_download,
    ).pack(side="right", padx=(8, 0))

    tk.Button(
        btn_frame,
        text="Close",
        font=ui_font(10),
        **btn_colors(C["button_head_bg"], C["button_head_fg"]),
        cursor="hand2",
        padx=16,
        highlightthickness=0,
        bd=0,
        relief="raised",
        command=_on_close,
    ).pack(side="right")

    center_window(win)
