import tkinter as tk
import webbrowser

from _version import __version__
from utils import center_window


def show_update_dialog(parent, latest_version, download_url, body):
    win = tk.Toplevel(parent)
    win.title("Update Available")
    win.configure(background="#1A1A1A")
    win.resizable(True, True)
    win.transient(parent)
    win.grab_set()
    win.maxsize(3333, 3333)

    frame = tk.Frame(win, background="#2A2A2A", padx=20, pady=20)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text=f"PlaylistManager v{latest_version} is available!",
        font=("Noto", 14, "bold"),
        background="#2A2A2A",
        foreground="white",
    ).pack(anchor="w")

    tk.Label(
        frame,
        text=f"Current version: v{__version__}",
        font=("Noto", 10),
        background="#2A2A2A",
        foreground="#AAAAAA",
    ).pack(anchor="w", pady=(0, 10))

    if body:
        text_frame = tk.Frame(frame, background="#333333")
        text_frame.pack(fill="both", expand=True, pady=(0, 10))
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)

        text_widget = tk.Text(
            text_frame,
            wrap="word",
            font=("Noto", 10),
            background="#333333",
            foreground="#CCCCCC",
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

    btn_frame = tk.Frame(frame, background="#2A2A2A")
    btn_frame.pack(fill="x")

    tk.Button(
        btn_frame,
        text="Download",
        font=("Noto", 10),
        background="#006713",
        activebackground="#008A1A",
        foreground="white",
        activeforeground="white",
        relief="flat",
        cursor="hand2",
        padx=16,
        command=lambda: (webbrowser.open(download_url), win.destroy()),
    ).pack(side="right", padx=(8, 0))

    tk.Button(
        btn_frame,
        text="Close",
        font=("Noto", 10),
        background="#444444",
        activebackground="#555555",
        foreground="white",
        activeforeground="white",
        relief="flat",
        cursor="hand2",
        padx=16,
        command=win.destroy,
    ).pack(side="right")

    center_window(win)
