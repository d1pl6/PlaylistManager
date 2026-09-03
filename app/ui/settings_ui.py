import logging
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from _version import __version__
from services import profile_store
from ui.profiles_ui import (
    show_create_profile_dialog,
    show_edit_buckets_dialog,
    show_rename_profile_dialog,
)
from ui.scrollable import ScrollableFrame
from ui.settings_theme_ui import show_theme_dialog
from utils.config import (
    get_setting,
    get_setting_value,
    set_setting,
    set_setting_value,
)
from utils.scaling import UI_SCALE_PRESETS, ui_font
from utils.platform import is_wayland_session
from utils.theme import C, btn_colors, hover_bg
from utils.window import center_window

logger = logging.getLogger(__name__)
REPO_URL = "https://github.com/d1pl6/PlaylistManager"


def _toggle_setting(section, var):
    try:
        set_setting(section, bool(var.get()))
    except Exception as e:
        logger.error("Failed to write settings file: %s", e)


def _section_header(parent, title):
    frame = tk.Frame(parent, background=C["frame_main_bg"])
    frame.pack(fill="x", padx=8, pady=(6, 2))
    tk.Label(
        frame,
        text=title,
        background=C["frame_head_bg"],
        foreground=C["label_def_fg"],
        font=ui_font(12, "bold"),
    ).pack(fill="x", ipady=4)
    return frame


def _open_repo():
    try:
        webbrowser.open(REPO_URL)
    except Exception as exc:
        logger.warning("Failed to open repository URL: %s", exc)


