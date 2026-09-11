# AGENTS.md

## Running

```bash
python main.py                # launch GUI (root launcher — also: python -m app)
python main.py --debug        # verbose logging
python main.py -a 1,2,3       # headless CLI: add current song to playlists #1,#2,#3 (also by name/range: -a "Chill Mix", -a 1-3)
python main.py -s             # headless CLI: scrobble current song to Last.fm (no playlist add)
python main.py -p add "<URL>" # headless CLI: register a playlist from its URL
python main.py -p del 1,"Chill Mix"  # headless CLI: remove locally (registry + DB; "all" for every playlist)
python main.py -p ref 2,3     # headless CLI: re-import all tracks from the platform
python main.py --login youtube_music  # headless CLI: authenticate (spotify takes --client-id/--client-secret/--refresh-token)
python main.py --logout spotify       # headless CLI: delete credentials + registry entries + db/<platform> + duplicate-queue/scrobble records
python main.py --install spotify      # headless CLI: download/install a platform plugin ("all" for every platform)
python main.py --uninstall spotify    # headless CLI: remove a platform plugin + credentials + registry + db (or "all")
python main.py --list         # headless CLI: numbered playlists (no display needed)
python main.py --data-saver   # launch GUI with thumbnails disabled ('max' mode; overrides the [thumbnails] setting for the run)
python main.py --fullscreen   # launch GUI fullscreen for this run (overrides the [fullscreen] setting)
python main.py --start-in-tray  # launch hidden in the system tray for this run (overrides the [start_in_tray] setting)
```

Every `python main.py ...` above is equivalent under `python -m app ...` (CLI.MD and the cli.py docstring use the `-m app` form). Platform names in the CLI are the **directory** names: `youtube_music`, `spotify`, `lastfm`, `soundcloud`, `deezer`.

Run from the repo root. The `app/` package uses bare imports (`from services.X import ...`, `from utils.X import ...`), so `app/` must be on `sys.path`. The root `main.py` inserts the repo root and delegates to `app/main.py`, which inserts its own directory — either way `app/` lands on `sys.path`. Any standalone script you write needs `sys.path.insert(0, "app")` first.

The global `playlistmanager` command (what compositor shortcuts bind to) is a **user-installed** wrapper or symlink to `main.py` — see CLI.MD "Global install (for compositor shortcuts)". `pip install .` does **not** install it: `pyproject.toml` has no `[project.scripts]`.

The `App` class name is an import trap: `app` is both the package (`app/`) and the module (`app/app.py`). `from app import App` only works when `app/` resolves to `app/app.py` as a plain module (`python main.py`); under `python -m app` the package sits in `sys.modules` without an `App` attribute. `app/main.py:_import_app()` branches on `__package__` to handle both — don't "simplify" it.

## Python version

Requires **Python 3.10+** (uses `X | Y` union type syntax).

## Dependencies

