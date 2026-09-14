import logging
import tkinter as tk
import tkinter.font
import webbrowser
from tkinter import messagebox, ttk

from _version import __version__
from services import profile_store
from services.scrobble import SCROBBLE_NONE, split_platforms
from ui.profiles_ui import (
    show_create_profile_dialog,
    show_edit_buckets_dialog,
    show_rename_profile_dialog,
)
from ui.scrollable import ScrollableFrame
from ui.settings_theme_ui import show_theme_dialog
from utils.config import (
    GRID_SORT_DEFAULT_DIRECTION,
    GRID_SORT_DEFAULT_KEY,
    GRID_SORT_KEY_LABELS,
    REMOVE_PLAYLIST_DEFAULT_MODE,
    REMOVE_PLAYLIST_MODE_LABELS,
    THUMBNAIL_MODES,
    get_setting,
    get_setting_value,
    set_setting,
    set_setting_value,
)
from utils.i18n import available_languages, language_code_for, language_display, tr
from utils.scaling import UI_SCALE_PRESETS, px, ui_font
from utils.platform import is_wayland_session
from utils.theme import C, btn_colors, hover_bg
from utils.thumbnail import ThumbnailService
from utils.window import center_window, fit_window_to_screen, resize_window

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
    on_pin_buttons_change=None,
    on_sort_change=None,
    on_scrobble_keybind_change=None,
    on_restart_app=None,
    plugin_availability=None,
    platform_display_names=None,
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
        platform_display_names: optional mapping of platform id -> display
            name, used for the per-platform scrobble-source list labels.
            Falls back to the raw id when a platform is absent.
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

    win.geometry("420x640")
    win.minsize(350, 400)

    tk.Label(
        win,
        text=tr("settings.title"),
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
    remember_var_value = 1 if get_setting("remember_geometry", fallback=True) else 0
    fullscreen_var_value = 1 if get_setting("fullscreen", fallback=False) else 0
    global_var_value = 1 if get_setting("global_listener", fallback=True) else 0
    tray_var_value = 1 if get_setting("hide_to_tray", fallback=False) else 0
    start_tray_var_value = 1 if get_setting("start_in_tray", fallback=False) else 0
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
    font_family_value = get_setting_value("font", "family", "")

    update_var = tk.IntVar(value=update_var_value)
    center_var = tk.IntVar(value=center_var_value)
    resize_var = tk.IntVar(value=resize_var_value)
    remember_var = tk.IntVar(value=remember_var_value)
    fullscreen_var = tk.IntVar(value=fullscreen_var_value)
    global_var = tk.IntVar(value=global_var_value)
    tray_var = tk.IntVar(value=tray_var_value)

    app_section = tk.Frame(content, background=theme_win_bg)
    app_section.pack(fill="both", padx=8, pady=(0, 8))
    _section_header(app_section, tr("settings.app_behavior"))

    tk.Checkbutton(
        app_section,
        text=tr("settings.check_updates_startup"),
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
        update_btn.configure(state="disabled", text=tr("settings.checking"))

        def _on_complete(available, error):
            try:
                if available:
                    update_btn.configure(state="normal", text=tr("settings.check_updates_now"))
                elif error:
                    update_btn.configure(
                        state="normal",
                        text=tr("settings.check_failed_retry"),
                    )
                else:
                    update_btn.configure(state="normal", text=tr("settings.up_to_date"))
            except tk.TclError:
                return
            try:
                win.after(
                    4000,
                    lambda: update_btn.configure(text=tr("settings.check_updates_now")),
                )
            except tk.TclError:
                pass

        on_check_updates_now(on_done=_on_complete)

    update_btn = tk.Button(
        app_section,
        text=tr("settings.check_updates_now"),
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
        text=tr("settings.center_windows"),
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

    tk.Checkbutton(
        app_section,
        text=tr("settings.remember_window_geometry"),
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=lambda: _toggle_setting("remember_geometry", remember_var),
        variable=remember_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    def _on_reset_window_geometry():
        # Forget the remembered size/position and apply the default
        # geometry live: resize to fit the current content, then center
        # on screen.  The persisted value is cleared so the next launch
        # starts with the same default; remember_geometry is untouched.
        try:
            set_setting_value("window", "geometry", "")
        except Exception as e:
            logger.error("Failed to reset window geometry: %s", e)
        try:
            resize_window(parent)
            center_window(parent)
        except Exception:
            pass

    tk.Button(
        app_section,
        text=tr("settings.reset_window_geometry"),
        cursor="hand2",
        relief="raised",
        bd=0,
        highlightthickness=0,
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        font=ui_font(10),
        command=_on_reset_window_geometry,
    ).pack(fill="x", pady=(0, 5), padx=16)

    def _on_fullscreen_toggle():
        _toggle_setting("fullscreen", fullscreen_var)
        # Live toggle: apply to the main window (dialog parent).  Works
        # through XWayland; F11 inside the app does the same.
        try:
            parent.attributes("-fullscreen", bool(fullscreen_var.get()))
        except Exception:
            pass

    tk.Checkbutton(
        app_section,
        text=tr("settings.start_fullscreen"),
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_fullscreen_toggle,
        variable=fullscreen_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    def _on_auto_resize_toggle():
        _toggle_setting("auto_resize", resize_var)
        if on_auto_resize_toggle is not None:
            on_auto_resize_toggle(resize_var.get() == 1)

    tk.Checkbutton(
        app_section,
        text=tr("settings.auto_resize"),
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
        text=tr("settings.global_key_listener"),
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
            text=tr("settings.wayland_global_hint"),
            background=theme_win_bg,
            foreground=theme_label_fg,
            font=ui_font(9),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=16)

    def _on_tray_toggle():
        _toggle_setting("hide_to_tray", tray_var)
        if on_tray_toggle is not None:
            on_tray_toggle(tray_var.get() == 1)

    tray_ck = tk.Checkbutton(
        app_section,
        text=tr("settings.hide_in_tray"),
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

    start_tray_var = tk.IntVar(value=start_tray_var_value)
    start_tray_ck = tk.Checkbutton(
        app_section,
        text=tr("settings.start_in_tray"),
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=lambda: _toggle_setting("start_in_tray", start_tray_var),
        variable=start_tray_var,
    )
    start_tray_ck.pack(fill="both", pady=(0,5), padx=16)
    if not getattr(tray_available, "available", False):
        start_tray_ck.configure(state="disabled", cursor="arrow")

    pin_buttons_var = tk.BooleanVar(value=get_setting("show_pin_buttons", fallback=True))
    pin_buttons_check = tk.Checkbutton(
        app_section,
        text=tr("settings.pin_unpin_buttons"),
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        variable=pin_buttons_var,
    )
    pin_buttons_check.pack(fill="both", pady=(0, 5), padx=16)

    def _on_pin_buttons_toggle():
        _toggle_setting("show_pin_buttons", pin_buttons_var)
        if on_pin_buttons_change:
            try:
                on_pin_buttons_change(bool(pin_buttons_var.get()))
            except Exception as e:
                logger.error("Failed to apply show_pin_buttons change: %s", e)

    pin_buttons_check.config(command=_on_pin_buttons_toggle)

    # --- Sort playlists: key combobox + direction arrow ----------------
    sort_row = tk.Frame(app_section, background=theme_win_bg)
    sort_row.pack(fill="both", pady=(0, 5), padx=16)
    tk.Label(
        sort_row,
        text=tr("settings.sort_playlists_by"),
        background=theme_win_bg,
        fg=C["label_def_fg"],
        font=ui_font(12),
    ).pack(side="left")

    # Combo labels are translated at creation time; the underlying sort
    # keys (name/platform/added/used) stay untranslated data, so both the
    # displayed values and the label->key reverse-map share one mapping.
    _SORT_LABELS = {
        k: tr(f"settings.sort_key_{k}") for k in GRID_SORT_KEY_LABELS
    }
    sort_key_var = tk.StringVar(
        value=_SORT_LABELS.get(
            get_setting_value("grid_sort", "key", GRID_SORT_DEFAULT_KEY),
            _SORT_LABELS[GRID_SORT_DEFAULT_KEY],
        )
    )
    sort_combo = ttk.Combobox(
        sort_row,
        textvariable=sort_key_var,
        values=list(_SORT_LABELS.values()),
        state="readonly",
        width=12,
    )
    sort_combo.pack(side="left", padx=(8, 4))
    sort_arrow = tk.Button(
        sort_row,
        text="\u2191",
        cursor="hand2",
        highlightthickness=0,
        relief="raised",
        font=ui_font(12),
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
    )
    sort_arrow.pack(side="left")

    def _sort_direction() -> str:
        return get_setting_value(
            "grid_sort", "direction", GRID_SORT_DEFAULT_DIRECTION
        )

    def _apply_sort() -> None:
        if callable(on_sort_change):
            try:
                on_sort_change()
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to re-sort grid: %s", e)

    def _on_sort_key_selected(_event=None) -> None:
        label = sort_key_var.get()
        key = next(
            (k for k, v in _SORT_LABELS.items() if v == label),
            GRID_SORT_DEFAULT_KEY,
        )
        set_setting_value("grid_sort", "key", key)
        _apply_sort()

    def _on_sort_arrow(_event=None) -> None:
        new_dir = "desc" if _sort_direction() == "asc" else "asc"
        set_setting_value("grid_sort", "direction", new_dir)
        sort_arrow.config(text="\u2191" if new_dir == "asc" else "\u2193")
        _apply_sort()

    sort_combo.bind("<<ComboboxSelected>>", _on_sort_key_selected)
    sort_arrow.config(command=_on_sort_arrow)
    sort_arrow.config(text="\u2191" if _sort_direction() == "asc" else "\u2193")

    dupcheck_section = tk.Frame(content, background=theme_win_bg)
    dupcheck_section.pack(fill="both", padx=8, pady=(0, 8))
    _section_header(dupcheck_section, tr("settings.duplicate_check"))

    def _on_dup_check_toggle():
        _toggle_setting("duplicate_check", dup_var)

    dup_var = tk.IntVar(value=1 if get_setting("duplicate_check", fallback=False) else 0)
    tk.Checkbutton(
        dupcheck_section,
        text=tr("settings.extra_duplicate_check"),
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_dup_check_toggle,
        variable=dup_var,
    ).pack(fill="both", pady=(0,0), padx=16)

    tk.Label(
        dupcheck_section,
        text=tr("settings.dup_ask_hint"),
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(9),
        anchor="w",
    ).pack(fill="x", padx=16)

    # --- Title similarity threshold slider -----------------------------
    def _clamp_step(value: float, lo: float, hi: float, step: float) -> float:
        return round(min(max(value, lo), hi) / step) * step

    try:
        _threshold_now = float(
            get_setting_value("duplicate_check", "title_threshold", "0.85")
        )
    except (TypeError, ValueError):
        _threshold_now = 0.85
    _threshold_now = _clamp_step(_threshold_now, 0.50, 1.00, 0.05)

    def _persist_setting(option: str, var, fmt) -> None:
        try:
            set_setting_value("duplicate_check", option, fmt(var.get()))
        except Exception as e:
            logger.error("Failed to write duplicate_check %s setting: %s", option, e)

    threshold_var = tk.DoubleVar(value=_threshold_now)
    threshold_row = tk.Frame(dupcheck_section, background=theme_check_bg)
    threshold_row.pack(fill="both", pady=(0, 2), padx=16)
    tk.Label(
        threshold_row,
        text=tr("settings.title_similarity"),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    ).pack(side="left", pady=(0, 5))
    threshold_val = tk.Label(
        threshold_row,
        text=f"{_threshold_now:.2f}",
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    )
    threshold_val.pack(side="right", pady=(0, 5))

    def _on_threshold_drag(value: str) -> None:
        threshold_val.configure(text=f"{float(value):.2f}")

    def _persist_threshold(_event=None) -> None:
        _persist_setting("title_threshold", threshold_var, lambda v: f"{float(v):.2f}")

    threshold_scale = tk.Scale(
        threshold_row,
        from_=0.50,
        to=1.00,
        resolution=0.05,
        orient="horizontal",
        showvalue=0,
        length=px(150),
        sliderlength=px(14),
        variable=threshold_var,
        background=theme_check_bg,
        troughcolor=C["scrollable_frame_bg"],
        activebackground=theme_check_select,
        highlightthickness=0,
        bd=0,
        command=_on_threshold_drag,
    )
    threshold_scale.pack(side="left", fill="x", expand=True, pady=(0, 5), padx=10)
    # Persist when the drag ends (mouse or keyboard), not on every tick:
    # set_setting_value rewrites the whole INI, so firing per mouse-move
    # would thrash the file.
    threshold_scale.bind("<ButtonRelease-1>", _persist_threshold)
    threshold_scale.bind("<KeyRelease>", _persist_threshold)

    tk.Label(
        dupcheck_section,
        text=tr("settings.match_score_hint"),
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(9),
        anchor="w",
        padx=16,
    ).pack(fill="x", padx=0, pady=(0, 3))

    # --- Duration tolerance slider --------------------------------------
    try:
        _tolerance_now = int(
            get_setting_value("duplicate_check", "duration_tolerance", "5")
        )
    except (TypeError, ValueError):
        _tolerance_now = 5
    _tolerance_now = int(_clamp_step(float(_tolerance_now), 0.0, 60.0, 1.0))

    tolerance_var = tk.IntVar(value=_tolerance_now)
    tolerance_row = tk.Frame(dupcheck_section, background=theme_check_bg)
    tolerance_row.pack(fill="both", pady=(0, 2), padx=16)
    tk.Label(
        tolerance_row,
        text=tr("settings.duration_tolerance"),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    ).pack(side="left", pady=(0, 5))
    tolerance_val = tk.Label(
        tolerance_row,
        text=tr("settings.duration_value", n=_tolerance_now),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    )
    tolerance_val.pack(side="right", pady=(0, 5))

    def _on_tolerance_drag(value: str) -> None:
        tolerance_val.configure(text=tr("settings.duration_value", n=int(float(value))))

    def _persist_tolerance(_event=None) -> None:
        _persist_setting("duration_tolerance", tolerance_var, lambda v: str(int(v)))

    tolerance_scale = tk.Scale(
        tolerance_row,
        from_=0,
        to=60,
        resolution=1,
        orient="horizontal",
        showvalue=0,
        length=px(150),
        sliderlength=px(14),
        variable=tolerance_var,
        background=theme_check_bg,
        troughcolor=C["scrollable_frame_bg"],
        activebackground=theme_check_select,
        highlightthickness=0,
        bd=0,
        command=_on_tolerance_drag,
    )
    tolerance_scale.pack(side="left", fill="x", expand=True, pady=(0, 5), padx=10)
    tolerance_scale.bind("<ButtonRelease-1>", _persist_tolerance)
    tolerance_scale.bind("<KeyRelease>", _persist_tolerance)

    tk.Label(
        dupcheck_section,
        text=tr("settings.duration_hint"),
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(9),
        anchor="w",
        padx=16,
    ).pack(fill="x", padx=0, pady=(0, 3))

    def _do_dup_check_now():
        if not callable(on_check_duplicates_now):
            return
        dup_btn.configure(state="disabled", text=tr("settings.scanning"))

        def _on_complete(found, error):
            try:
                if error:
                    dup_btn.configure(state="normal", text=tr("settings.scan_failed_retry"))
                elif found:
                    dup_btn.configure(state="normal", text=f"{found} found!")
                else:
                    dup_btn.configure(state="normal", text=tr("settings.no_duplicates_found"))
            except tk.TclError:
                return
            try:
                win.after(
                    4000,
                    lambda: dup_btn.configure(text=tr("settings.check_duplicates_now")),
                )
            except tk.TclError:
                pass

        on_check_duplicates_now(on_done=_on_complete)

    dup_btn = tk.Button(
        dupcheck_section,
        text=tr("settings.check_duplicates_now"),
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
    _section_header(appearance_section, tr("settings.appearance"))

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
        text=tr("settings.show_log_row"),
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
        text=tr("settings.show_playlist_stats"),
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_playlist_stats_toggle,
        variable=playlist_stats_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    # --- Song removal: ask before the showcase ✕ deletes a song.  The
    # removal is always a full one (platform + local row + scrobble).
    confirm_song_remove_var = tk.IntVar(
        value=1 if get_setting("confirm_on_song_remove", fallback=True) else 0
    )
    tk.Checkbutton(
        appearance_section,
        text=tr("settings.ask_before_song_remove"),
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=lambda: _toggle_setting(
            "confirm_on_song_remove", confirm_song_remove_var
        ),
        variable=confirm_song_remove_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    # --- Playlist removal: ask before a playlist card is closed; when
    # not asking, [remove_playlist] default decides (Remove / Keep DB).
    confirm_playlist_remove_var = tk.IntVar(
        value=1 if get_setting("confirm_on_playlist_remove", fallback=True) else 0
    )

    def _on_confirm_playlist_remove_toggle():
        _toggle_setting("confirm_on_playlist_remove", confirm_playlist_remove_var)
        # The default-mode row is moot while the dialog is on.
        try:
            playlist_remove_combo.configure(
                state="readonly" if confirm_playlist_remove_var.get() == 0 else "disabled"
            )
        except tk.TclError:
            pass

    tk.Checkbutton(
        appearance_section,
        text=tr("settings.ask_before_playlist_remove"),
        cursor="hand2",
        selectcolor=theme_check_select,
        **checkbutton_style,
        font=ui_font(12),
        command=_on_confirm_playlist_remove_toggle,
        variable=confirm_playlist_remove_var,
    ).pack(fill="both", pady=(0,5), padx=16)

    playlist_remove_default_value = get_setting_value(
        "remove_playlist", "default", REMOVE_PLAYLIST_DEFAULT_MODE
    )
    _REMOVAL_LABELS = {
        k: tr(f"settings.removal_mode_{k}") for k in REMOVE_PLAYLIST_MODE_LABELS
    }
    playlist_remove_default_label = _REMOVAL_LABELS.get(
        playlist_remove_default_value,
        _REMOVAL_LABELS[REMOVE_PLAYLIST_DEFAULT_MODE],
    )

    def _on_playlist_remove_default_change(label_value):
        chosen = next(
            (k for k, v in _REMOVAL_LABELS.items() if v == label_value),
            REMOVE_PLAYLIST_DEFAULT_MODE,
        )
        try:
            set_setting_value("remove_playlist", "default", chosen)
        except Exception as e:
            logger.error("Failed to save playlist-removal default: %s", e)

    playlist_remove_default_row = tk.Frame(appearance_section, background=theme_check_bg)
    playlist_remove_default_row.pack(fill="both", pady=(0,5), padx=16)
    tk.Label(
        playlist_remove_default_row,
        text=tr("settings.default_playlist_removal"),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    ).pack(side="left", pady=(0,5))

    playlist_remove_var = tk.StringVar(value=playlist_remove_default_label)
    playlist_remove_combo = ttk.Combobox(
        playlist_remove_default_row,
        textvariable=playlist_remove_var,
        cursor="hand2",
        values=tuple(_REMOVAL_LABELS.values()),
        state="disabled" if confirm_playlist_remove_var.get() == 1 else "readonly",
        width=18,
        font=ui_font(12),
    )
    playlist_remove_combo.pack(side="left", pady=(0,5))
    playlist_remove_combo.bind(
        "<<ComboboxSelected>>",
        lambda e: _on_playlist_remove_default_change(playlist_remove_var.get()),
    )

    showcase_row = tk.Frame(appearance_section, background=theme_check_bg)
    showcase_row.pack(fill="both", pady=(0,5), padx=16)
    tk.Label(
        showcase_row,
        text=tr("settings.show_last_n_added"),
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
        text=tr("settings.zero_off_hint"),
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
        text=tr("settings.ui_scale"),
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
        text=tr("settings.restart_to_apply"),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(9),
    ).pack(side="left", pady=(0,5))

    # --- Font family ---
    def _on_font_family_change(value: str) -> None:
        try:
            set_setting_value("font", "family", value)
        except Exception as e:
            logger.error("Failed to write font family setting: %s", e)

    font_family_row = tk.Frame(appearance_section, background=theme_check_bg)
    font_family_row.pack(fill="both", pady=(0,5), padx=16)
    tk.Label(
        font_family_row,
        text=tr("settings.font"),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    ).pack(side="left", pady=(0,5))

    # Build the family list: "(Default)" first, then every Tk-known family
    # sorted case-insensitively.
    _all_families = sorted(set(tkinter.font.families()), key=str.casefold)
    font_family_values = ["(Default)"] + list(_all_families)
    # If user has a custom family selected, make sure it appears in the list.
    if font_family_value and font_family_value not in _all_families:
        font_family_values.insert(0, font_family_value)

    font_family_var = tk.StringVar(
        value=font_family_value if font_family_value else "(Default)"
    )
    font_family_combo = ttk.Combobox(
        font_family_row,
        textvariable=font_family_var,
        cursor="hand2",
        values=font_family_values,
        state="readonly",
        width=18,
        font=ui_font(12),
    )
    font_family_combo.pack(side="left", pady=(0,5))
    font_family_combo.bind(
        "<<ComboboxSelected>>",
        lambda e: _on_font_family_change(
            "" if font_family_var.get() == "(Default)" else font_family_var.get()
        ),
    )
    tk.Label(
        font_family_row,
        text=tr("settings.restart_to_apply"),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(9),
    ).pack(side="left", pady=(0,5))

    # --- Language (restart to apply) ---
    def _on_language_change(value: str) -> None:
        try:
            set_setting_value("language", "lang", language_code_for(value))
        except Exception as e:
            logger.error("Failed to write language setting: %s", e)

    _lang_codes = available_languages()
    language_row = tk.Frame(appearance_section, background=theme_check_bg)
    language_row.pack(fill="both", pady=(0, 5), padx=16)
    tk.Label(
        language_row,
        text=tr("settings.language"),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    ).pack(side="left", pady=(0,5))

    language_var = tk.StringVar(
        value=language_display(get_setting_value("language", "lang", "en"))
    )
    language_combo = ttk.Combobox(
        language_row,
        textvariable=language_var,
        cursor="hand2",
        values=tuple(language_display(c) for c in _lang_codes),
        state="readonly",
        width=12,
        font=ui_font(12),
    )
    language_combo.pack(side="left", pady=(0,5))
    language_combo.bind(
        "<<ComboboxSelected>>",
        lambda e: _on_language_change(language_var.get()),
    )
    tk.Label(
        language_row,
        text=tr("settings.restart_to_apply"),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(9),
    ).pack(side="left", pady=(0,5))

    # --- Thumbnails (data-saver modes) ---
    # Mode keys match utils.config.THUMBNAIL_MODES; the combo shows
    # friendly labels.  The mode is read per fetch, so a change applies
    # live (existing in-memory/disk entries are simply reused as-is).
    _THUMBNAIL_LABELS = tuple(
        (mode, tr(f"settings.thumbnail_mode_{mode}")) for mode in THUMBNAIL_MODES
    )

    def _on_thumbnail_mode_change(value: str) -> None:
        key = value
        for k, label in _THUMBNAIL_LABELS:
            if label == value:
                key = k
                break
        try:
            set_setting_value("thumbnails", "mode", key)
        except Exception as e:
            logger.error("Failed to write thumbnail mode setting: %s", e)

    thumb_row = tk.Frame(appearance_section, background=theme_check_bg)
    thumb_row.pack(fill="both", pady=(0,5), padx=16)
    tk.Label(
        thumb_row,
        text=tr("settings.data_saver"),
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(12),
    ).pack(side="left", pady=(0,5))

    thumb_mode_value = get_setting_value("thumbnails", "mode", "off")
    if thumb_mode_value not in THUMBNAIL_MODES:
        thumb_mode_value = "off"
    _thumb_key_to_label = dict(_THUMBNAIL_LABELS)
    thumb_labels = tuple(_thumb_key_to_label.values())
    thumb_var = tk.StringVar(value=_thumb_key_to_label[thumb_mode_value])
    thumb_combo = ttk.Combobox(
        thumb_row,
        textvariable=thumb_var,
        cursor="hand2",
        values=thumb_labels,
        state="readonly",
        width=22,
        font=ui_font(12),
    )
    thumb_combo.pack(side="left", pady=(0,5))
    thumb_combo.bind(
        "<<ComboboxSelected>>",
        lambda e: _on_thumbnail_mode_change(thumb_var.get()),
    )

    cache_size_var = tk.StringVar(value="")

    def _refresh_cache_size_label() -> None:
        try:
            size = ThumbnailService.disk_cache_size()
        except Exception:
            size = 0
        if size > 0:
            text = (
                f"{size / (1024 * 1024):.1f} MB"
                if size >= 1024 * 1024
                else f"{size / 1024:.0f} KB"
            )
        else:
            text = tr("settings.cache_empty")
        cache_size_var.set(f"({text})")

    tk.Label(
        thumb_row,
        textvariable=cache_size_var,
        background=theme_check_bg,
        foreground=theme_check_fg,
        font=ui_font(9),
    ).pack(side="left", pady=(0,5), padx=(6, 0))
    _refresh_cache_size_label()

    tk.Label(
        appearance_section,
        text=tr("settings.thumbnails_modes_desc"),
        background=theme_win_bg,
        foreground=theme_label_fg,
        justify="left",
        font=ui_font(9),
    ).pack(anchor="w", padx=16, pady=(0, 6))

    def _on_clear_cache() -> None:
        if not messagebox.askyesno(
            tr("settings.clear_thumbnail_cache_title"),
            tr("settings.clear_thumbnail_cache_ask"),
            parent=win,
        ):
            return
        try:
            ThumbnailService.clear_disk_cache()
        except Exception as e:
            logger.error("Failed to clear thumbnail cache: %s", e)
            return
        _refresh_cache_size_label()

    tk.Button(
        appearance_section,
        text=tr("settings.clear_thumbnail_cache"),
        cursor="hand2",
        relief="raised",
        bd=0,
        highlightthickness=0,
        **btn_colors(C["button_main_bg"], C["button_main_fg"]),
        font=ui_font(10),
        command=_on_clear_cache,
    ).pack(fill="x", pady=(0, 5), padx=16)

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
        text=tr("settings.card_columns"),
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

    tk.Button(
        appearance_section,
        text=tr("settings.theme_settings"),
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
            text=tr("settings.like_button"),
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
            text=tr("settings.scrobble_added_songs"),
            variable=scrobble_on_add_var,
            cursor="hand2",
            selectcolor=theme_check_select,
            **checkbutton_style,
            font=ui_font(11),
        )
        scrobble_on_add_check.pack(anchor="w", padx=16, pady=4)

        # -- Per-platform scrobble sources ---------------------------------
        # Only visible when the master gate is on.  Only installed music
        # platforms are listed (Last.fm is the scrobble TARGET, never a
        # source).  Storage semantics: empty = all installed (historical
        # default), "none" = nothing, otherwise an explicit comma list.
        scrobble_plat_area = tk.Frame(lastfm_section, background=theme_check_bg)
        scrobble_plat_vars: dict = {}
        raw_plat = get_setting_value("scrobble", "platforms", "")
        _is_none = raw_plat.strip().lower() == SCROBBLE_NONE
        _stored_ids = set() if _is_none else set(split_platforms(raw_plat))
        _music_platforms = sorted(
            (p for p in installed if p != "lastfm"),
            key=lambda pid: (platform_display_names or {}).get(pid, pid).lower(),
        )

        if _music_platforms:
            tk.Label(
                scrobble_plat_area,
                text=tr("settings.scrobble_platforms_from"),
                background=theme_check_bg,
                foreground=theme_check_fg,
                font=ui_font(11),
            ).pack(anchor="w", padx=4, pady=(6, 0))

            _plat_scf = ScrollableFrame(
                scrobble_plat_area,
                bg=theme_check_bg,
                max_viewport_height=px(100),
            )
            _plat_scf.pack(fill="both", padx=0, pady=(2, 4))
            _plat_scf.update_scrollregion()
            _plat_scf.style_scrollbar(
                hover_bg(theme_check_fg), theme_check_bg,
            )

            for _pid in _music_platforms:
                _pname = (platform_display_names or {}).get(_pid, _pid)
                _var = tk.BooleanVar(
                    value=(not _stored_ids and not _is_none) or _pid in _stored_ids,
                )
                _cb = tk.Checkbutton(
                    _plat_scf.content,
                    text=_pname,
                    variable=_var,
                    cursor="hand2",
                    selectcolor=theme_check_select,
                    **checkbutton_style,
                    font=ui_font(11),
                    command=lambda: set_setting_value(
                        "scrobble",
                        "platforms",
                        ",".join(
                            p for p in _music_platforms
                            if scrobble_plat_vars[p].get()
                        ) or SCROBBLE_NONE,
                    ),
                )
                _cb.pack(anchor="w", padx=4, pady=1)
                scrobble_plat_vars[_pid] = _var
        else:
            tk.Label(
                scrobble_plat_area,
                text=tr("settings.install_integration_first"),
                background=theme_check_bg,
                foreground=theme_check_fg,
                font=ui_font(11),
            ).pack(anchor="w", padx=4, pady=(6, 4))

        def _apply_scrobble_list_visibility():
            """Pack / un-pack the platform list based on the master gate."""
            try:
                if scrobble_on_add_var.get():
                    scrobble_plat_area.pack(fill="x", padx=0, pady=(0, 4))
                else:
                    scrobble_plat_area.pack_forget()
            except tk.TclError:
                pass

        def on_scrobble_on_add_toggle():
            _toggle_setting("scrobble_on_add", scrobble_on_add_var)
            _apply_scrobble_list_visibility()

        scrobble_on_add_check.config(command=on_scrobble_on_add_toggle)
        _apply_scrobble_list_visibility()

        # Scrobble keybind capture row
        scrobble_keybind_row = tk.Frame(lastfm_section, background=theme_check_bg)
        scrobble_keybind_row.pack(fill="x", padx=16, pady=4)

        tk.Label(
            scrobble_keybind_row,
            text=tr("settings.scrobble_keybind"),
            background=theme_check_bg,
            foreground=theme_check_fg,
            font=ui_font(11),
        ).pack(side="left")

        scrobble_keybind_display = tk.Label(
            scrobble_keybind_row,
            text=get_setting_value("scrobble_keybind", "keybind") or tr("settings.keybind_none"),
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
                scrobble_keybind_display.config(text=combo_str or tr("settings.keybind_none"))
                if on_scrobble_keybind_change is not None:
                    try:
                        on_scrobble_keybind_change()
                    except Exception as e:
                        logger.error("Failed to re-register scrobble keybind: %s", e)

            keybind_controller.start_recording(on_combo)

        tk.Button(
            scrobble_keybind_row,
            text=tr("settings.record"),
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
            text=tr("settings.clear"),
            cursor="hand2",
            **btn_colors(C["button_main_bg"], C["button_main_fg"]),
            font=ui_font(10),
            highlightthickness=0,
            relief="raised",
            bd=0,
            command=lambda: (
                set_setting_value("scrobble_keybind", "keybind", ""),
                scrobble_keybind_display.config(text=tr("settings.keybind_none")),
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
            text=tr("settings.capture_song_via"),
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
            text=tr("settings.soundcloud_mode_desc"),
            background=theme_win_bg,
            foreground=theme_label_fg,
            justify="left",
            font=ui_font(9),
        ).pack(anchor="w", padx=16, pady=(0, 6))

    # -- Profiles section ----------------------------------------------
    profiles_section = tk.Frame(content, background=theme_win_bg)
    profiles_section.pack(fill="both", padx=8, pady=(0, 8))
    _section_header(profiles_section, tr("settings.profiles"))

    def _restart_prompt(on_commit=None) -> None:
        """Ask to restart the app so the profile change applies.

        *on_commit* (optional callable) runs only if the user chooses to
        restart ("Restart now?" -> Yes).  It should persist the profile
        change (e.g. ``set_active``) that must NOT be applied unless the
        app actually restarts - so a user who declines the restart is not
        left with an un-deletable active profile.
        """
        if not messagebox.askyesno(
            tr("settings.profiles"),
            tr("settings.profile_restart_ask"),
            parent=win,
        ):
            return
        if callable(on_commit):
            try:
                on_commit()
            except ValueError as e:
                messagebox.showerror(tr("settings.profile"), str(e), parent=win)
                return
        if callable(on_restart_app):
            win.after(50, on_restart_app)

    active_name = profile_store.active_profile()

    active_row = tk.Frame(profiles_section, background=theme_win_bg)
    active_row.pack(fill="x", padx=16, pady=(0, 4))
    tk.Label(
        active_row,
        text=tr("settings.active_profile"),
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
            tr("settings.switch_profile"),
            tr("settings.switch_profile_ask", sel=sel),
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
        text=tr("settings.profiles_label"),
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
                tr("settings.delete_profile_title"),
                tr("settings.default_profile_not_deletable"),
                parent=win,
            )
            return
        if not messagebox.askyesno(
            tr("settings.delete_profile_title"),
            tr("settings.delete_profile_ask", sel=sel),
            parent=win,
        ):
            return
        try:
            profile_store.delete(sel)
        except ValueError as e:
            messagebox.showerror(tr("settings.delete_profile_title"), str(e), parent=win)
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
    _ok_button(tr("common.add"), _add_profile).pack(side="left", padx=(0, 4), expand=True, fill="x")
    _ok_button(tr("settings.rename"), _rename_profile).pack(side="left", padx=(0, 4), expand=True, fill="x")
    _ok_button(tr("settings.edit"), _edit_profile).pack(side="left", padx=(0, 4), expand=True, fill="x")
    _ok_button(tr("common.delete"), _delete_profile).pack(side="left", padx=(0, 4), expand=True, fill="x")

    tk.Label(
        profiles_section,
        text=tr("settings.profiles_note"),
        background=theme_win_bg,
        foreground=C["label_playlist_warn_fg"],
        justify="left",
        font=ui_font(9),
    ).pack(anchor="w", padx=16, pady=(2, 4))

    about_section = tk.Frame(content, background=theme_win_bg)
    about_section.pack(fill="both", padx=8, pady=(0, 8))
    _section_header(about_section, tr("settings.about"))
    tk.Label(
        about_section,
        text=tr("settings.author"),
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(12),
        anchor="w",
    ).pack(fill="x", pady=(0,4), padx=16)

    repo_label = tk.Label(
        about_section,
        text=tr("settings.repo", url=REPO_URL),
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
        text=tr("settings.version", version=__version__),
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(12),
        anchor="w",
    ).pack(fill="x", pady=(0,4), padx=16)

    tk.Label(
        about_section,
        text=tr("settings.support"),
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(12),
        anchor="w",
    ).pack(fill="x", pady=(0,4), padx=16)

    tk.Label(
        about_section,
        text=tr("settings.monero"),
        background=theme_win_bg,
        foreground=theme_label_fg,
        font=ui_font(12),
        anchor="w",
    ).pack(fill="x", pady=(0,4), padx=16)

    # Fit the window width to its content at the current font settings.
    # Wide font families (e.g. Adwaita Mono) render rows much wider than
    # the 420px default, and the scrollable canvas stretches its content
    # to the window width with no horizontal scrollbar - wider content
    # clips at the window edge.  Widen the window to the content's
    # requested width instead (capped so a huge font cannot force a
    # full-screen dialog); the 24px margin covers the scrollbar and frame
    # paddings.
    sf.update_scrollregion()
    win.update_idletasks()
    needed = sf.content.winfo_reqwidth() + 24
    if needed > 420:  # the default window width
        win.geometry(f"{min(needed, 900)}x{win.winfo_height()}")

    # Safety net: never open taller than the screen (the width cap above
    # already stays under it; a hand-edited geometry would not).  With the
    # settings dialog this is a no-op, but it guards future dialog edits.
    fit_window_to_screen(win, margin=60)

    if center_var_value == 1:
        # Center AFTER the size is final, or the fit above would leave
        # the widened window off-centre.
        center_window(win)