def show_settings_dialog(
    parent,
    keybind_controller=None,
    on_theme_change=None,
    tray_available=None,
    on_tray_toggle=None,
    on_auto_resize_toggle=None,
    on_showcase_count_change=None,
    on_showcase_log_change=None,
    on_playlist_stats_change=None,
    on_columns_change=None,
    on_check_updates_now=None,
    on_check_duplicates_now=None,
    on_like_button_change=None,
    on_scrobble_keybind_change=None,
    on_restart_app=None,
    plugin_availability=None,
    ):
    """Show the settings dialog.

    Args:
        parent: tkinter parent window.
        keybind_controller: optional KeybindController for the global
            listener toggle.
        on_theme_change: optional callback re-applying the theme.
        tray_available: optional TrayService (or any object with an
            ``available`` attribute); when falsy/absent the hide-to-tray
            checkbutton is disabled.
        on_tray_toggle: optional callback applied live with the new
            hide-to-tray state (bool) when the checkbox changes.
        on_auto_resize_toggle: optional callback applied live with the
            new auto-resize state (bool) when the checkbox changes -
            without it the toggle only takes effect after a restart.
        on_showcase_count_change: optional callback applied live with the
            new showcase count (int) when the combobox changes - without
            it the change only takes effect after a restart.
        on_showcase_log_change: optional callback applied live with the
            new show-log-row state (bool) when the checkbox changes.
        on_playlist_stats_change: optional callback applied live with the
            new show-stats state (bool) when the checkbox changes.
        on_columns_change: optional callback applied live with the new
            card column count (int) when the combobox changes - without
            it the change only takes effect after a restart.
        on_check_duplicates_now: optional callback(on_done) running a
            manual duplicate scan off-thread; *on_done* receives
            ``(found_count, error_or_None)`` on the UI thread.
        on_like_button_change: optional callback applied live with the new
            like-button state (bool) when the checkbox changes.
        on_scrobble_keybind_change: optional callback fired after the
            scrobble keybind is recorded or cleared (no args); the caller
            re-registers/unregisters the live action keybind so the change
            takes effect immediately instead of after a restart.
        on_restart_app: optional callable()` restarting the app.  Used
            by the Profiles section when the user switches profile or
            creates/edits one - the change needs a restart to apply.
        plugin_availability: optional iterable of installed platform ids
            (from the live ``IntegrationRegistry``).  When given, sections
            for optional service plugins (Last.fm, SoundCloud) are hidden
            when the plugin is not installed.
    """
    theme_win_bg = C["frame_main_bg"]
    theme_header_bg = C["frame_head_bg"]
    theme_label_fg = C["label_def_fg"]
    theme_check_bg = C["checkbutton_bg"]
    theme_check_fg = C["checkbutton_fg"]
    theme_check_select = C["checkbutton_selector"]
    theme_check_btn = btn_colors(theme_check_bg, theme_check_fg)
    checkbutton_style = {
        **theme_check_btn,
        "highlightthickness": 0,
        "highlightbackground": theme_check_bg,
        "highlightcolor": theme_check_bg,
        "takefocus": False,
    }

    win = tk.Toplevel(parent)
    win.title("PlaylistManager")
    win.configure(background=theme_win_bg)
    win.transient(parent)
    win.grab_set()

    win.geometry("420x540")
    win.minsize(350, 320)

    tk.Label(
        win,
        text="Settings",
        background=theme_header_bg,
        foreground=theme_label_fg,
        font=ui_font(14),
    ).pack(fill="both", pady=(0,5))

    sf = ScrollableFrame(win, bg=C["scrollable_frame_bg"], show_scrollbar=True,
                         bind_all_mousewheel=True)
    sf.pack(side="left", fill="both", expand=True)
    sf.style_scrollbar(
        hover_bg(C["button_main_bg"]),
        C["scrollable_frame_bg"],
    )
    content = sf.content

    # Optional service plugins: hide their settings sections when the
    # plugin is not installed (integration not registered).
    installed = set(plugin_availability or ())
    has_lastfm = "lastfm" in installed
    has_soundcloud = "soundcloud" in installed

    update_var_value = 1 if get_setting("update_check", fallback=True) else 0
    center_var_value = 1 if get_setting("center_windows", fallback=True) else 0
    resize_var_value = 1 if get_setting("auto_resize", fallback=False) else 0
    global_var_value = 1 if get_setting("global_listener", fallback=True) else 0
    tray_var_value = 1 if get_setting("hide_to_tray", fallback=False) else 0
    try:
        showcase_count_value = int(get_setting_value("showcase", "count", "0"))
    except (ValueError, TypeError):
        showcase_count_value = 0
    showcase_log_value = 1 if get_setting("showcase_log", fallback=True) else 0
    playlist_stats_value = 1 if get_setting("playlist_stats", fallback=True) else 0
    try:
        columns_value = int(get_setting_value("layout", "columns", "2"))
    except (ValueError, TypeError):
        columns_value = 2

    update_var = tk.IntVar(value=update_var_value)
    center_var = tk.IntVar(value=center_var_value)
    resize_var = tk.IntVar(value=resize_var_value)
    global_var = tk.IntVar(value=global_var_value)
    tray_var = tk.IntVar(value=tray_var_value)

    app_section = tk.Frame(content, background=theme_win_bg)
    app_section.pack(fill="both", padx=8, pady=(0, 8))
    _section_header(app_section, "App behavior")

    tk.Checkbutton(
        app_section,
        text="Check for updates on startup",
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=lambda: _toggle_setting("update_check", update_var),
        variable=update_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    def _do_check_now():
        if not callable(on_check_updates_now):
            return
        update_btn.configure(state="disabled", text="Checking\u2026")

        def _on_complete(available, error):
            try:
                if available:
                    update_btn.configure(state="normal", text="Check for updates now")
                elif error:
                    update_btn.configure(
                        state="normal",
                        text="Check failed \u2014 try again",
                    )
                else:
                    update_btn.configure(state="normal", text="Up to date!")
            except tk.TclError:
                return
            try:
                win.after(
                    4000,
                    lambda: update_btn.configure(text="Check for updates now"),
                )
            except tk.TclError:
                pass

        on_check_updates_now(on_done=_on_complete)

    update_btn = tk.Button(
        app_section,
        text="Check for updates now",
        cursor="hand2",
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        font=ui_font(12),
        highlightthickness=0,
        relief="raised",
        command=_do_check_now,
    )
    update_btn.pack(fill="both", pady=(0,5), padx=16)

    tk.Checkbutton(
        app_section,
        text="Center windows after launch",
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=lambda: (
            _toggle_setting("center_windows", center_var),
            center_var.get() and center_window(win),
        ),
        variable=center_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    def _on_auto_resize_toggle():
        _toggle_setting("auto_resize", resize_var)
        if on_auto_resize_toggle is not None:
            on_auto_resize_toggle(resize_var.get() == 1)

    tk.Checkbutton(
        app_section,
        text="Auto-resize main window",
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_auto_resize_toggle,
        variable=resize_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    def _on_global_toggle():
        _toggle_setting("global_listener", global_var)
        if keybind_controller is not None:
            keybind_controller.set_global_listener(global_var.get() == 1)

    on_wayland = is_wayland_session()
    if on_wayland:
        # Global keybinds are impossible on native Wayland (the compositor
        # owns input; pynput's X11 listener never sees keys).  Disable the
        # toggle and force the local-listener mode.
        global_var.set(0)
        _toggle_setting("global_listener", global_var)
        if keybind_controller is not None:
            keybind_controller.set_global_listener(False)

    global_ck = tk.Checkbutton(
        app_section,
        text="Use global key listener",
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_global_toggle,
        variable=global_var,
    )
    global_ck.pack(fill="both", pady=(0,5), padx=16)
    if on_wayland:
        global_ck.configure(state="disabled", cursor="arrow")
        tk.Label(
            app_section,
            text="(not available on Wayland - use compositor shortcuts "
                 "bound to 'playlistmanager add N')",
            background=theme_win_bg,
            foreground=theme_label_fg,
            font=ui_font(9),
            anchor="w",
        ).pack(fill="x", padx=16)

    def _on_tray_toggle():
        _toggle_setting("hide_to_tray", tray_var)
        if on_tray_toggle is not None:
            on_tray_toggle(tray_var.get() == 1)

    tray_ck = tk.Checkbutton(
        app_section,
        text="Hide in tray on minimize",
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_tray_toggle,
        variable=tray_var,
    )
    tray_ck.pack(fill="both", pady=(0,5), padx=16)
    if not getattr(tray_available, "available", False):
        tray_ck.configure(state="disabled", cursor="arrow")

    dupcheck_section = tk.Frame(content, background=theme_win_bg)
    dupcheck_section.pack(fill="both", padx=8, pady=(0, 8))
    _section_header(dupcheck_section, "Duplicate check")

    def _on_dup_check_toggle():
        _toggle_setting("duplicate_check", dup_var)

    dup_var = tk.IntVar(value=1 if get_setting("duplicate_check", fallback=False) else 0)
    tk.Checkbutton(
        dupcheck_section,
        text="Extra duplicate check",
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_dup_check_toggle,
        variable=dup_var,
    ).pack(fill="both", pady=(0,0), padx=16)

    tk.Label(
        dupcheck_section,
        text="(asks when a similar song is already in the playlist)",
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(9),
        anchor="w",
    ).pack(fill="x", padx=16)

    def _do_dup_check_now():
        if not callable(on_check_duplicates_now):
            return
        dup_btn.configure(state="disabled", text="Scanning\u2026")

        def _on_complete(found, error):
            try:
                if error:
                    dup_btn.configure(state="normal", text="Scan failed \u2014 try again")
                elif found:
                    dup_btn.configure(state="normal", text=f"{found} found!")
                else:
                    dup_btn.configure(state="normal", text="No duplicates found")
            except tk.TclError:
                return
            try:
                win.after(
                    4000,
                    lambda: dup_btn.configure(text="Check for duplicates now"),
                )
            except tk.TclError:
                pass

        on_check_duplicates_now(on_done=_on_complete)

    dup_btn = tk.Button(
        dupcheck_section,
        text="Check for duplicates now",
        cursor="hand2",
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        font=ui_font(12),
        highlightthickness=0,
        relief="raised",
        command=_do_dup_check_now,
    )
    dup_btn.pack(fill="both", pady=(4,5), padx=16)

    appearance_section = tk.Frame(content, background=theme_win_bg)
    appearance_section.pack(fill="both", padx=8, pady=(0, 8))
    _section_header(appearance_section, "Appearance")

    def _on_showcase_count_change(value: str) -> None:
        try:
            set_setting_value("showcase", "count", value)
        except Exception as e:
            logger.error("Failed to write showcase count setting: %s", e)
        if on_showcase_count_change is not None:
            try:
                on_showcase_count_change(int(value))
            except (ValueError, TypeError):
                pass

    def _on_showcase_log_toggle():
        _toggle_setting("showcase_log", showcase_log_var)
        if on_showcase_log_change is not None:
            on_showcase_log_change(showcase_log_var.get() == 1)

    showcase_log_var = tk.IntVar(value=showcase_log_value)
    tk.Checkbutton(
        appearance_section,
        text="Show log row (artist / song / status)",
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_showcase_log_toggle,
        variable=showcase_log_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    def _on_playlist_stats_toggle():
        _toggle_setting("playlist_stats", playlist_stats_var)
        if on_playlist_stats_change is not None:
            on_playlist_stats_change(playlist_stats_var.get() == 1)

    playlist_stats_var = tk.IntVar(value=playlist_stats_value)
    tk.Checkbutton(
        appearance_section,
        text="Show playlist stats (songs / duration / followers)",
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_playlist_stats_toggle,
        variable=playlist_stats_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    showcase_row = tk.Frame(appearance_section, background=theme_check_bg)
    showcase_row.pack(fill="both", pady=(0,5), padx=16)
    tk.Label(
        showcase_row,
        text="Show last N added songs:",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    ).pack(side="left", pady=(0,5))

    showcase_count_var = tk.StringVar(value=str(showcase_count_value))
    showcase_combo = ttk.Combobox(
        showcase_row,
        textvariable=showcase_count_var,
        cursor="hand2",
        values=("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"),
        state="readonly",
        width=4,
        font=ui_font(12),
    )
    showcase_combo.pack(side="left", pady=(0,5))
    showcase_combo.bind(
        "<<ComboboxSelected>>",
        lambda e: _on_showcase_count_change(showcase_count_var.get()),
    )
    tk.Label(
        showcase_row,
        text="(0 = off)",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(9),
    ).pack(side="left", pady=(0,5))

    def _on_ui_scale_change(value: str) -> None:
        try:
            set_setting_value("ui_scale", "value", value)
        except Exception as e:
            logger.error("Failed to write ui_scale setting: %s", e)

    scale_row = tk.Frame(appearance_section, background=theme_check_bg)
    scale_row.pack(fill="both", pady=(0, 5), padx=16)
    tk.Label(
        scale_row,
        text="UI scale:",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    ).pack(side="left", pady=(0,5))

    scale_var = tk.StringVar(value=get_setting_value("ui_scale", "value", "auto"))
    scale_combo = ttk.Combobox(
        scale_row,
        textvariable=scale_var,
        cursor="hand2",
        values=UI_SCALE_PRESETS,
        state="readonly",
        width=6,
        font=ui_font(12),
    )
    scale_combo.pack(side="left", pady=(0,5))
    scale_combo.bind(
        "<<ComboboxSelected>>",
        lambda e: _on_ui_scale_change(scale_var.get()),
    )
    tk.Label(
        scale_row,
        text="(restart to apply)",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(9),
    ).pack(side="left", pady=(0,5))

    def _on_columns_change(value: str) -> None:
        try:
            set_setting_value("layout", "columns", value)
        except Exception as e:
            logger.error("Failed to write columns setting: %s", e)
        if on_columns_change is not None:
            try:
                on_columns_change(int(value))
            except (ValueError, TypeError):
                pass

    columns_row = tk.Frame(appearance_section, background=theme_check_bg)
    columns_row.pack(fill="both", pady=(0,5), padx=16)
    tk.Label(
        columns_row,
        text="Card columns:",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    ).pack(side="left", pady=(0,5))

    columns_var = tk.StringVar(value=str(columns_value))
    columns_combo = ttk.Combobox(
        columns_row,
        textvariable=columns_var,
        cursor="hand2",
        values=("1", "2", "3", "4"),
        state="readonly",
        width=4,
        font=ui_font(12),
    )
    columns_combo.pack(side="left", pady=(0,5))
    columns_combo.bind(
        "<<ComboboxSelected>>",
        lambda e: _on_columns_change(columns_var.get()),
    )
    tk.Label(
        columns_row,
        text="(applies immediately)",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(9),
    ).pack(side="left", pady=(0,5))

    tk.Button(
        appearance_section,
        text="Theme Settings",
        cursor="hand2",
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        font=ui_font(12),
        highlightthickness=0,
        relief="raised",
        bd=0,
        command=lambda: show_theme_dialog(win, on_theme_change=on_theme_change),
    ).pack(fill="both", pady=(0,4), padx=16)

    # -- Last.fm section -----------------------------------------------
    if has_lastfm:
        lastfm_section = tk.Frame(content, background=theme_win_bg)
        lastfm_section.pack(fill="both", padx=8, pady=(0, 8))
        _section_header(lastfm_section, "Last.fm")

        like_button_var = tk.BooleanVar(value=get_setting("like_button"))
        like_button_check = tk.Checkbutton(
            lastfm_section,
            text="Like button (♥/♡ under remove button)",
            variable=like_button_var,
            cursor="hand2",
            selectcolor=theme_check_select,
            **checkbutton_style,
            font=ui_font(11),
        )
        like_button_check.pack(anchor="w", padx=16, pady=4)

        def on_like_button_toggle():
            enabled = bool(like_button_var.get())
            _toggle_setting("like_button", like_button_var)
            if on_like_button_change:
                try:
                    on_like_button_change(enabled)
                except Exception as e:
                    logger.error("Failed to apply like_button change: %s", e)

        like_button_check.config(command=on_like_button_toggle)

        scrobble_on_add_var = tk.BooleanVar(value=get_setting("scrobble_on_add"))
        scrobble_on_add_check = tk.Checkbutton(
            lastfm_section,
            text="Scrobble added songs",
            variable=scrobble_on_add_var,
            cursor="hand2",
            selectcolor=theme_check_select,
            **checkbutton_style,
            font=ui_font(11),
        )
        scrobble_on_add_check.pack(anchor="w", padx=16, pady=4)
        scrobble_on_add_check.config(command=lambda: _toggle_setting("scrobble_on_add", scrobble_on_add_var))

        # Scrobble keybind capture row
        scrobble_keybind_row = tk.Frame(lastfm_section, background=theme_check_bg)
        scrobble_keybind_row.pack(fill="x", padx=16, pady=4)

        tk.Label(
            scrobble_keybind_row,
            text="Scrobble keybind:",
            background=theme_check_bg,
            foreground=theme_check_fg,
            font=ui_font(11),
        ).pack(side="left")

        scrobble_keybind_display = tk.Label(
            scrobble_keybind_row,
            text=get_setting_value("scrobble_keybind", "keybind") or "(none)",
            background=theme_check_bg,
            foreground=theme_check_fg,
            font=ui_font(11),
            anchor="w",
        )
        scrobble_keybind_display.pack(side="left", fill="x", expand=True, padx=(8, 4))

        def capture_scrobble_keybind():
            if not keybind_controller:
                logger.warning("Keybind controller not available")
                return

            def on_combo(combo_str):
                set_setting_value("scrobble_keybind", "keybind", combo_str)
                scrobble_keybind_display.config(text=combo_str or "(none)")
                if on_scrobble_keybind_change is not None:
                    try:
                        on_scrobble_keybind_change()
                    except Exception as e:
                        logger.error("Failed to re-register scrobble keybind: %s", e)

            keybind_controller.start_recording(on_combo)

        tk.Button(
            scrobble_keybind_row,
            text="Record",
            cursor="hand2",
            **btn_colors(C["button_main_bg"], C["button_main_fg"]),
            font=ui_font(10),
            highlightthickness=0,
            relief="raised",
            bd=0,
            command=capture_scrobble_keybind,
        ).pack(side="left", padx=(0, 4))

        tk.Button(
            scrobble_keybind_row,
            text="Clear",
            cursor="hand2",
            **btn_colors(C["button_main_bg"], C["button_main_fg"]),
            font=ui_font(10),
            highlightthickness=0,
            relief="raised",
            bd=0,
            command=lambda: (
                set_setting_value("scrobble_keybind", "keybind", ""),
                scrobble_keybind_display.config(text="(none)"),
                on_scrobble_keybind_change() if on_scrobble_keybind_change is not None else None,
            ),
        ).pack(side="left")

    if has_soundcloud:
        # -- SoundCloud section ---------------------------------------------
        soundcloud_section = tk.Frame(content, background=theme_win_bg)
        soundcloud_section.pack(fill="both", padx=8, pady=(0, 8))
        _section_header(soundcloud_section, "SoundCloud")

        scmode_row = tk.Frame(soundcloud_section, background=theme_win_bg)
        scmode_row.pack(fill="both", pady=(0, 5), padx=16)
        tk.Label(
            scmode_row,
            text="Capture current song via:",
            background=theme_win_bg,
            foreground=theme_label_fg,
            font=ui_font(12),
        ).pack(side="left", pady=(0, 5))

        scmode_var = tk.StringVar(
            value=get_setting_value("soundcloud", "capture_mode", "hybrid")
        )
        scmode_combo = ttk.Combobox(
            scmode_row,
            textvariable=scmode_var,
            cursor="hand2",
            values=("hybrid", "api", "extension"),
            state="readonly",
            width=11,
            font=ui_font(12),
        )
        scmode_combo.pack(side="left", pady=(0, 5))

        def _on_soundcloud_mode_change(mode: str) -> None:
            try:
                set_setting_value("soundcloud", "capture_mode", mode)
            except Exception:
                logger.error("Failed to write SoundCloud capture mode", exc_info=True)
            # Applied on the next flow build; keybind flows read it at
            # construction (KeybindController refresh rebuilds the flow).

        scmode_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: _on_soundcloud_mode_change(scmode_var.get()),
        )
        tk.Label(
            soundcloud_section,
            text=(
                "hybrid: prefers the browser extension (exact URL + play/pause),\n"
                "falls back to recently-played on a receiver miss\n"
                "api: reads the last played track from SoundCloud's API\n"
                "extension: extension only (no API fallback)"
            ),
            background=theme_win_bg,
            foreground=theme_label_fg,
            justify="left",
            font=ui_font(9),
        ).pack(anchor="w", padx=16, pady=(0, 6))

    # -- Profiles section ----------------------------------------------
    profiles_section = tk.Frame(content, background=theme_win_bg)
    profiles_section.pack(fill="both", padx=8, pady=(0, 8))
    _section_header(profiles_section, "Profiles")

    def _restart_prompt(on_commit=None) -> None:
        """Ask to restart the app so the profile change applies.

        *on_commit* (optional callable) runs only if the user chooses to
        restart ("Restart now?" -> Yes).  It should persist the profile
        change (e.g. ``set_active``) that must NOT be applied unless the
        app actually restarts - so a user who declines the restart is not
        left with an un-deletable active profile.
        """
        if not messagebox.askyesno(
            "Profiles",
            "The profile change takes effect after the app restarts.\n\n"
            "Restart now?",
            parent=win,
        ):
            return
        if callable(on_commit):
            try:
                on_commit()
            except ValueError as e:
                messagebox.showerror("Profile", str(e), parent=win)
                return
        if callable(on_restart_app):
            win.after(50, on_restart_app)

    active_name = profile_store.active_profile()

    active_row = tk.Frame(profiles_section, background=theme_win_bg)
    active_row.pack(fill="x", padx=16, pady=(0, 4))
    tk.Label(
        active_row,
        text="Active profile:",
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(11),
    ).pack(side="left")
    active_label = tk.Label(
        active_row,
        text=active_name,
        background=C["button_main_bg"],
        foreground=C["button_main_fg"],
        font=ui_font(11, "bold"),
        padx=6,
        pady=1,
    )
    active_label.pack(side="left", padx=(8, 0))

    def _refresh_active_label() -> None:
        active_label.config(text=profile_store.active_profile())

    def _on_profile_selected(event) -> None:
        # A Combobox selection switch: how the user picks/creates a
        # different profile.
        sel = profile_combo.get()
        if not sel or sel == profile_store.active_profile():
            return
        if not messagebox.askyesno(
            "Switch profile",
            f'Switch to profile "{sel}"?\n\nThe app must restart to apply.',
            parent=win,
        ):
            profile_combo.set(profile_store.active_profile())
            return
        # Do NOT call set_active here.  Persist the switch only once the
        # user commits to restarting; declining the restart leaves this
        # profile un-activated (and therefore still deletable).  Otherwise
        # a "Yes, switch" then "No, don't restart" run leaves the profile
        # marked active with no way to reset it without another restart.
        _restart_prompt(lambda: profile_store.set_active(sel))
        _refresh_active_label()

    combo_row = tk.Frame(profiles_section, background=theme_win_bg)
    combo_row.pack(fill="x", padx=16, pady=(0, 4))
    tk.Label(
        combo_row,
        text="Profiles:",
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(11),
    ).pack(side="left")
    profile_combo = ttk.Combobox(
        combo_row,
        cursor="hand2",
        state="readonly",
        width=18,
        font=ui_font(11),
    )
    profile_combo["values"] = profile_store.list_profiles()
    profile_combo.set(active_name)
    profile_combo.bind("<<ComboboxSelected>>", _on_profile_selected)
    profile_combo.pack(side="left", padx=(8, 0))

    btn_row = tk.Frame(profiles_section, background=theme_win_bg)
    btn_row.pack(fill="x", padx=16, pady=(4, 4))

    def _add_profile() -> None:
        # create() no longer auto-activates.  On save, prompt to restart;
        # the new profile becomes active only once the user commits to the
        # restart (so a declined restart leaves it deletable).
        def _on_created(name: str) -> None:
            _restart_prompt(lambda: profile_store.set_active(name))

        show_create_profile_dialog(win, on_created=_on_created)
        _refresh_profiles()

    def _rename_profile() -> None:
        sel = profile_combo.get()
        if not sel:
            return
        show_rename_profile_dialog(win, sel, on_restart=_restart_prompt)
        _refresh_profiles()

    def _edit_profile() -> None:
        sel = profile_combo.get()
        if not sel:
            return
        show_edit_buckets_dialog(win, sel, on_restart=_restart_prompt)
        _refresh_profiles()

    def _delete_profile() -> None:
        sel = profile_combo.get()
        if not sel:
            return
        if sel == "default":
            messagebox.showinfo(
                "Delete profile",
                "The default profile cannot be deleted.",
                parent=win,
            )
            return
        if not messagebox.askyesno(
            "Delete profile",
            f'Are you sure you want to delete "{sel}" profile? '
            "This will delete all playlist databases that are not in "
            "other profiles.",
            parent=win,
        ):
            return
        try:
            profile_store.delete(sel)
        except ValueError as e:
            messagebox.showerror("Delete profile", str(e), parent=win)
            return
        # Deleting a profile is immediate and needs no restart: it cannot be
        # the active profile, so no running path changes.
        _refresh_profiles()

    def _refresh_profiles() -> None:
        profiles = profile_store.list_profiles()
        profile_combo["values"] = profiles
        now_active = profile_store.active_profile()
        if now_active in profiles:
            profile_combo.set(now_active)
        elif profiles:
            profile_combo.set(profiles[0])
        _refresh_active_label()

    _ok_button = lambda text, cmd: tk.Button(
        btn_row,
        text=text,
        cursor="hand2",
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        font=ui_font(10),
        highlightthickness=0,
        relief="raised",
        bd=0,
        command=cmd,
    )
    _ok_button("Add", _add_profile).pack(side="left", padx=(0, 4), expand=True, fill="x")
    _ok_button("Rename", _rename_profile).pack(side="left", padx=(0, 4), expand=True, fill="x")
    _ok_button("Edit", _edit_profile).pack(side="left", padx=(0, 4), expand=True, fill="x")
    _ok_button("Delete", _delete_profile).pack(side="left", padx=(0, 4), expand=True, fill="x")

    tk.Label(
        profiles_section,
        text=(
            "Plugins and their settings stay shared across all profiles.\n"
            "Switching profiles restarts the app."
        ),
        background=theme_win_bg,
        foreground=C["label_playlist_warn_fg"],
        justify="left",
        font=ui_font(9),
    ).pack(anchor="w", padx=16, pady=(2, 4))

    about_section = tk.Frame(content, background=theme_win_bg)
    about_section.pack(fill="both", padx=8, pady=(0, 8))
    _section_header(about_section, "About")
    tk.Label(
        about_section,
        text="Author: d1pl",
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(12),
        anchor="w",
    ).pack(fill="x", pady=(0,4), padx=16)

    repo_label = tk.Label(
        about_section,
        text=f"Repo: {REPO_URL}",
        background=theme_win_bg,
        foreground=C["button_main_fg"],
        font=ui_font(12),
        anchor="w",
        cursor="hand2",
    )
    repo_label.pack(fill="x", padx=16, pady=(0, 4))
    repo_label.bind("<Button-1>", lambda _event: _open_repo())

    tk.Label(
        about_section,
        text=f"Version: {__version__}",
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(12),
        anchor="w",
    ).pack(fill="x", pady=(0,4), padx=16)

    tk.Label(
        about_section,
        text="Support:",
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(12),
        anchor="w",
    ).pack(fill="x", pady=(0,4), padx=16)

    tk.Label(
        about_section,
        text="monero: soon",
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(12),
        anchor="w",
    ).pack(fill="x", pady=(0,4), padx=16)

    if center_var_value == 1:
        center_window(win)



