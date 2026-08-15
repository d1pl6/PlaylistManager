"""
Platform login / credential dialog.

Responsibilities:
  - tkinter widget layout and visual feedback (status labels, buttons).
  - Thread-safe credential verification (threading + ``win.after()``).

Actual credential-file i/o, terminal launching, and API verification are
delegated to :mod:`services.auth_setup` and :mod:`utils.platform`.
"""

import json
import logging
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Any, Optional, cast

from services import auth_setup
from constants import PLATFORM_SPOTIFY, PLATFORM_YOUTUBE_MUSIC
from utils.scaling import ui_font
from utils.icons import IconService
from utils.window import center_window
from utils.theme import C
from utils.logging_config import user_log

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
        font=ui_font(14),
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
        img = IconService.get(icon_path, 32)
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
            font=ui_font(20),
        ).pack(pady=(5, 5))

    tk.Label(
        frame,
        text=name,
        background=frame_bg,
        foreground=label_fg,
        font=ui_font(10),
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
    if on_success:
        # The YT handoff is asynchronous - poll until browser.json lands,
        # then tell the app which platform's credentials changed so a
        # scoped refresh doesn't re-verify (and possibly deauthenticate)
        # Spotify.
        _poll_for_browser_json(
            parent, lambda: on_success(PLATFORM_YOUTUBE_MUSIC)
        )


def _browser_file_ready() -> bool:
    """Whether a *complete, usable* browser.json exists.

    The auth manager (``YouTubeAuthManager._find_browser_file``) accepts
    the primary platformdirs path plus legacy fallback locations; the
    poll must mirror that, or a fallback copy would never trigger the
    refresh and the login would silently only apply on restart.

    ``ytmusicapi browser`` writes the file with a plain ``open(path, "w")``
    + ``json.dump``: the file *exists* from the moment the write truncates
    it, before a single byte of JSON lands.  Firing the refresh on mere
    existence builds the client from an empty or partial file - ``YTMusic``
    raises ``JSONDecodeError``, the scoped refresh fails, and the poll
    never fires again (``on_success`` is called exactly once), leaving
    every YouTube feature broken until the app is restarted.  Requiring
    valid JSON that actually carries the auth cookie/access token closes
    that window.
    """
    try:
        from integrations.music_youtube.music_youtube import (
            BROWSER_FILE as YT_BROWSER_FILE,
            BROWSER_FILE_FALLBACKS,
        )
    except Exception:
        return _complete_browser_file(auth_setup.BROWSER_FILE)
    return any(
        _complete_browser_file(p) for p in [YT_BROWSER_FILE, *BROWSER_FILE_FALLBACKS]
    )


def _complete_browser_file(path: Path) -> bool:
    """True when *path* is a browser.json that ``YTMusic`` can consume.

    Valid JSON is not enough on its own - a headers file without the
    auth cookie constructs an UNAUTHORIZED client whose every browse
    fails.  The check mirrors ytmusicapi's own requirement (its setup
    refuses to write a file without ``cookie``).
    """
    try:
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        keys = {k.lower() for k in data}
        return bool(keys & {"cookie", "access_token"})
    except Exception:
        return False


def _poll_for_browser_json(parent, on_success, attempts: int = 0) -> None:
    """Wait for browser.json and fire *on_success* once it appears.

    The YT auth flow hands off to a terminal (``ytmusicapi browser``),
    so the credential file can land any time after the terminal opens -
    long after this callback returns.  Poll cheaply on the main thread
    via ``after`` so ``refresh_auth`` picks the new credentials up
    without an app restart.  Stops when the login dialog closes, or the
    wait window (~5 min) expires.
    """
    try:
        if not parent.winfo_exists():
            return
    except tk.TclError:
        return

    if _browser_file_ready():
        on_success()
        return

    if attempts >= 150:
        user_log(
            logger,
            "browser.json not detected yet - re-login will be picked up on restart",
        )
        return

    parent.after(2000, lambda: _poll_for_browser_json(parent, on_success, attempts + 1))


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
        font=ui_font(14),
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
            font=ui_font(10),
        ).grid(row=i, column=0, sticky="w", pady=5)

        entry = tk.Entry(
            fields_frame,
            textvariable=var,
            background=entry_bg,
            foreground=entry_fg,
            insertbackground=entry_fg,
            font=ui_font(10),
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
        font=ui_font(10),
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
                    on_success(PLATFORM_SPOTIFY)
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
        _set_busy(True)

        def run():
            result = auth_setup.verify_spotify_credentials(**creds)
            try:
                win.after(0, _test_done, result)
            except Exception:
                # App quit (or dialog destroyed) while the verify round
                # trip was in flight - nothing to schedule against.
                logger.debug("Login dialog closed during verification", exc_info=True)

        threading.Thread(target=run, daemon=True).start()

    def _test_done(result):
        # The dialog may have been closed while the verify thread ran -
        # the scheduled after() callback then fires against destroyed
        # widgets, so bail out before touching them.
        try:
            if not win.winfo_exists():
                return
        except tk.TclError:
            return
        _set_busy(False)
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
        _set_busy(True)

        def run():
            result = auth_setup.save_and_verify_spotify_credentials(**creds)
            try:
                win.after(0, _save_done, result)
            except Exception:
                # App quit (or dialog destroyed) while the verify round
                # trip was in flight - nothing to schedule against.
                logger.debug("Login dialog closed during verification", exc_info=True)

        threading.Thread(target=run, daemon=True).start()

    def _save_done(result):
        try:
            if not win.winfo_exists():
                return
        except tk.TclError:
            return
        _set_busy(False)
        if result.get("ok"):
            status_label.config(
                text=f"OK: {result['display_name']}",
                foreground=C["label_playlist_good_fg"],
            )
            if on_success:
                on_success(PLATFORM_SPOTIFY)
        else:
            status_label.config(text=result.get("error", "Error"), foreground="red")

    # ---- layout ----
    btn_delete = tk.Button(
        btn_frame,
        text="Delete",
        cursor="hand2",
        background=button_close_bg,
        foreground=button_close_fg,
        activebackground=button_close_a_bg,
        font=ui_font(10),
        highlightthickness=0,
        relief="raised",
        command=delete_credentials,
    )
    btn_delete.pack(side="left")

    btn_test = tk.Button(
        btn_frame,
        text="Test",
        cursor="hand2",
        background=btn_test_bg,
        foreground=btn_test_fg,
        activebackground=btn_test_abg,
        font=ui_font(10),
        highlightthickness=0,
        relief="raised",
        command=test_credentials,
    )
    btn_test.pack(side="left", padx=5)

    btn_save = tk.Button(
        btn_frame,
        text="Save",
        cursor="hand2",
        background=button_save_bg,
        foreground=button_save_fg,
        activebackground=button_save_a_bg,
        font=ui_font(10),
        highlightthickness=0,
        relief="raised",
        command=save_credentials,
    )
    btn_save.pack(side="right")

    def _set_busy(busy: bool) -> None:
        """Disable/enable every credential button while a verify round trip
        is in flight - Test and Save must never run concurrently (Spotify's
        refresh-token rotation makes a second concurrent /v1/me fail, and a
        delete mid-verify races the file write)."""
        state = "disabled" if busy else "normal"
        cursor = "arrow" if busy else "hand2"
        btn_test.config(state=state, cursor=cursor)
        btn_save.config(state=state, cursor=cursor)
        btn_delete.config(state=state, cursor=cursor)

    center_window(win)
