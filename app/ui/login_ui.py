"""
Platform login / credential dialog.

Responsibilities:
  - tkinter widget layout and visual feedback (status labels, buttons).
  - Thread-safe credential verification (threading + ``win.after()``).

Actual credential-file i/o, terminal launching, and API verification are
delegated to :mod:`services.auth_setup` and :mod:`utils.platform`.
"""

import logging
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Any, Optional, cast

from services import auth_setup
from utils import center_window
from utils.config import get_theme_value

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
LOGOS_DIR = ASSETS_DIR / "logos"


def _theme_value(section: str, option: str, default: str) -> str:
    try:
        return get_theme_value(section, option, default)
    except Exception:
        return default


# ======================================================================
# Public entry point
# ======================================================================


def show_login_dialog(parent, on_success=None):
    win = tk.Toplevel(parent)
    win.title("Login")
    win.configure(background=_theme_value("frame_main", "background", "#2A2A2A"))
    win.transient(parent)
    win.update_idletasks()
    win.grab_set()

    header_bg = _theme_value("frame_header", "background", "#2A2A2A")
    label_fg = _theme_value("label", "foreground", "white")
    button_c_bg = _theme_value("button_close", "background", "#0A0000")
    button_c_fg = _theme_value("button_close", "foreground", "white")
    button_c_abg = _theme_value("button_close", "activebackground", "#320000")
    button_c_afg = _theme_value("button_close", "activeforeground", "#FF0000")
    frame_bg = _theme_value("frame_main", "background", "#2A2A2A")

    header = tk.Frame(win, background=header_bg)
    header.pack(fill="x", padx=10, pady=10)

    tk.Label(
        header,
        text="Select platform",
        background=header_bg,
        foreground=label_fg,
        font=("Noto", 14),
    ).pack(side="left", anchor="w")

    tk.Button(
        header,
        text="Cancel",
        background=button_c_bg,
        foreground=button_c_fg,
        activebackground=button_c_abg,
        activeforeground=button_c_afg,
        font=("Noto", 10),
        bd=1,
        command=win.destroy,
    ).pack(side="right", anchor="e")

    platforms_frame = tk.Frame(win, background=frame_bg)
    platforms_frame.pack(pady=20, padx=20)

    _create_platform_button(
        platforms_frame,
        "YouTube Music",
        LOGOS_DIR / "youtube-music.png",
        lambda: _on_youtube_music(win, on_success),
    ).pack(side="left", padx=20)

    _create_platform_button(
        platforms_frame,
        "Spotify",
        LOGOS_DIR / "spotify.png",
        lambda: _on_spotify(win, on_success),
    ).pack(side="left", padx=20)

    center_window(win)


def _create_platform_button(parent, name, icon_path, command):
    frame_bg = _theme_value("frame_main", "background", "#404040")
    label_fg = _theme_value("label", "foreground", "white")
    frame = tk.Frame(parent, background=frame_bg, cursor="hand2", padx=10, pady=10)

    img: Optional[tk.PhotoImage] = None
    try:
        img = tk.PhotoImage(file=str(icon_path))
    except Exception as e:
        logger.error("Failed to load icon %s: %s", icon_path, e)

    if img:
        label_img = tk.Label(frame, image=img, background=frame_bg)
        cast(Any, label_img).image = img
        label_img.pack(pady=(5, 5))
    else:
        tk.Label(
            frame,
            text="?",
            background=frame_bg,
            foreground=label_fg,
            font=("Noto", 20),
        ).pack(pady=(5, 5))

    tk.Label(
        frame,
        text=name,
        background=frame_bg,
        foreground=label_fg,
        font=("Noto", 10),
    ).pack(pady=(0, 5))

    for widget in [frame] + list(frame.winfo_children()):
        widget.bind("<Button-1>", lambda e: command())

    return frame


# ======================================================================
# YouTube Music flow
# ======================================================================


def _on_youtube_music(parent, on_success):
    result = auth_setup.setup_ytmusic_auth()
    if result.get("manual"):
        messagebox.showinfo(
            "Manual Step Required",
            f"Open a terminal and run:\n\n"
            f"cd {result['auth_dir']}\n"
            f"ytmusicapi browser\n\n"
            f"Then place the generated browser.json in:\n{result['auth_dir']}",
            parent=parent,
        )


# ======================================================================
# Spotify flow
# ======================================================================


