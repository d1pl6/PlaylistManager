# Environment quirks: Wayland and the system tray

How the app behaves under Wayland compositors and with the optional pystray
tray. The commands and the `playlistmanager` wrapper are covered in `CLI.MD`.

## Wayland

- **Global hotkeys are impossible on native Wayland by design** — the compositor owns input; apps cannot grab global keys. pynput's global listener is X11-only: on a native Wayland session it receives nothing, and under XWayland it only sees keys while an XWayland client has focus. `KeybindController._start_global_listener` logs a warning on Wayland sessions. The supported path is compositor shortcuts bound to `playlistmanager -a N` (see CLI.MD "Global install (for compositor shortcuts)").
- **Tray**: only the `_appindicator` backend (StatusNotifierItem over DBus) works under Wayland — the `_gtk`/`_xorg` backends dock into an X11 system tray that doesn't exist there. TrayService sets `available=False` for those backends on Wayland sessions so the Settings checkbox can't falsely enable hide-to-tray; don't remove that guard. On GNOME the AppIndicator/KStatusNotifierItem extension is required.
- `is_wayland_session()` (`utils/platform.py`) is True when `WAYLAND_DISPLAY` is set **or** `XDG_SESSION_TYPE == wayland` — deliberately includes stale env inherited by X11 apps launched from a Wayland session.
- **Restore-from-tray is best-effort** (compositors may refuse raise/focus — focus-stealing prevention), and a maximized window may restore unmaximized after hide-to-tray. Maximize-on-restore is intentionally unimplemented (old plan item, resolved by removal) — do not re-add it.

## Tray (pystray)

- `pystray` is optional: `TrayService.available` (`services/tray.py`) is False when it's missing or the icon can't be built, and the app runs normally. `_start_tray` is guarded so a pystray exception cannot abort app startup.
- **Gtk-family backends** (`pystray._appindicator` / `pystray._gtk`) register icon/menu updates as GLib idle callbacks, so `Icon.run_detached()` never shows the icon (no GLib mainloop starts). TrayService runs `Icon.run()` in a daemon thread for those (`_is_gtk_backend()`); `win32`/`xorg`/`darwin` use `run_detached()`. Keep that branch.
- pystray 0.19.x renamed `HAS_DEFAULT` → `HAS_DEFAULT_ACTION`; `_has_default_action()` resolves both via `getattr` fallback — keep that pattern.
- Tray callbacks fire on the tray backend thread — the caller must marshal them to the tkinter main thread via `root.after(0, ...)`.
- `start_in_tray` (setting or `--start-in-tray`) withdraws the window **before any setup work, so it never maps even once** (a late withdraw would flash the window — not "start in tray"); the tray must actually be running (`App._tray_service` set) or the window is deiconified — a missing/unstartable tray leaves the app reachable. Fullscreen wins over start-in-tray when both are requested. `utils.window.center_window` guards unmapped windows (`winfo_width() <= 1` → requested size) so a withdrawn boot still centres correctly.