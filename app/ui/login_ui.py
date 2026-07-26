import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
from typing import Any, Optional, cast

from platformdirs import user_config_dir

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
LOGOS_DIR = ASSETS_DIR / "logos"
AUTH_DIR = Path(user_config_dir("playlistmanager")) / "auth"


from utils import center_window
from utils.config import get_theme_value


def _open_directory(path: Path):
    try:
        if platform.system() == "Windows":
            os.startfile(str(path))
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        logger.error(f"Failed to open directory {path}: {e}")


def _theme_value(section: str, option: str, default: str) -> str:
    try:
        return get_theme_value(section, option, default)
    except Exception:
        return default


def show_login_dialog(parent, on_success=None):
    win = tk.Toplevel(parent)
    win.title("Login")
    win.configure(background=_theme_value("frame_main", "background", "#2A2A2A"))
    win.transient(parent)
    win.update_idletasks()
    win.grab_set()

    header_bg = _theme_value("frame_header", "background", "#2A2A2A")
    label_fg = _theme_value("label", "foreground", "white")
    button_bg = _theme_value("button_header", "background", "#0A0000")
    button_abg = _theme_value("button_header", "activebackground", "#320000")
    button_fg = _theme_value("button_header", "foreground", "white")
    button_afg = _theme_value("button_header", "activeforeground", "#FFFFFF")
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

    img = None
    try:
        img = tk.PhotoImage(file=str(icon_path))
    except Exception as e:
        logger.error(f"Failed to load icon {icon_path}: {e}")

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


def _on_youtube_music(parent, on_success):
    AUTH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _open_directory(AUTH_DIR)

    terminal_cmd = _get_terminal_command(AUTH_DIR)
    try:
        subprocess.Popen(terminal_cmd)
    except Exception as e:
        logger.error(f"Failed to open terminal: {e}")
        messagebox.showinfo(
            "Manual Step Required",
            f"Open a terminal and run:\n\n"
            f"cd {AUTH_DIR}\nytmusicapi browser\n\n"
            f"Then place the generated browser.json in:\n{AUTH_DIR}",
            parent=parent,
        )


def _find_linux_terminal() -> Optional[str]:
    for term in [
        "kgx", "gnome-terminal", "konsole", "xfce4-terminal",
        "xterm", "alacritty", "kitty", "wezterm", "tilix",
        "foot", "x-terminal-emulator",
    ]:
        if shutil.which(term):
            return term
    return None


def _find_shell() -> str:
    for sh in ["bash", "sh", "dash", "mksh", "busybox"]:
        path = shutil.which(sh)
        if path:
            return path
    for p in ["/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh"]:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return "sh"


def _get_terminal_command(work_dir: Path) -> list:
    system = platform.system()
    if system == "Windows":
        return ["cmd", "/k", f"cd /d \"{work_dir}\" && ytmusicapi browser"]
    elif system == "Darwin":
        return ["open", "-a", "Terminal", str(work_dir)]
    else:
        term = _find_linux_terminal()
        if not term:
            raise FileNotFoundError("No supported terminal emulator found")
        shell = _find_shell()
        cmd = f'cd "{work_dir}" && ytmusicapi browser; exec {shell}'
        if term in ("xterm",):
            return [term, "-e", shell, "-c", cmd]
        return [term, "-e", shell, "-c", cmd]


def _on_spotify(parent, on_success):
    win = tk.Toplevel(parent)
    win.title("Spotify Login")
    win.configure(background=_theme_value("frame_main", "background", "#2A2A2A"))
    win.transient(parent)
    win.update_idletasks()
    win.grab_set()

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

    existing = {}
    spotify_file = AUTH_DIR / "spotify.json"
    if spotify_file.exists():
        try:
            existing = json.loads(spotify_file.read_text(encoding="utf-8"))
        except Exception:
            pass

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

    fields_frame = tk.Frame(
        win,
        background=_theme_value("frame_main", "background", "#2A2A2A"),
        )
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
            background=_theme_value("frame_main", "background", "#2A2A2A"),
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
            show="*"
        )
        entry.grid(row=i, column=1, sticky="ew", pady=5, padx=(10, 0))

    fields_frame.columnconfigure(1, weight=1)

    status_label = tk.Label(
        win,
        text="",
        background=_theme_value("frame_main", "background", "#2A2A2A"),
        foreground=label_fg,
        font=("Noto", 10),
    )
    status_label.pack(padx=20, pady=5)

    btn_frame = tk.Frame(win, background=_theme_value("frame_main", "background", "#2A2A2A"))
    btn_frame.pack(fill="x", padx=20, pady=10)

    def save_credentials():
        creds = {
            "client_id": client_id_var.get().strip(),
            "client_secret": client_secret_var.get().strip(),
            "refresh_token": refresh_token_var.get().strip(),
        }
        if not all(creds.values()):
            status_label.config(text="All fields are required", foreground="red")
            return
        try:
            AUTH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(str(spotify_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, json.dumps(creds, indent=2).encode("utf-8"))
            finally:
                os.close(fd)
            status_label.config(text="Verifying...", foreground="white")
            btn_save.config(state="disabled")

            def verify():
                try:
                    from integrations.music_spotify.music_spotify import SpotifyAPI

                    api = SpotifyAPI(**creds)
                    me = api.get_me()
                    if me:
                        win.after(
                            0,
                            lambda: _save_done(
                                True, f"OK: {me.get('display_name', 'unknown')}"
                            ),
                        )
                    else:
                        win.after(0, lambda: _save_done(False, "Auth failed"))
                except Exception as e:
                    win.after(0, lambda: _save_done(False, f"Error: {e}"))

            def _save_done(ok, msg):
                btn_save.config(state="normal")
                if ok:
                    status_label.config(text=msg, foreground="#006713")
                    if on_success:
                        on_success()
                else:
                    status_label.config(text=msg, foreground="red")

            threading.Thread(target=verify, daemon=True).start()
        except Exception as e:
            status_label.config(text=f"Save failed: {e}", foreground="red")

    def test_credentials():
        creds = {
            "client_id": client_id_var.get().strip(),
            "client_secret": client_secret_var.get().strip(),
            "refresh_token": refresh_token_var.get().strip(),
        }
        if not all(creds.values()):
            status_label.config(text="All fields are required", foreground="red")
            return
        status_label.config(text="Testing...", foreground="white")
        btn_test.config(state="disabled")

        def run_test():
            try:
                from integrations.music_spotify.music_spotify import SpotifyAPI

                api = SpotifyAPI(**creds)
                me = api.get_me()
                if me:
                    win.after(
                        0,
                        lambda: status_label.config(
                            text=f"OK: {me.get('display_name', 'unknown')}",
                            foreground="#006713",
                        ),
                    )
                else:
                    win.after(
                        0,
                        lambda: status_label.config(
                            text="Auth failed", foreground="red"
                        ),
                    )
            except Exception as e:
                win.after(
                    0,
                    lambda: status_label.config(text=f"Error: {e}", foreground="red"),
                )
            finally:
                win.after(0, lambda: btn_test.config(state="normal"))

        threading.Thread(target=run_test, daemon=True).start()

    def delete_credentials():
        if spotify_file.exists():
            try:
                spotify_file.unlink()
                client_id_var.set("")
                client_secret_var.set("")
                refresh_token_var.set("")
                status_label.config(text="Credentials deleted", foreground="#006713")
                if on_success:
                    on_success()
            except Exception as e:
                status_label.config(text=f"Delete failed: {e}", foreground="red")
        else:
            status_label.config(text="No credentials file found", foreground="red")

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