def _on_spotify(parent, on_success):
    win = tk.Toplevel(parent)
    win.title("Spotify Login")
    win.configure(background=_theme_value("frame_main", "background", "#2A2A2A"))
    win.transient(parent)
    win.update_idletasks()
    win.grab_set()

    # ---------- theme colours ----------
    header_bg = _theme_value("frame_header", "background", "#2A2A2A")
    label_fg = _theme_value("label", "foreground", "white")
    entry_bg = _theme_value("frame_main", "background", "#404040")
    entry_fg = _theme_value("label", "foreground", "white")
    btn_bg = _theme_value("button_main", "background", "#006713")
    btn_abg = _theme_value("button_main", "activebackground", "#004d0e")
    btn_fg = _theme_value("button_main", "foreground", "white")
    btn_test_bg = _theme_value("button_main", "background", "#404040")
    btn_test_abg = _theme_value("button_main", "activebackground", "#555555")
    btn_test_fg = _theme_value("button_main", "foreground", "white")
    normal_btn_bg = _theme_value("button_header", "background", "#0A0000")
    normal_btn_abg = _theme_value("button_header", "activebackground", "#320000")
    normal_btn_fg = _theme_value("button_header", "foreground", "white")
    normal_btn_afg = _theme_value("button_header", "activeforeground", "#FFFFFF")
    button_c_bg = _theme_value("button_close", "background", "#0A0000")
    button_c_fg = _theme_value("button_close", "foreground", "white")
    button_c_abg = _theme_value("button_close", "activebackground", "#320000")
    button_c_afg = _theme_value("button_close", "activeforeground", "#FF0000")
    frame_bg = _theme_value("frame_main", "background", "#2A2A2A")

    # ---------- load existing ----------
    existing = auth_setup.load_spotify_credentials()

    # ---------- header ----------
    header = tk.Frame(win, background=header_bg)
    header.pack(fill="x", padx=10, pady=10)

    tk.Label(
        header,
        text="Spotify Credentials",
        background=header_bg,
        foreground=label_fg,
        font=("Noto", 14),
    ).pack(side="left", anchor="w")

    tk.Button(
        header,
        text="Cancel",
        background=button_c_bg,
        foreground=button_c_fg,
        activebackground=button_c_abg,
        activeforeground=button_c_afg,
        font=("Noto", 10),
        bd=1,
        command=win.destroy,
    ).pack(side="right", anchor="e")

    # ---------- fields ----------
    fields_frame = tk.Frame(win, background=frame_bg)
    fields_frame.pack(fill="x", padx=20, pady=10)

    client_id_var = tk.StringVar(value=existing.get("client_id", ""))
    client_secret_var = tk.StringVar(value=existing.get("client_secret", ""))
    refresh_token_var = tk.StringVar(value=existing.get("refresh_token", ""))

    fields = [
        ("Client ID", client_id_var),
        ("Client Secret", client_secret_var),
        ("Refresh Token", refresh_token_var),
    ]

    for i, (label_text, var) in enumerate(fields):
        tk.Label(
            fields_frame,
            text=label_text,
            background=frame_bg,
            foreground=label_fg,
            font=("Noto", 10),
        ).grid(row=i, column=0, sticky="w", pady=5)

        entry = tk.Entry(
            fields_frame,
            textvariable=var,
            background=entry_bg,
            foreground=entry_fg,
            insertbackground=entry_fg,
            font=("Noto", 10),
            width=40,
            show="*",
        )
        entry.grid(row=i, column=1, sticky="ew", pady=5, padx=(10, 0))

    fields_frame.columnconfigure(1, weight=1)

    # ---------- status ----------
    status_label = tk.Label(
        win,
        text="",
        background=frame_bg,
        foreground=label_fg,
        font=("Noto", 10),
    )
    status_label.pack(padx=20, pady=5)

    # ---------- buttons ----------
    btn_frame = tk.Frame(win, background=frame_bg)
    btn_frame.pack(fill="x", padx=20, pady=10)

    def _get_creds():
        return {
            "client_id": client_id_var.get().strip(),
            "client_secret": client_secret_var.get().strip(),
            "refresh_token": refresh_token_var.get().strip(),
        }

    def _all_filled(creds) -> bool:
        return all(v for v in creds.values())

    # ---- Delete ----
    def delete_credentials():
        try:
            deleted = auth_setup.delete_spotify_credentials()
            if deleted:
                client_id_var.set("")
                client_secret_var.set("")
                refresh_token_var.set("")
                status_label.config(text="Credentials deleted", foreground="#006713")
                if on_success:
                    on_success()
            else:
                status_label.config(text="No credentials file found", foreground="red")
        except Exception as e:
            status_label.config(text=f"Delete failed: {e}", foreground="red")

    # ---- Test ----
    def test_credentials():
        creds = _get_creds()
        if not _all_filled(creds):
            status_label.config(text="All fields are required", foreground="red")
            return
        status_label.config(text="Testing...", foreground="white")
        btn_test.config(state="disabled")

        def run():
            result = auth_setup.verify_spotify_credentials(**creds)
            win.after(0, _test_done, result)

        threading.Thread(target=run, daemon=True).start()

    def _test_done(result):
        btn_test.config(state="normal")
        if result.get("ok"):
            status_label.config(
                text=f"OK: {result['display_name']}", foreground="#006713"
            )
        else:
            status_label.config(text=result.get("error", "Error"), foreground="red")

    # ---- Save ----
    def save_credentials():
        creds = _get_creds()
        if not _all_filled(creds):
            status_label.config(text="All fields are required", foreground="red")
            return
        status_label.config(text="Verifying...", foreground="white")
        btn_save.config(state="disabled")

        def run():
            result = auth_setup.save_and_verify_spotify_credentials(**creds)
            win.after(0, _save_done, result)

        threading.Thread(target=run, daemon=True).start()

    def _save_done(result):
        btn_save.config(state="normal")
        if result.get("ok"):
            status_label.config(
                text=f"OK: {result['display_name']}", foreground="#006713"
            )
            if on_success:
                on_success()
        else:
            status_label.config(text=result.get("error", "Error"), foreground="red")

    # ---- layout ----
    btn_delete = tk.Button(
        btn_frame,
        text="Delete",
        background=button_c_bg,
        foreground=button_c_fg,
        activebackground=button_c_abg,
        font=("Noto", 10),
        command=delete_credentials,
    )
    btn_delete.pack(side="left")

    btn_test = tk.Button(
        btn_frame,
        text="Test",
        background=btn_test_bg,
        foreground=btn_test_fg,
        activebackground=btn_test_abg,
        font=("Noto", 10),
        command=test_credentials,
    )
    btn_test.pack(side="left", padx=5)

    btn_save = tk.Button(
        btn_frame,
        text="Save",
        background=btn_bg,
        foreground=btn_fg,
        activebackground=btn_abg,
        font=("Noto", 10),
        command=save_credentials,
    )
    btn_save.pack(side="right")

    center_window(win)
