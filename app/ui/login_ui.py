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
from utils.window import center_window
from utils.theme import C

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
LOGOS_DIR = ASSETS_DIR / "logos"


# ======================================================================
# Public entry point
# ======================================================================


def show_login_dialog(parent, on_success=None):
    frame_main_bg = C["frame_main_bg"]
    frame_header_bg = C["frame_head_bg"]
    label_bg = C["label_def_bg"]
    label_fg = C["label_def_fg"]

    win = tk.Toplevel(parent)
    win.title("Login")
    win.configure(background=frame_main_bg)
    win.transient(parent)
    win.update_idletasks()
    win.grab_set()


    header = tk.Frame(win, background=frame_header_bg)
    header.pack(fill="both")

    tk.Label(
        header,
        text="Select platform",
        background=frame_header_bg,
        foreground=label_fg,
        font=("Noto", 14),
    ).pack(fill="both", pady=5, padx=5)

    platforms_frame = tk.Frame(win, background=label_bg)
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
    frame_bg = C["frame_main_bg"]
    label_fg = C["label_def_fg"]
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
    win.configure(background=C["frame_main_bg"])
    win.transient(parent)
    win.update_idletasks()
    win.grab_set()

    # ---------- theme colours ----------
    header_bg = C["frame_head_bg"]
    label_fg = C["label_def_fg"]
    entry_bg = C["entry_default_bg"]
    entry_fg = C["entry_default_fg"]
    btn_bg = C["button_main_bg"]
    btn_abg = C["button_main_a_bg"]
    btn_fg = C["button_main_fg"]
    button_close_bg = C["button_close_bg"]
    button_close_fg = C["button_close_fg"]
    button_close_a_bg = C["button_close_a_bg"]
    btn_test_bg = C["button_main_bg"]
    btn_test_abg = C["button_main_a_bg"]
    btn_test_fg = C["button_main_fg"]
    button_save_bg = C["button_save_bg"]
    button_save_fg = C["button_save_fg"]
    button_save_a_bg = C["button_save_a_bg"]
    frame_bg = C["frame_main_bg"]

    existing = auth_setup.load_spotify_credentials()

    header = tk.Frame(win, background=header_bg)
    header.pack(fill="x", padx=10, pady=10)

    tk.Label(
        header,
        text="Spotify Credentials",
        background=header_bg,
        foreground=label_fg,
        font=("Noto", 14),
    ).pack(fill="both", pady=5, padx=5)

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
                status_label.config(
                    text="Credentials deleted",
                    foreground=C["label_playlist_good_fg"],
                )
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
                text=f"OK: {result['display_name']}",
                foreground=C["label_playlist_good_fg"],
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
                text=f"OK: {result['display_name']}", foreground=button_save_fg
            )
            if on_success:
                on_success()
        else:
            status_label.config(text=result.get("error", "Error"), foreground="red")

    # ---- layout ----
    btn_delete = tk.Button(
        btn_frame,
        text="Delete",
        background=button_close_bg,
        foreground=button_close_fg,
        activebackground=button_close_a_bg,
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
        background=button_save_bg,
        foreground=button_save_fg,
        activebackground=button_save_a_bg,
        font=("Noto", 10),
        command=save_credentials,
    )
    btn_save.pack(side="right")

    center_window(win)
