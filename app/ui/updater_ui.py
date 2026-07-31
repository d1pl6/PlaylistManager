import tkinter as tk
import webbrowser

from _version import __version__
from utils.window import center_window
from utils.theme import C


def show_update_dialog(parent, latest_version, download_url, body):
    theme_win_bg = C["frame_main_bg"]
    theme_frame_bg = C["frame_main_bg"]
    theme_title_fg = C["label_def_fg"]
    theme_subtitle_fg = C["label_def_fg"]
    theme_text_bg = C["frame_main_bg"]
    theme_text_fg = C["label_def_fg"]
    theme_download_bg = C["button_main_bg"]
    theme_download_abg = C["button_main_a_bg"]
    theme_download_fg = C["button_main_fg"]
    theme_close_bg = C["button_head_bg"]
    theme_close_abg = C["button_head_a_bg"]
    theme_close_fg = C["button_head_fg"]

    win = tk.Toplevel(parent)
    win.title("Update Available")
    win.configure(background=theme_win_bg)
    win.resizable(True, True)
    win.transient(parent)
    win.grab_set()
    win.maxsize(3333, 3333)

    frame = tk.Frame(win, background=theme_frame_bg, padx=20, pady=20)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text=f"PlaylistManager v{latest_version} is available!",
        font=("Noto", 14, "bold"),
        background=theme_frame_bg,
        foreground=theme_title_fg,
    ).pack(anchor="w")

    tk.Label(
        frame,
        text=f"Current version: v{__version__}",
        font=("Noto", 10),
        background=theme_frame_bg,
        foreground=theme_subtitle_fg,
    ).pack(anchor="w", pady=(0, 10))

    if body:
        text_frame = tk.Frame(frame, background=theme_text_bg)
        text_frame.pack(fill="both", expand=True, pady=(0, 10))
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)

        text_widget = tk.Text(
            text_frame,
            wrap="word",
            font=("Noto", 10),
            background=theme_text_bg,
            foreground=theme_text_fg,
            relief="flat",
            borderwidth=0,
            height=10,
            width=60,
        )
        text_widget.insert("1.0", body.strip())
        text_widget.configure(state="disabled")
        text_widget.grid(row=0, column=0, sticky="nsew")

        scrollbar = tk.Scrollbar(text_frame, command=text_widget.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text_widget.configure(yscrollcommand=scrollbar.set)

    btn_frame = tk.Frame(frame, background=theme_frame_bg)
    btn_frame.pack(fill="x")

    tk.Button(
        btn_frame,
        text="Download",
        font=("Noto", 10),
        background=theme_download_bg,
        activebackground=theme_download_abg,
        foreground=theme_download_fg,
        activeforeground=theme_download_fg,
        relief="flat",
        cursor="hand2",
        padx=16,
        command=lambda: (webbrowser.open(download_url), win.destroy()),
    ).pack(side="right", padx=(8, 0))

    tk.Button(
        btn_frame,
        text="Close",
        font=("Noto", 10),
        background=theme_close_bg,
        activebackground=theme_close_abg,
        foreground=theme_close_fg,
        activeforeground=theme_close_fg,
        relief="flat",
        cursor="hand2",
        padx=16,
        command=win.destroy,
    ).pack(side="right")

    center_window(win)