Core: `pillow`, `requests`, `platformdirs`, `flask`, `flask-cors`, `pynput`.
Optional: `ytmusicapi` (for YouTube Music integration), `pystray` (for the system tray / hide-to-tray; the code degrades gracefully when it's missing).

On Arch Linux, prefer `pacman` packages over `pip` for system-level deps (see README.MD).

## No lint / typecheck; pytest suite

There is no linter or type checker configured. There **is** a pytest suite (service layer, headless — see `tests/` and CONTRIBUTING.md "Testing"):

```bash
python -m pytest            # full suite (fast, ~4 s)
python -m pytest -q         # quiet
python -m compileall -q app/   # byte-compile everything
python -m app --list        # headless smoke test (CLI shares the GUI's service layer, needs no display)
```

Tests must stay headless-safe: `tests/conftest.py` redirects every profile-rooted path (`db/`, `cfg/`, `auth/`) to a throwaway temp tree and disables `profile_store._MIGRATE_LEGACY` before any app module import, so a test run never reads/writes/migrates the real data. The GUI requires a display; headless verification of services is possible by importing modules with `sys.path.insert(0, "app")` (avoid instantiating `tkinter.Tk` without one — but note `root.after(...)` from a worker thread raises `RuntimeError: main thread is not in main loop` unless the mainloop is actually running).

## Filesystem

The project lives on an **exFAT external drive** (`/mnt/ex_ssd/`). exFAT is case-insensitive — `TODO.MD`, `todo.md`, and `Todo.md` are all the same file. Always match existing filename casing when creating or referencing files.

Several files/dirs are **gitignored, so commits cannot carry them**: `AGENTS.md`, `opencode.jsonc`, `requirements.txt`, `0.3.0.md`, `0.3.0/` (step plans), `log/`, `.agents/`, `integrations/*` (every plugin dir — only `integrations/__init__.py` is tracked). (The `auth/`, `db/`, and `cfg/` dirs no longer exist in the repo — all app data lives under the platformdirs root, see Key data paths.) Edits to ignored paths never show in `git status`. `docs/*` is ignored by the committed `.gitignore`, but the working-tree copy (uncommitted change) **dropped that rule** — so `docs/`, `tests/`, and `CONTRIBUTING.md` currently show as untracked, and docs/ stays current only in the working tree (can drift from the committed tree). `pyproject.toml` is the tracked dependency manifest; `requirements.txt` is an untracked mirror. `CLI.MD` is the CLI reference (tracked; README.MD "Usage" links to it and it spells out the commands above plus the compositor-shortcut wrapper) — do not treat it as dead. `plan.md` no longer exists; its resolved items are folded into README.MD/docs.

## Architecture

Desktop tkinter GUI app that adds currently-playing songs to music-service playlists via hotkeys.

Cross-file relationships (who imports whom), the plugin.json contract, and end-to-end flow traces live in `docs/` — start at `docs/README.md`. Update the matching doc in the same commit as the code change (modules.md for moved/renamed modules, plugins.md for manifest keys, flows.md for changed call chains). This file keeps the operational rules; docs/ keeps the structural ones.

```
main.py                  # root launcher (inserts repo root, delegates to app.main)
theme.txt                # palette spec: short key names, 1:1 with THEME_MAP (see Theme system)
app/
  main.py                # CLI entry point + argparse (works because it's inside app/)
  cli.py                 # headless CLI implementation (-a, -p add/del/ref, --list, --login/--logout; see CLI.MD)
  __main__.py            # enables `python -m app`
  app.py                 # App class: bootstraps tkinter, auth, plugin discovery
  plugin_loader.py       # PluginRegistry — scans integrations/*/plugin.json, lazy class refs
  _version.py            # single source of version (pyproject.toml reads it)
  controllers/
    app_controller.py      # quit, refresh auth; force-quit dialog
    keybind_controller.py  # pynput global/local listener loop, dispatches flows (plugin-driven)
    keybind_registry.py    # KeybindRegistry — hotkey -> playlist mapping (UI-driven)
    playlist_controller.py # "add playlist" workflow orchestration (no widget code)
  services/
    integration.py         # BaseIntegration, IntegrationRegistry, BaseFlowController protocol
    profile_store.py       # profiles: active-profile metadata + the single path resolver (db_dir/cfg_dir/auth_dir — see Key data paths)
    duplicate_check.py     # near-duplicate matcher + shared add-path policy (resolve_near_duplicate)
    duplicate_queue.py     # db/extra.json: pending decisions, pair memory, error log
    scrobble_log.py        # db/scrobbles.json: the accepted scrobble timestamp per (platform, playlist_id, song_id) so remove-song deletes THAT scrobble
    playlist_sync.py       # PlaylistSyncService — threaded import/reload
    playlist_store.py      # reads/writes db/playlists.json (thread-safe, TTL cache)
    playlist_url.py        # parse_playlist_url — host table from manifests, shared URL shapes
    song_manager.py        # SQLite CRUD per-playlist (songs table)
    database.py            # DatabaseManager — per-playlist .db files under db/platform/, per-thread conn cache
    auth_setup.py          # YT/Spotify/Last.fm/SoundCloud/Deezer credential setup, verification, deletion
    integration_manager.py # download/uninstall platform plugins ("with database, etc." — see Uninstall ordering)
    tray.py                # TrayService — pystray wrapper, optional (see Tray section)
  ui/
    main_window.py          # composition root: toolbar, card grid, search, showcase rows, dialog wiring; Activity window host (badge, decision dispatcher)
    card_grid.py            # CardGridManager — grid of playlist cards; per-card actions (keybind capture, remove, reload)
    card.py                 # PlaylistCard dataclass + card widget
    showcase_manager.py     # last-N-added-songs row per card (cfg [showcase] count, default "0" = off); song-aware thumb fetch + cache-mode prune
    search_manager.py       # search bar filtering the grid
    activity_window.py      # non-modal Errors log + duplicate-decision window (hide-on-close singleton)
    playlist_dialog.py      # playlist picker (async thumbnails)
    scrollable.py           # ScrollableFrame — reusable Canvas+Scrollbar+mousewheel container; use it for any new scrollable window
    login_ui.py             # first-run login dialog + per-platform tiles; "Manage" button
    manage_integrations_ui.py  # download/uninstall dialog for platform plugins (uninstall orchestration, see Integration quirks)
    settings_ui.py          # Settings dialog (booleans, ui_scale, columns, font family, thumbnails mode + clear-cache button, duplicate-check, profiles section)
    settings_theme_ui.py    # theme picker Toplevel, writes cfg/theme.ini directly
    profiles_ui.py          # profile create/rename/bucket-edit dialogs (uses services/profile_store.py)
    updater_ui.py, tooltip.py, close_playlist_dialog.py
  utils/
    config.py              # SETTINGS_PATH, THEME_PATH, DEFAULT_THEME, THUMBNAIL_MODES, ensure_settings_file(), ensure_theme_file()
    theme.py               # central theme palette: THEME_MAP, C dict, load_theme()
    thumbnail.py           # ThumbnailService — fetch_image()/fetch_song_image() (any thread), to_photoimage() (main thread only); data-saver modes + on-disk cache (see Key data paths; modes see Config)
    scaling.py             # HiDPI: init(root), ui_font(), px() — single source of scale factor
    icons.py               # IconService — PIL-resized PhotoImages, LANCZOS + cache, main-thread-only
    key_mapping.py         # pynput key normalization/parsing
    platform.py            # get_terminal_command (per-OS shell command for ytmusicapi browser)
    window.py              # pure geometry: center_window, resize_window, fit_window_to_screen, window-geometry persistence (save/restore + validation)
    updater.py             # GitHub release version check
    logging_config.py      # root logger setup, --verbose/--debug/--trace levels

integrations/              # platform plugins (0.3.0 step 1) — each dir is a separate repo (gitignored), declared by plugin.json; the working tree ships deezer/, lastfm/, soundcloud/, spotify/, youtube_music/ (the two below show the full layout)
  youtube_music/
    plugin.json                 # id/display_name/auth_file/auth_file_fallbacks/url_hosts/flow_type/receiver_port/login_module/login_class/login_logo/url templates/class refs; logo.png in the dir root is the standard logo (PluginInfo.logo_path); imports use the DIR name
    youtube_music.py            # YouTubeAuthManager, ytmusicapi monkey-patch
    youtube_music_receiver.py   # Flask HTTP receiver (localhost:5000) — pull-based URL protocol
    integration.py              # YouTubeMusicIntegration (BaseIntegration subclass)
    flow.py                     # YouTubeMusicFlow (BaseFlowController) + capture() for CLI batch mode
    youtube-music-extension/    # Firefox browser extension (Manifest V3, gecko-only) — lives inside its plugin
      manifest.json
      content.js                 # Polls server /status, sends URL with X-PM-Token when ready; server URL derives from manifest host_permissions
  spotify/
    plugin.json
    spotify.py                  # SpotifyAuthManager, SpotifyAPI (OAuth refresh flow), save_spotify_credentials_file()
    integration.py              # SpotifyIntegration
    flow.py                     # SpotifyFlow
```

## Key data paths

Everything except `theme.txt` lives under the platformdirs root
``platformdirs.user_config_dir("playlistmanager")`` (OS-specific:
`~/.config/playlistmanager/` on Linux, `~/Library/Application Support/PlaylistManager/` on macOS, etc.) — `db/`, `cfg/`, and `auth/` are all siblings there. On 0.3.1+ launches, legacy repo-root `db/`/`cfg/` data is migrated once into this root (gated by sentinel files; the repo dirs are no longer read).

| What | Path |
|---|---|
| Playlist registry | `<root>/db/playlists.json` |
| Per-playlist SQLite DBs | `<root>/db/platform/<sanitized>_<md5(playlist_id)[:8]>.db` (legacy: `<sanitized>.db`) |
| App settings (INI) | `<root>/cfg/settings.ini` |
| Theme settings (INI) | `<root>/cfg/theme.ini` |
| Thumbnail cache | `platformdirs.user_cache_dir("playlistmanager")` — `~/.cache/playlistmanager/` on Linux (NOT the config root; **not profile-aware** — a thumbnail is the same bytes whichever profile fetched it): `playlists/` (URL-keyed covers, `md5(url|WxH).png`), `songs/` (URL-keyed or — in `dedupe` mode — identity-keyed song thumbs plus `index.json`), `full/` (original images, cleared on image-view close) |
| Palette spec (docs) | `theme.txt` |
| Auth credentials | `<root>/auth/` |
| Active profile + metadata | `<root>/cfg/profile.json` (name), `<root>/db/profiles.json` (bucket capture per profile) |

**Every path above is profile-aware.** `services/profile_store.py` is the **single resolver** for all of them — `db_dir()`/`cfg_dir()`/`auth_dir()` return the shared dirs (table values) when the **active profile** doesn't capture that bucket, and the profile slot when it does: `db/profiles/<name>/`, `cfg/profiles/<name>/`, `auth/<name>/`. `utils/config.py` imports `profile_store` and calls `initialize()` at load (a deliberate upward edge — see docs/modules.md "Cross-layer exceptions"), so every module's path binds to the active profile automatically. Never hardcode the shared dirs or build paths around them — always go through `profile_store` (or `global_db_dir()`/`global_cfg_dir()`/`global_auth_dir()` when you deliberately want the shared dir regardless). Rename/delete of a profile moves/purges the auth dir too (Logins bucket) — see `profile_store.rename`/`delete`.

The repo's `auth/`, `db/`, `cfg/`, and `log/` directories are gone — everything is under the platformdirs root. **`<root>/db/` and `<root>/cfg/` do not exist on a fresh install** — they are created on demand: `App.__init__` calls `ensure_settings_file()`/`ensure_theme_file()` (cfg/) and `PlaylistStore.ensure_playlists_file()` (creates `db/playlists.json`); `PlaylistStore._write()`, `DatabaseManager.get_playlist_db_path()`, and the store writers mkdir their parents defensively. Any new path under `db/` or `cfg/` must create its parent directory.

## Theme system

Runtime colors are centralized in `app/utils/theme.py`, not re-read from the INI per widget:

- `THEME_MAP` maps short palette keys → `(ini section, option)` pairs; `C` is the flat `key -> color` dict; `load_theme()` re-reads `cfg/theme.ini` into `C`.
- UI modules do `from utils.theme import C` and read `C[...]` at **widget-creation time**. Never freeze a color into a module-level constant at import — runtime theme changes would not propagate.
- Call `load_theme()` again before re-applying colors to existing widgets (the theme picker's `on_theme_change` does this in `main_window.apply_theme`).
- **Adding a color touches four places**: `THEME_MAP` (`utils/theme.py`), `DEFAULT_THEME` (`utils/config.py`), `theme.txt`, and — unless `ensure_theme_file()` has run since — `cfg/theme.ini` (`ensure_theme_file` merges new `DEFAULT_THEME` keys in on every call; stale keys are never removed — the old `_strip_legacy_active_keys` migration in `utils/config.py`, which deleted the dead `activebackground`/`activeforeground` options from user INIs, was already removed).
- **Every `C["..."]` access must exist in `THEME_MAP`** — a missing key raises `KeyError` at widget creation, not at import. Verify with a quick grep/scan after adding keys.
- **Hover colors are derived, not themed**: since 0.2.x the palette has no `*_a_bg`/`*_a_fg` keys. Style every `tk.Button`/`tk.Checkbutton` with `**btn_colors(bg, fg)` (`utils/theme.py`) — it derives `activebackground` via `hover_bg()` (a shade of the resting bg: light colors darkened, dark colors lightened) and reuses the resting fg as `activeforeground`. Never pass `activebackground`/`activeforeground` explicitly; a leftover `C["..._a_bg"]` read raises `KeyError` at widget creation.
- `cfg/theme.ini` is the source of truth when files disagree — correct the other files, not the ini.

## Integration quirks

- **YouTube Music auth**: Expects `browser.json` from `ytmusicapi browser` command. Searched in platformdirs auth folder, then two fallback locations. The `ytmusicapi.get_library_playlists` method gets an instance-level MethodType patch with a fallback implementation installed on each client at auth time (`youtube_music.py`, NOT at import time). On macOS, "open terminal" uses AppleScript (`utils/platform.py`) — do not change it back to `open -a Terminal <dir>`, which opens Finder.
- **Spotify auth**: Expects `spotify.json` with `client_id`, `client_secret`, `refresh_token`. Tokens are auto-refreshed; new refresh tokens are persisted back to disk. **All writes go through `save_spotify_credentials_file()` in `spotify.py`** (600 perms) — don't duplicate the write logic in new code.
- **Add-flow invariant**: the keybind flows (`integrations/*/flow.py`) add to the **platform API first** and abort on failure — a `False` return or missing playlist ID raises, `on_error` fires, and the song is **never** written only to the local DB. Keep this ordering; a platform failure must not leave a "successful" local entry.
- **Uninstall ordering (login window → Manage)**: `ui/manage_integrations_ui.py` orchestrates, `services/integration_manager.uninstall_platform_data()` does the disk work ("with database, etc."). The order is fixed: close the platform's playlist cards first (per-card keybind unregister + store entry + song DB via `close_main_frame`), then stop the flow/receiver (`KeybindController.update_credentials(refreshed_ids=[platform])`), then unregister the live `PluginRegistry`/`IntegrationRegistry` objects, and only then delete credentials, registry entries, `db/<platform>/`, and duplicate-queue records. Reversing it lets a live listener or flow resurrect the platform mid-cleanup.
- **URL receiver**: YouTube Music flow starts a **plain HTTP** Flask server on `localhost` (port from `plugin.json` `"receiver_port"`, fallback `DEFAULT_RECEIVER_PORT` in the receiver module). Short-lived (only while waiting for a URL, ~30 s). Pull-based protocol, **token-authenticated**:
  1. Flow controller starts the server and calls `set_waiting(True)`; the server generates a fresh per-flow token.
  2. Extension polls `GET /status` → `{"ready": true, "token": "..."}`.
  3. Extension POSTs `/receive-url` with header `X-PM-Token: <token>`.
  4. Server validates the token (403 otherwise), consumes the URL, shuts down.
  CORS is restricted to `https://music.youtube.com`. **The port is pinned in two places that must stay in sync**: `integrations/youtube_music/plugin.json` `"receiver_port"` (Python side; `PluginInfo.build_receiver()` passes it to the receiver) and `integrations/youtube_music/youtube-music-extension/manifest.json` `host_permissions` (extension side). `content.js` derives its candidate server URLs from the manifest at runtime (`chrome.runtime.getManifest()`) — no hardcoded port there.
- **Spotify flow**: Uses `GET /me/player/currently-playing` instead of URL receiver — no local server needed.
- **ytmusicapi is optional**: the plugin flow modules use `from __future__ import annotations` so they import without `ytmusicapi` installed (the import happens lazily inside methods). Don't add a top-level `from ytmusicapi import ...` in service-layer files either.

## Tray (pystray)

- `pystray` is optional: `TrayService.available` (`services/tray.py`) is False when it's missing or the icon can't be built, and the app runs normally. `_start_tray` is guarded so a pystray exception cannot abort app startup.
- **Gtk-family backends** (`pystray._appindicator` / `pystray._gtk`) register icon/menu updates as GLib idle callbacks, so `Icon.run_detached()` never shows the icon (no GLib mainloop starts). TrayService runs `Icon.run()` in a daemon thread for those (`_is_gtk_backend()`); `win32`/`xorg`/`darwin` use `run_detached()`. Keep that branch.
- pystray 0.19.x renamed `HAS_DEFAULT` → `HAS_DEFAULT_ACTION`; `_has_default_action()` resolves both via `getattr` fallback — keep that pattern.
- Tray callbacks fire on the tray backend thread — the caller must marshal them to the tkinter main thread via `root.after(0, ...)`.
- `start_in_tray` (setting or `--start-in-tray`) withdraws the window **before any setup work, so it never maps even once** (a late withdraw would flash the window — not "start in tray"); the tray must actually be running (`App._tray_service` set) or the window is deiconified — a missing/unstartable tray leaves the app reachable. Fullscreen wins over start-in-tray when both are requested. `utils.window.center_window` guards unmapped windows (`winfo_width() <= 1` → requested size) so a withdrawn boot still centres correctly.

## Wayland

- **Global hotkeys are impossible on native Wayland by design** — the compositor owns input; apps cannot grab global keys. pynput's global listener is X11-only: on a native Wayland session it receives nothing, and under XWayland it only sees keys while an XWayland client has focus. `KeybindController._start_global_listener` logs a warning on Wayland sessions. The supported path is compositor shortcuts bound to `playlistmanager -a N` (see CLI.MD "Global install (for compositor shortcuts)").
- **Tray**: only the `_appindicator` backend (StatusNotifierItem over DBus) works under Wayland — the `_gtk`/`_xorg` backends dock into an X11 system tray that doesn't exist there. TrayService sets `available=False` for those backends on Wayland sessions so the Settings checkbox can't falsely enable hide-to-tray; don't remove that guard. On GNOME the AppIndicator/KStatusNotifierItem extension is required.
- `is_wayland_session()` (`utils/platform.py`) is True when `WAYLAND_DISPLAY` is set **or** `XDG_SESSION_TYPE == wayland` — deliberately includes stale env inherited by X11 apps launched from a Wayland session.
- **Restore-from-tray is best-effort** (compositors may refuse raise/focus — focus-stealing prevention), and a maximized window may restore unmaximized after hide-to-tray. Maximize-on-restore is intentionally unimplemented (old plan item, resolved by removal) — do not re-add it.

## Threading and the pynput listener

- **Quit path**: never join the pynput listener thread. `listener.stop()` alone stops event delivery; on some Linux setups the thread stays alive until process exit (daemon), and `join(timeout=N)` blocks the caller for the **full** N seconds. Teardown must call `kc.stop_listener(wait=False)` — a `wait=True` join is only acceptable on the Settings→toggle-listener path, and keep it short (≤0.5 s). See `app_controller`/`MainWindow.cleanup()` for the established pattern.
- **Background work** (keybind flows, thumbnail downloads, playlist import/reload) runs in daemon threads. Worker threads must not touch tkinter widgets — they hand results to the UI thread via `root.after(0, ...)` (which requires the mainloop to be running).
- **Guard every `root.after(0, ...)` from a worker thread**: after `root.destroy()` it raises `TclError("application has been destroyed")`, and from a non-main thread before/during a non-running mainloop it raises `RuntimeError("main thread is not in main loop")` — an uncaught raise kills the daemon thread with a traceback at quit. The repo's established guard helpers are `_schedule_ui` (`keybind_controller.handle_keybind`), `_async_ui` (`playlist_controller`), `_tray_after` (`App._start_tray`), plus inline try/except in `main_window` import/reload `on_done` — follow one of those patterns in new background work.
- **Thumbnails**: `ThumbnailService.fetch_image()`/`fetch_song_image()` (network + PIL) are thread-safe; `to_photoimage()`/`ImageTk.PhotoImage` is **main thread only**. Never create a `PhotoImage` in a worker.
- **SQLite**: `DatabaseManager` caches one connection per thread per DB (`db/platform/<name>.db`). Don't share connections across threads; use `get_db_connection`. Before a reload deletes a DB file, the main thread must call `close_thread_connections()` or its cached handle would write to the orphaned file. `delete_playlist_db` is the canonical deletion path (closes cached conns, removes `-wal`/`-shm` sidecars). SQLite leaves `-wal`/`-shm` sidecars behind when only the main file is unlinked. `DatabaseManager._set_pragmas` sets `PRAGMA busy_timeout=30000` on every connection — the sqlite3 default 5s timeout causes "database is locked" when a keybind flow collides with a reload's write lock.

## Config

`DEFAULT_SETTINGS` in `utils/config.py` defines the **entire** settings surface (booleans + values, optional-section defaults included): `update_check`, `center_windows`, `auto_resize`, `remember_geometry` (restore last window size/position on launch; saved via a debounced `<Configure>` binding + a flush in `App.cleanup()`, skipped while maximized/fullscreen — see `utils/window.py`), `fullscreen` (start the window fullscreen; `--fullscreen` overrides per run; window-local F11 toggles it, no global grab), `global_listener`, `hide_to_tray` (the last enables hide-to-tray via `services/tray.py`), `start_in_tray` (launch hidden in the tray when the tray actually starts — falls back to a visible window otherwise), `confirm_on_song_remove` (ask before the showcase ✕ deletes a song — a plain yes/no dialog; full removal is the only action, there is no "keep db" for songs), `confirm_on_playlist_remove` (ask before a playlist card is closed — gates the existing Cancel/Keep-DB/Confirm dialog), `showcase_log` (the per-card log row), `show_pin_buttons` (per-card pin/unpin buttons next to the playlist ✕ — toggled live from Settings), `playlist_stats` (song count/followers/duration row), `like_button` + `scrobble_on_add` + `scrobble_keybind` (Last.fm side effects — all default **off**, and don't flip their defaults; scrobbling is privacy-relevant by design), plus value sections: `ui_scale` (`value` key), `showcase` (`count` — last-N-added-songs per card, "0" = off), `layout` (`columns` — grid columns, default "2", clamped 1-4, applied live from Settings), `window` (`geometry` — last main-window "WxH+X+Y", validated on restore: size clamped to the screen, position must intersect it; best-effort under Wayland where the compositor owns placement), `duplicate_check` (`is_true` off + `title_threshold` "0.85" / `duration_tolerance` "5" — read via `services/duplicate_check.read_settings()`), `remove_playlist` (`default`: `remove`/`keep_db` — the playlist card-✕ behaviour when `confirm_on_playlist_remove` is off, resolved by `ui.card_grid.resolve_playlist_removal_mode`; `keep_db` deletes the registry entry but keeps the per-playlist SQLite file, mirroring the close dialog's Keep-DB button; the dialog itself is always used when the ask is on), `soundcloud` (`capture_mode`: `api`/`hybrid`/`extension` — see docs/modules.md SoundCloud row), `grid_sort` (`key` `name`/`platform`/`added`/`used` + `direction` `asc`/`desc` — written live by the sort combobox/arrow in the Settings dialog (`ui/settings_ui.py`), resolved by `services/playlist_sort.sort_playlists`; `added` = registry insertion order, `used` = per-playlist `last_used_at` stamps from `PlaylistStore.mark_used`, entries that were never used sort to the end; pinned playlists (`pinned: true` on the registry entry, toggled by the card pin button → `PlaylistStore.set_pinned`) float above the rest, the active key applying within each group), and `thumbnails` (`mode`: `off`/`download`/`dedupe`/`cache`/`max` — data-saver modes, see Key data paths; `--data-saver` forces `max` per run). All booleans are read with `ConfigParser.getboolean()` (accepts `yes/no/true/false/1/0`); defaults are applied by `utils/config.py:ensure_settings_file()` (which merges missing sections/keys into existing user files without touching unknown legacy sections).

**Do not assume the INI contains only the sections above**: user files can carry legacy sections (e.g. a stale `toggle_frameless`). Settings writers (`_toggle_setting`) and readers must tolerate and preserve unknown sections.

Theme settings live in `cfg/theme.ini` (see Theme system). The theme picker dialog (`settings_theme_ui.py`) opens as a separate scrollable Toplevel from the main Settings dialog and writes the INI directly.

## Adding a new integration

1. Create `integrations/<name>/` with a `plugin.json` (see `plugin_loader.py` header for the schema: id, display_name, auth_file (optionally auth_file_fallbacks), url_hosts, flow_type, receiver_port, login_module/login_class/login_logo, url templates, class refs).
2. Subclass `BaseIntegration` from `services/integration.py` in `integration.py`, implement the interface; declare it via `integration_class`.
3. Add an auth manager module and declare it via `auth_module`/`auth_attr` (optional — plugins without credentials can omit it).
4. Add `flow.py` with a `BaseFlowController` subclass declared via `flow_module`/`flow_class`. Any plugin that declares `receiver_module`/`receiver_class` gets a receiver injected into its flow (not just extension-type flows — SoundCloud's API flow receives one so its hybrid capture mode can prefer the browser extension); extension-type flows also need `capture()` for CLI batch mode. To get a tile in the login dialog (beyond login_ui's built-in handlers for `youtube_music`/`spotify`/`lastfm`/`soundcloud`), declare `login_module`/`login_class` — a callable `(parent, on_success)` — plus ship the standard `integrations/<dir>/logo.png` (the login tile and the Manage-integrations row both load it via `PluginInfo.logo_path`; a manifest `login_logo` can override it, otherwise omit both and a generic placeholder is used); without these a downloaded plugin is keybind-discoverable but hidden from login. Declare `auth_file_fallbacks` (repo-root-relative paths) for any legacy credential copies that must be deleted on logout/uninstall. Restart the app — no core registration needed.

## Version

Defined in `app/_version.py` as `__version__`. `pyproject.toml` reads it dynamically via `setuptools`. Keep both in sync if adding a build.
