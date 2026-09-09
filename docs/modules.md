# Module map

Per-file dependency table. "Uses" lists project modules from static
top-level imports only (stdlib and third-party omitted). `(lazy)` marks a
function-level import that does not fire at module load. Entry-point and
dynamic-import edges (`_import_app`, argparse dispatch) are noted by hand.

To regenerate the raw edge list while updating this file:

```bash
grep -rn "^from \|^import " app/ --include="*.py"
```

## Entry points and bootstrap

| File | Role | Used by | Uses |
|---|---|---|---|
| `main.py` | Root launcher; puts repo root on `sys.path`, delegates to `app.main` | user shell, `playlistmanager` wrapper, compositor shortcuts | `app.main` |
| `app/main.py` | argparse dispatch; `-a/-p/--list/login/...` go to `cli`, default starts GUI | `main.py`, `app/__main__.py` | `utils.logging_config`; `app.app` via `_import_app()` (handles package/module dual identity) |
| `app/__main__.py` | Makes `python -m app` work | interpreter | `app.main` |
| `app/app.py` | `App`: boot sequence (see flows.md #1), auth bootstrap, quit path | `app.main._import_app()` | `plugin_loader`, `services.integration`, `services.playlist_store`, `services.tray`, `controllers.app_controller`, `controllers.keybind_controller`, `ui.main_window`, `ui.updater_ui`, `utils.updater`, `utils.{config,scaling,window,logging_config}` |
| `app/cli.py` | Headless commands; shares the service layer, never touches tkinter | `app.main` | `plugin_loader`, `services.{database,integration,playlist_store,playlist_sync,playlist_url,song_manager,auth_setup,scrobble_log}`, `utils.{thumbnail,logging_config}` |
| `app/plugin_loader.py` | `PluginInfo` / `PluginRegistry`: manifest scan without importing plugin code; lazy class-ref importers (`import_login` for login tiles); `auth_paths` (profile-aware auth-dir file + manifest `auth_file_fallbacks`); `unregister()` (uninstall path, also evicts the plugin's modules from `sys.modules`); `get_default_registry()` singleton | `app.app`, `app.cli`, `services.playlist_url`, `services.integration_manager` (`base_dir`), `ui.manage_integrations_ui`, `ui.login_ui` | `services.profile_store` `(lazy)`; stdlib only |
| `app/_version.py` | `__version__`, single source of version | `pyproject.toml` (dynamic), `ui.settings_ui`, `ui.updater_ui`, `utils.updater` | none |

## controllers/

| File | Role | Used by | Uses |
|---|---|---|---|
| `app_controller.py` | Quit flow, force-quit dialog, refresh-auth entry point, restart-on-profile-switch (`restart_app()`: cleanup + re-launch). Holds the `App` instance | **only** `app.app`; `ui.main_window` (restart callback) | `utils.scaling`, `utils.theme` |
| `keybind_controller.py` | pynput listener lifecycle, keybind recording state machine, per-platform flow init (`_ensure_initialized` -> lock-serialized `_build_flow`, receiver registered only after flow construction succeeds), public accessors `get_flow` + `flow_busy` for the activity window's Add worker (which holds the busy lock around `execute_flow` so captures cannot race keybind/scrobble flows), dispatch (`handle_keybind`), credential swap (`update_credentials`) | `app.app` (constructed); `ui.card_grid` / `ui.main_window` hand it callbacks | `controllers.keybind_registry`, `services.{duplicate_queue,playlist_store,song_manager,scrobble_log}`, `utils.{key_mapping,platform,theme}` |
| `keybind_registry.py` | hotkey -> (name, platform, playlist_id) mapping; defines the `KeybindCallbacks` protocol that the UI side implements | `keybind_controller`, `ui.card_grid`, `ui.main_window` | `utils.key_mapping` |
| `playlist_controller.py` | add-playlist workflow orchestration (`open_playlist_dialog`), no widget code | `ui.main_window` | `services.playlist_store` |

## services/

| File | Role | Used by | Uses |
|---|---|---|---|
| `profile_store.py` | Active-profile persistence + metadata + path resolution: `active_profile()`/`set_active()`, `list_profiles()`/`create()`/`delete()`/`rename()`/`set_bucket()`/`get_bucket()`, and the single source of truth for profile-aware paths `db_dir()`/`cfg_dir()`/`auth_dir()` (each returns a `profiles/<name>/` slot when that bucket is captured, or the shared/global dir when not). Reads the active name from `cfg/profile.json`, metadata from `db/profiles.json`. Import-time cheap, no app/tkinter imports. Every other module that stores data derives its path from here | `utils.config` (calls `initialize()` on import), `services.{playlist_store,duplicate_queue,scrobble_log,database,auth_setup}`, `plugin_loader`, `ui.settings_ui`, `ui.profiles_ui` | stdlib + `platformdirs` only |
| `integration.py` | `BaseIntegration` ABC, `IntegrationRegistry` (+ `unregister()` for the uninstall path), `BaseFlowController` ABC. The plugin contract | `app.app`, `app.cli`, `ui.manage_integrations_ui`, both integrations' `integration.py` + `flow.py` | stdlib only |
| `duplicate_check.py` | Near-duplicate matcher (normalized-title ratio + artist gate + duration gate), `find_similar` / `find_duplicate_pairs`, shared add-path policy `resolve_near_duplicate`, `[duplicate_check]` knob reader | both integrations' `flow.py`, `ui.main_window` | `services.duplicate_queue`, `utils.config` `(lazy)` |
| `duplicate_queue.py` | `db/extra.json` (profile-aware): pending duplicate songs, pair-song memory (`added` / `not_duplicate` / `dismissed`), persistent error log; module lock + atomic writes; `stamp()` mtime signal for the badge poll; `purge_platform()` (uninstall) and `purge_playlist()` (playlist delete, called by `card_grid.close_main_frame` / `cli.run_del`) scoped cleanup | integrations' `flow.py` `(via resolve_near_duplicate)`, `keybind_controller`, `ui.main_window`, `ui.activity_window` `(via load_data)`, `ui.card_grid`, `app.cli` | stdlib only; `services.playlist_store` `(lazy liveness check)`, `services.profile_store` |
| `scrobble_log.py` | `db/scrobbles.json` (profile-aware): records the exact accepted scrobble timestamp per (platform, playlist_id, song_id) at auto-scrobble time so remove-song can delete THAT scrobble, not the track's most recent one; FIFO-capped, module lock + atomic writes | `keybind_controller`, `app.cli` (record); `ui.showcase_manager` (lookup/clear); `ui.card_grid`, `app.cli`, `services.integration_manager` (cleanup) | stdlib only; `services.profile_store` |
| `playlist_store.py` | `db/playlists.json` (profile-aware) CRUD, thread-safe, TTL cache; `ensure_playlists_file()`, `migrate_schema()` | `app.app`, `app.cli`, `keybind_controller`, `playlist_controller`, `card_grid`, `main_window`, `showcase_manager`, `playlist_sync`, integrations' `flow.py` | stdlib only; `services.profile_store` |
| `playlist_sync.py` | `PlaylistSyncService`: threaded `import_tracks` / `reload_database` (+ `_sync` cores run in worker threads) | `ui.main_window`, `app.cli` | `services.{database,playlist_store,song_manager}`, `utils.thumbnail` |
| `song_manager.py` | `SongManager`: per-playlist songs CRUD against the SQLite file | `keybind_controller`, `main_window`, `showcase_manager`, `playlist_sync`, `app.cli`, integrations' `flow.py` | `services.database` |
| `database.py` | `DatabaseManager`: id-keyed paths under `db/platform/` (profile-aware via `profile_store.db_dir()`), legacy migration, per-thread connection cache, canonical `delete_playlist_db` | `song_manager`, `playlist_sync`, `app.cli`, `ui.card_grid`, `ui.main_window` | stdlib only; `services.profile_store` |
| `playlist_url.py` | `parse_playlist_url(url, plugin_registry=None)`: host table from manifests, shared URL shapes + path-form hosts (`_PATH_FORM_PLATFORMS`, e.g. SoundCloud — any path on a declared host is a resource URL); `build_playlist_url` / `build_song_url` (plugin-declared `playlist_url_template` / `song_url_template`, hardcoded fallbacks, generic `/<id>`; SoundCloud URN ids map onto `/sets/<id>`, track URNs yield `None`); falls back to `get_default_registry()` | `app.cli` (GUI does not parse URLs today), `ui.card_grid`, `ui.showcase_manager` | `plugin_loader` |
| `auth_setup.py` | YT/Spotify/Last.fm credential setup, verify-first persistence, deletion, `PLATFORM_CREDENTIAL_FILES` + manifest-driven `_credential_paths` / `delete_platform_credentials`; `AUTH_DIR` is profile-aware via `profile_store.auth_dir()` | `app.cli` (login/logout), `ui.login_ui`, `services.integration_manager` (credential-file deletion on uninstall) | `services.profile_store`, `integrations.spotify.spotify` / `integrations.lastfm.lastfm` `(lazy)`, `utils.{platform,logging_config}` |
| `integration_manager.py` | Download (GitHub tarball -> `integrations/`, safe extraction, manifest-verified, temp-sibling swap) and uninstall ("with database, etc.": credentials, playlist registry entries, per-platform song DBs, duplicate-queue purge, scrobble-ledger purge, plugin dir) of platform integrations; `INTEGRATION_REPOS` catalog | `ui.manage_integrations_ui` | `plugin_loader`, `services.{auth_setup,duplicate_queue,database,playlist_store,scrobble_log}` |
| `tray.py` | `TrayService`: optional pystray wrapper, backend detection | `app.app` | `utils.platform` |

## ui/

| File | Role | Used by | Uses |
|---|---|---|---|
| `main_window.py` | Composition root: toolbar, card grid, search, showcase rows, dialog wiring; implements the UI side of `KeybindCallbacks`; activity-window host (badge poll, song dispatcher, Add worker, duplicate scanner) | `app.app` | `activity_window`, `card_grid`, `playlist_controller`, `login_ui`, `playlist_dialog`, `search_manager`, `showcase_manager`, `settings_ui`, `scrollable`, `tooltip`, `keybind_registry`, `services.{database,duplicate_check,duplicate_queue,playlist_store,playlist_sync,song_manager}`, `utils.{config,icons,platform,scaling,theme,window}` |
| `card_grid.py` | `CardGridManager`: grid of cards, per-card actions; receives a `make_keybind_callbacks(card_num)` factory, never builds flows itself | `main_window` | `ui.{card,close_playlist_dialog,scrollable,tooltip}`, `controllers.keybind_registry`, `services.{database,playlist_store,scrobble_log}`, `utils.{scaling,theme}` |
| `card.py` | `PlaylistCard` dataclass + card widget | `card_grid` | stdlib/tkinter |
| `showcase_manager.py` | last-N-songs row per card | `main_window` | `services.{playlist_store,song_manager,scrobble_log}`, `utils.{icons,thumbnail,scaling,theme}`, `ui.tooltip` |
| `search_manager.py` | search bar filtering the grid | `main_window` | `utils.{scaling,theme}` |
| `playlist_dialog.py` | library picker; thumbnails fetched on a thread, converted on the main thread | `main_window` | `ui.scrollable`, `utils.{thumbnail,scaling,theme}` |
| `login_ui.py` | first-run login dialog; polls for `browser.json` readiness; degrades when an integration repo is absent; rebuildable tiles + "Manage" button opening the integration manager dialog; Last.fm Test validates key/secret only (no browser), Save runs the full web-auth flow | `main_window` | `services.auth_setup`, `integrations.youtube_music.youtube_music` `(lazy)`, `ui.manage_integrations_ui` `(lazy, Manage button)`, `utils.{icons,window,scaling,theme,logging_config}` |
| `settings_ui.py` | settings dialog (booleans, ui_scale, columns...; "Duplicate check" section with the enable toggle, title-threshold / duration-tolerance sliders, and the manual scan button; "Profiles" section for switching/creating/renaming/editing/deleting profiles; hides Last.fm / SoundCloud sections when those plugins are not installed, gated by the `plugin_availability` ids passed in) | `main_window` | `settings_theme_ui`, `profiles_ui`, `services.profile_store`, `ui.scrollable`, `utils.{config,scaling,theme}` |
| `profiles_ui.py` | profile management Toplevels: create / rename / edit-bucket dialogs (steal the grab, restore the parent's on close) | `settings_ui` | `services.profile_store`, `ui.scrollable`, `utils.{scaling,theme,window}` |
| `manage_integrations_ui.py` | integration manage dialog (login window's "Manage" button): per-platform Download/Uninstall rows, download worker thread, uninstall orchestration (close cards via `on_uninstall` callback -> stop flow/receiver -> unregister live registries -> service cleanup); modal, scrollable | `ui.login_ui` | `services.integration_manager`, `plugin_loader`, `ui.scrollable`, `utils.{logging_config,scaling,theme}` |
| `activity_window.py` | Non-modal Activity window: Errors log (with Clear) + Duplicates tab (pending songs / scan results / marked-pairs manager); hide-on-close singleton; all actions funnel through the injected `on_song(record, action)` | `ui.main_window` | `ui.scrollable`, `utils.{scaling,theme}` |
| `settings_theme_ui.py` | theme picker Toplevel; writes `cfg/theme.ini` directly; Themes bar at the top (combo of built-in + saved user themes with Save/Create/Delete, mirroring the Profiles section) applies themes live via the existing `on_theme_change` callback; window is capped at the screen height (`fit_window_to_screen`) so the swatch list scrolls instead of opening taller than the display | `settings_ui` | `ui.scrollable`, `utils.{config,scaling,theme,window}` |
| `updater_ui.py` | `show_update_dialog(root, version, url, body)` | `app.app` (after the updater thread lands) | `utils.{scaling,theme,window}` |
| `close_playlist_dialog.py` | confirm-remove dialog | `card_grid` | `utils.{scaling,theme,window}` |
| `scrollable.py` | `ScrollableFrame` mixin | `card_grid`, `playlist_dialog`, `settings_ui`, `settings_theme_ui`, `main_window` | `utils.theme` |
| `tooltip.py` | `ToolTip` hover class | `card_grid`, `showcase_manager`, `main_window` | `utils.{scaling,theme}` |

## utils/

| File | Role | Used by | Uses |
|---|---|---|---|
| `config.py` | `SETTINGS_PATH`/`THEME_PATH` (profile-aware via `profile_store.cfg_dir()`; imported first so the active profile is initialised before paths bind), `DEFAULT_SETTINGS`/`DEFAULT_THEME`, `ensure_*_file()`, `get_setting(_value)`, named-theme CRUD: `THEMES_DIR` (`cfg/themes/`, one full-palette INI per saved theme), `list_themes()`/`save_theme()`/`apply_theme()`/`delete_theme()`/`rename_theme()` with `_NAME_RE`-style validation and reserved built-in names ("Default theme"/"White Theme") | most modules (theme, scaling, key_mapping, updater, settings dialogs, main_window, app.app) | `services.profile_store` |
| `theme.py` | `THEME_MAP`, flat dict `C`, `load_theme()`, `btn_colors()` | every UI module, `app_controller`, `keybind_controller` | `utils.{config,scaling}` |
| `scaling.py` | `init(root)` / `ui_font()` / `px()`: single source of scale factor and font family; must run before any widget; validates font family against `tk.font.families()` at startup | `app.app` (first), then icons, theme, all UI | `utils.config` |
| `icons.py` | `IconService`: PIL-resized PhotoImages, LANCZOS + cache, main-thread only | `login_ui`, `main_window`, `showcase_manager` | `utils.scaling` |
| `thumbnail.py` | `ThumbnailService.fetch_image()` any thread / `to_photoimage()` main thread only | `playlist_dialog`, `showcase_manager`, `playlist_sync`, `app.cli` | requests, Pillow |
| `key_mapping.py` | pynput/tk key normalization and parsing | `keybind_controller`, `keybind_registry` | stdlib only |
| `platform.py` | `is_wayland_session()`, `x11_root_desktop_state()` (EWMH root props via `xprop`, used by hide-to-tray to exclude desktop-switch/show-desktop minimizes), `get_terminal_command()`, `open_directory()` | `keybind_controller`, `tray`, `auth_setup`, `main_window` | stdlib only |
| `window.py` | `center_window` / `resize_window` / `fit_window_to_screen` (caps a Toplevel at the screen size, used before centering so over-tall dialogs scroll instead of opening off-screen), pure geometry | `app.app` and most dialogs | tkinter only |
| `updater.py` | GitHub latest-release check on a worker thread | `app.app` | `utils.config`, `_version` |
| `logging_config.py` | `configure_logging()` levels, `user_log()` helper | `app.main`, `app.app`, `cli`, `auth_setup`, several UI modules | stdlib only |

## integrations/ (separate repositories)

The directories are untracked here; core only knows their manifests.
Core touchpoints, for orientation when reading a plugin repo:

| Module | Contract |
|---|---|
| `<plat>/integration.py` | subclass of `BaseIntegration`; instantiated in `App.__init__` with `auth_manager=` |
| `<plat>/flow.py` | subclass of `BaseFlowController`; ctor takes the integration plus `SongManager` (a plugin declaring `receiver_class` also gets the receiver - not just extension-type; SoundCloud receives it so its hybrid mode can prefer the browser extension); implements `execute_flow` and `capture(timeout)` for CLI batch mode |
| `youtube_music/youtube_music_receiver.py` | Flask receiver on localhost, token-authenticated; port pinned jointly with the extension manifest (see AGENTS.md) |
| `spotify/spotify.py` | owns credential writes via `save_spotify_credentials_file()`; `auth_setup` delegates to it |
| `soundcloud/soundcloud.py` | owns credential writes via `save_soundcloud_credentials_file()` (verify-first, like Spotify); `SoundCloudAPI` wrapper (OAuth `Authorization: OAuth <token>`, fetch-modify-write `/playlists/{urn}` PUT because there is no add-track endpoint), `SoundCloudAuthManager`, and the self-contained login tile `show_soundcloud_login(parent, on_success)` via `login_module`/`login_class` (no `auth_setup` change) |
| `soundcloud/integration.py` | `SoundCloudIntegration` - `BaseIntegration`; `get_library_playlists` filters non-`playlist` `set_type`s (albums/station can't take adds); `add_tracks_to_playlist`/`remove_track` delegate to the wrapper's whole-array PUT; `_normalize_playlist_id` accepts URN / numeric / `user/slug` (the last via `/resolve`, cached) |
| `soundcloud/flow.py` | `SoundCloudFlow` - capture-mode aware (`[soundcloud] capture_mode` in `cfg/settings.ini`: `api` reads `/me/recently-played`[0], `hybrid` prefers the extension receiver then falls back, `extension` receiver-only); enforces the add-first-then-local-DB invariant and the paused-skip policy (paused POST -> no-op, never falls back to recently-played) |
| `soundcloud/soundcloud_receiver.py` | `SoundCloudURLReceiverManager` - Flask receiver on localhost:5001, CORS to soundcloud.com, receives `{url, title, playing}` with `X-PM-Token`; `get_received_payload()` + `was_polled()` let the flow discriminate receiver-miss vs paused |
| `lastfm/lastfm.py` | `LastFMClient` (signed Last.fm API: love/unlove/scrobble/removeScrobble/getInfo/getLovedTracks), `verify_lastfm_credentials` (token -> browser authorize -> session; fatal errors surface fast, only the not-authorized signal polls), `validate_api_credentials` (key/secret-only check for the login Test button), `save_lastfm_credentials_file` (0600, atomic, full-write + tmp cleanup on failure); `auth_setup` delegates creds/load/validate to it |
| `lastfm/integration.py` | `LastFMIntegration` - *service* plugin (no `flow_class`): `BaseIntegration` + `ScrobbleCapable` (`scrobble` returns the accepted timestamp (int) or None, `love`/`unlove`/`is_loved`/`get_loved_set` (batched loved-list fetch for showcase hearts)/`delete_scrobble`), TTL'd + LRU loved-state cache, reads `lastfm.json` itself |

## Cross-layer exceptions

All static edges follow the downward direction in README.md. These four
do not, and each is deliberate:

1. `services.auth_setup.save_spotify_credentials` delegates through a
   lazy import to `integrations.spotify.spotify.save_spotify_credentials_file`.
   Keeps exactly one credential writer, inside the plugin repo.
2. `ui.login_ui._browser_file_ready` reads
   `integrations.youtube_music.youtube_music.BROWSER_FILE[_FALLBACKS]`
   lazily and falls back to `auth_setup.BROWSER_FILE` if the plugin repo
   is missing.
3. The `KeybindCallbacks` protocol lives in
   `controllers/keybind_registry.py`, not in services: the UI side
   implements it, the controller consumes it.
4. `utils/config.py` statically imports `services/profile_store.py` on
   startup and calls `initialize()`. This is an upward edge (utils -->
   services) that the other modules need: `profile_store` is the single
   place the active profile is resolved at import, and every path-holding
   module (services + utils.config) must derive from it. It is safe only
   because `profile_store` has no app imports (stdlib + platformdirs), so
   it cannot create a cycle. All other `services.*` imports of
   `profile_store` are already in the services layer.
