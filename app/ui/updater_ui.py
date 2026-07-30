import tkinter as tk
import webbrowser

from _version import __version__
from utils.window import center_window
from utils.config import get_theme_value


def _theme_value(section: str, option: str, default: str) -> str:
    try:
        return get_theme_value(section, option, default)
    except Exception:
        return default


def show_update_dialog(parent, latest_version, download_url, body):
    theme_win_bg = _theme_value("frame_main", "background", "#1A1A1A")
    theme_frame_bg = _theme_value("frame_main", "background", "#2A2A2A")
    theme_title_fg = _theme_value("label", "foreground", "white")
    theme_subtitle_fg = _theme_value("label", "foreground", "#AAAAAA")
    theme_text_bg = _theme_value("frame_main", "background", "#333333")
    theme_text_fg = _theme_value("label", "foreground", "#CCCCCC")
    theme_download_bg = _theme_value("button_main", "background", "#006713")
    theme_download_abg = _theme_value("button_main", "activebackground", "#008A1A")
    theme_download_fg = _theme_value("button_main", "foreground", "white")
    theme_close_bg = _theme_value("button_header", "background", "#444444")
    theme_close_abg = _theme_value("button_header", "activebackground", "#555555")
    theme_close_fg = _theme_value("button_header", "foreground", "white")

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
