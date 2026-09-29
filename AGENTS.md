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

Every `python main.py ...` above is equivalent under `python -m app ...` (CLI.MD and the cli.py docstring use the `-m app` form). Platform names in the CLI are the **directory** names: `youtube_music`, `spotify`, `lastfm`, `listenbrainz`, `soundcloud`, `deezer`, `tidal`.

Run from the repo root. The `app/` package uses bare imports (`from services.X import ...`, `from utils.X import ...`), so `app/` must be on `sys.path`. The root `main.py` inserts the repo root and delegates to `app/main.py`, which inserts its own directory — either way `app/` lands on `sys.path`. Any standalone script you write needs `sys.path.insert(0, "app")` first.

The global `playlistmanager` command (what compositor shortcuts bind to) is a **user-installed** wrapper or symlink to `main.py` — see CLI.MD "Global install (for compositor shortcuts)". `pip install .` does **not** install it: `pyproject.toml` has no `[project.scripts]`.

The `App` class name is an import trap: `app` is both the package (`app/`) and the module (`app/app.py`). `from app import App` only works when `app/` resolves to `app/app.py` as a plain module (`python main.py`); under `python -m app` the package sits in `sys.modules` without an `App` attribute. `app/main.py:_import_app()` branches on `__package__` to handle both — don't "simplify" it.

## Python version

Requires **Python 3.10+** (uses `X | Y` union type syntax).

## Dependencies

Core: `pillow`, `requests`, `platformdirs`, `flask`, `flask-cors`, `pynput`, `py-minisign` (plugin download signature verification).
Optional: `ytmusicapi` (for YouTube Music integration), `pystray` (for the system tray / hide-to-tray; the code degrades gracefully when it's missing), `keyring` (OS credential-store mirror — optional because its Linux transitive chain `SecretStorage` -> `cryptography` + `jeepney` is the heaviest in the project and is useless without a running daemon; imported lazily inside `credential_store._backend()`, so a missing package degrades to file-only like a missing backend does).

`all` must list **every** extra: the nightly `pip-audit` job installs `.[all,test]` and scans only what is installed, so an extra missing from `all` silently escapes the audit. `test` needs `keyring` itself — `tests/conftest.py`'s autouse `_hermetic_keyring` fixture does `from keyring.backends.fail import Keyring`, so without the package the *entire* suite errors at fixture setup.

## Invariant checker; pytest suite

There is no linter or type checker configured, but `tools/invariants.py` machine-checks the rules this repo states in prose. Tier A: `C["..."]` keys against `THEME_MAP`, `tr()` keys against `DEFAULT_STRINGS`, settings sections against `DEFAULT_SETTINGS`, the `plugin.json` schema (and the class names it references), `receiver_port` against the extension's `host_permissions`, and the plugin classes against the `BaseIntegration`/`ScrobbleCapable`/`BaseFlowController` signatures. Tier B: no `app/` module imports from a layer above it (static or lazy), `docs/modules.md` documents every app module and every module it imports, and a `Thread(target=...)` function never calls `.after()` outside a `try`/`except`. Tier C: no import binds a name nothing reads (a deliberate re-export takes `# noqa`). It imports no app module (the catalogs are read with `ast.literal_eval`), so it is headless, dependency-free and safe to run anywhere. Run it before a PR, next to the suite (service layer, headless — see `tests/` and CONTRIBUTING.md "Testing"):

```bash
python -m pytest                 # full suite (fast, ~4 s)
python -m pytest -q              # quiet
python tools/invariants.py       # invariant checker (exit 1 on findings)
python tools/invariants.py --list-rules   # what it enforces
python -m compileall -q app/     # byte-compile everything
python -m app --list             # headless smoke test (CLI shares the GUI's service layer, needs no display)
```

Exemptions live in the tool's `EXEMPTIONS` table, each `{path: reason}` with a mandatory reason, and an exemption no rule consumes is itself reported. A `layer.import` reason must additionally **name the module it covers**, since a path-keyed entry would otherwise hide any second upward edge in that file. Adding a rule means a `@rule(...)` function plus a violating-case test in `tests/test_invariants.py`. Full rule catalogue, capability exemptions, and the limits: docs/invariants.md.

Tests must stay headless-safe: `tests/conftest.py` redirects every profile-rooted path (`db/`, `cfg/`, `auth/`) to a throwaway temp tree and disables `profile_store._MIGRATE_LEGACY` before any app module import, so a test run never reads/writes/migrates the real data. The GUI requires a display; headless verification of services is possible by importing modules with `sys.path.insert(0, "app")` (avoid instantiating `tkinter.Tk` without one — but note `root.after(...)` from a worker thread raises `RuntimeError: main thread is not in main loop` unless the mainloop is actually running).

## Filesystem

Gitignored paths — commits cannot carry them, and edits to them never show in `git status`: `opencode.jsonc`, `.agents/`, `integrations/*` (every plugin dir — only `integrations/__init__.py` is tracked).

There is no `log/` directory: `utils/logging_config.py` installs a single stderr `StreamHandler` (`basicConfig` with no `filename`), so nothing is written to disk and there is nothing to rotate or retain. A log file would be a new feature, not a setting.

No app data lives in the repo — `db/`, `cfg/`, and `auth/` are under the platformdirs root (see Key data paths).

`CLI.MD` is the CLI reference (tracked; README.MD "Usage" links to it and it spells out the commands above plus the compositor-shortcut wrapper) — do not treat it as dead. `plan.md` no longer exists; its resolved items are folded into README.MD/docs.

## Architecture

Desktop tkinter GUI app that adds currently-playing songs to music-service playlists via hotkeys.

Cross-file relationships (who imports whom), the repository layout, the plugin.json contract, and end-to-end flow traces live in `docs/` — start at `docs/README.md`. Update the matching doc in the same commit as the code change (modules.md for moved/renamed modules, plugins.md for manifest keys, flows.md for changed call chains). This file keeps condensed operational rules; docs/ keeps the detail.

### Documentation map

| Topic | Document |
|---|---|
| docs index + layers | docs/README.md |
| repository layout, per-file dependencies | docs/modules.md |
| end-to-end flows | docs/flows.md |
| plugin.json contract, adding a platform, integration quirks | docs/plugins.md |
| translations / adding a language | docs/i18n.md |
| machine-checked invariants, the pre-PR checker | docs/invariants.md |
| key data paths + profile-aware path resolution | docs/data-paths.md |
| theme system | docs/theming.md |
| settings surface (`cfg/settings.ini`) | docs/config.md |
| threading, pynput listener, `root.after` guards | docs/threading.md |
| Wayland + tray quirks | docs/environment.md |
| machine-specific environment (this dev machine) | `.agents/AGENTS.md` |

## Key data paths

All app data (`db/`, `cfg/`, `auth/`) lives under the platformdirs root — `platformdirs.user_config_dir("playlistmanager")` (`~/.config/playlistmanager/` on Linux) — **not** in the repo. Every path is **profile-aware** and resolves through `services/profile_store.py` (`db_dir()`/`cfg_dir()`/`auth_dir()`): **never hardcode the shared dirs** — always go through `profile_store` (or `global_db_dir()`/`global_cfg_dir()`/`global_auth_dir()` when you deliberately want the shared dir regardless). Thumbnails cache separately under `platformdirs.user_cache_dir("playlistmanager")`. `<root>/db/` and `<root>/cfg/` do not exist on a fresh install — they are created on demand; any new path under them must create its parent directory. Full path table, migration, and bucket rules: docs/data-paths.md.

**Credential permissions** are write-once: the auth dir is enforced `0700` and every credential file `0600` at write time, and nothing re-checks them. `services/auth_setup.audit_credential_permissions(plugin_registry)` is that read-only check — it audits exactly the paths logout/uninstall delete (profile-aware auth dir + `PLATFORM_CREDENTIAL_FILES` via `_credential_paths`, i.e. manifest `auth_file` **and** `auth_file_fallbacks`, plus any installed plugin's `auth_paths`) and delegates the mode comparison to the stdlib-only `services/health_check.audit_path_modes` (a finding = any bit beyond the expected mode, so a *more* private mode never trips it). It runs at startup (`App.__init__`, right after `restore_all()`) and again in the Settings → Integrations → Stored credentials panel, which shows one `settings.cred_perm_warning` line. **Report, never repair** — don't add a silent `chmod` of files the app didn't write. Windows is excluded (`health_check.supports_posix_modes()`): its `st_mode` bits are synthesized from the read-only attribute. Details: docs/data-paths.md "Credential file permissions (startup audit)".

## Theme system

Runtime colors are centralized in `app/utils/theme.py` (`THEME_MAP` → flat `C` dict, `load_theme()`); UI modules read `C[...]` at **widget-creation time**, never freezing a color at module import. **Adding a color touches four places**: `THEME_MAP` (`utils/theme.py`), `DEFAULT_THEME` (`utils/config.py`), `theme.txt`, and `cfg/theme.ini`. **Every `C["..."]` access must exist in `THEME_MAP`** — a missing key raises `KeyError` at widget creation, not at import. Hover colors are derived, not themed: style every `tk.Button`/`tk.Checkbutton` with `**btn_colors(bg, fg)`; the palette has no `*_a_bg`/`*_a_fg` keys. When files disagree, `cfg/theme.ini` wins — correct the other files. Details: docs/theming.md.

## Translation system (i18n)

Every user-facing string in `app/` goes through `utils/i18n.py`: `tr(key, **fmt)` for plain strings, `trn(key, n, **fmt)` for plurals, `tr_status(code)` for the stable card-status codes (opaque plugin/flow messages pass through unchanged). All keys live in `DEFAULT_STRINGS` (code-side English source of truth) — **add a catalog row in the same commit as any new `tr()` call**; `tests/test_i18n.py::TestCatalogConsistency` enforces both directions. `[language] lang` in `settings.ini` picks the language at startup; changes need a restart (same as ui_scale/font). Contract, engine rules, and workflow: docs/i18n.md.

## Integration quirks

Platform plugins are separate repos under `integrations/`, declared by `plugin.json` — the contract, lifecycle, URL receiver protocol, and per-platform quirks: docs/plugins.md. The non-negotiables:

- **Release signing (download supply chain)**: catalog installs fetch the plugin repo's **latest GitHub release asset** (`plugin.tar.gz`) + detached `plugin.tar.gz.minisig` and verify against the pinned `minisign.pub` (repo root; `services/plugin_signing.py`) — **fail-closed**: missing signature, failed check, missing key or unavailable verifier all refuse the install with a `ValueError` whose message is user-facing. The only documented bypass is `[security] verify_plugin_signatures = off` in `cfg/settings.ini` (default `on`); never add a silent path. Hand-dropped plugin dirs bypass verification by design (local filesystem is the trust boundary). Release process: docs/plugins.md "Release signing".
- **Scrobble backends are selected, not discovered**: a service plugin implements the duck-typed `ScrobbleCapable` capability (`services/integration.py`) and may implement it **partially** — it is a union of the scrobble half (`scrobble` + `delete_scrobble`) and the love half (`love`/`unlove`/`is_loved`, optional `get_loved_set`), and a backend implements what its service supports (`integrations/listenbrainz/` has the scrobble half only: its love/hate feedback is keyed on a MusicBrainz recording MBID). Absent methods are the mechanism, not a gap to stub. Every core selection goes through `services/scrobble.py`: `scrobble_backends(integrations, required, only_enabled=True)` returns **every** eligible backend in deterministic platform-id order (unauthenticated ones skipped) and each one receives the listen, `scrobble_backend_for(integrations, required, id)` addresses a single one by id for the remove path, and `resolve_scrobble_backend(...)` stays for the love paths, which are not scrobbles. The per-backend switch is `[<platform id>] scrobbles` in that platform's **own** settings section — absent = enabled, so a freshly logged-in backend works with no configuration and 2-of-3 is expressible; it is a *filter* (an unavailable id is dropped with the rest, never an assertion), and it is ignored on the delete and love paths on purpose. Do not reintroduce a "find the one ScrobbleCapable integration" search at a call site. The ledger (`db/scrobbles.json` v3) records every backend that accepted (`backends`, read from v2's single `backend`) so a later delete hits the same services.
- **Add-flow invariant**: keybind flows (`integrations/*/flow.py`) add to the **platform API first** and abort on failure — a `False` return or missing playlist ID raises, `on_error` fires, and the song is **never** written only to the local DB.
- **Uninstall ordering**: close the platform's playlist cards first (per-card keybind unregister + store entry + song DB via `close_main_frame`), then stop the flow/receiver, then unregister the live `PluginRegistry`/`IntegrationRegistry` objects, and only then delete credentials, registry entries, `db/<platform>/`, and duplicate-queue records. Reversing it lets a live listener or flow resurrect the platform mid-cleanup.
- **Spotify credential writes** go through `save_spotify_credentials_file()` only (600 perms) — don't duplicate the write logic in new code.
- **OS-keyring credential mirror** (`services/credential_store.py`): auth-dir files stay the plugin contract, but core-originated credential writes mirror into the OS keyring (service `playlistmanager`), missing files are restored from it at startup/load, and logout/uninstall clears both copies. Gated by `[security] credential_keyring` (default `on`); no usable backend (headless Linux, CI) degrades to file-only and logs once — never block a login. Only OS backends are trusted (`keyring.backends.fail` and `keyrings.alt` count as unavailable; availability is probed once per process). **Every path that destroys credentials must clear the mirror too**, or live tokens outlive the file: logout/uninstall (`delete_platform_credentials`, and `integration_manager.uninstall_platform_data`'s own unlink loop), profile delete (`delete_profile`), profile rename (`rename_profile` — accounts are `"<profile>:<file>"`, so a rename must move the entries or they strand under the old prefix). These sweeps run regardless of the `credential_keyring` toggle, and `keyring` has no entry enumeration — they can only sweep the filenames `auth_setup.PLATFORM_CREDENTIAL_FILES` declares. Full contract: docs/data-paths.md "Credentials at rest (OS keyring)".
- The YT URL receiver is token-authenticated; its port is pinned in two places that must stay in sync (`plugin.json` `"receiver_port"` + the extension's `host_permissions`). A port that is already taken at flow start is **not** left to the flow's `OSError` swallow: `services/receiver_port.conflict_for()` pre-flights it in `keybind_controller.handle_keybind` and aborts with the `receiver_port_busy` status code (card + status row) plus a `duplicate_queue.record_error` entry naming the port and its holder. Three exemptions keep the check safe, and the bias is toward *not* aborting: no `receiver_port`/no receiver, an already-`is_running()` receiver (it holds the port on our behalf and the flow reuses it), and any `flow_type` other than `"extension"` (a hybrid/api flow may never bind — SoundCloud declares a receiver on 5001 but is `api`, so a busy 5001 must not abort it). Core diagnoses the collision, it never reassigns the port.
- `ytmusicapi` is optional — no top-level `from ytmusicapi import ...` in service-layer files.
- **Credential log hygiene**: register every credential value a plugin loads/saves (and every in-memory token obtained from a live API — Deezer's JWT/`sid`, rotated access/refresh tokens) with `register_secret()` from `utils.logging_config`; the app's log redaction masks them even when they appear bare. Untracked in-memory tokens are only masked if the plugin registers them.
- **Secrets on the CLI**: `--login` resolves each credential **flag > `PLAYLISTMANAGER_<PLATFORM>_<FIELD>` env var > hidden prompt** (`cli._resolve_login_credentials`) — argv is world-readable through `/proc/<pid>/cmdline` (mode 0444) and is kept in the shell history, so a new CLI flag that carries a secret must come with an env/prompt tier. New platforms that take a credential get the same treatment (`_CREDENTIAL_PROMPT_KEYS` maps field → prompt label; blank env values count as unset); the Deezer ARL (`PLAYLISTMANAGER_DEEZER_ARL`) and the ListenBrainz token (`PLAYLISTMANAGER_LISTENBRAINZ_TOKEN`) have an env tier and deliberately no flag. Register resolved secrets with `register_secret()` and pass failure reasons through `redact()` before printing them, since a third-party error string can echo the value back. Never log a credential *value*, only which source it came from. Full comparison: CLI.MD "Authentication".

## Wayland and the system tray

- **Global hotkeys are impossible on native Wayland by design** — the supported path is compositor shortcuts bound to `playlistmanager -a N` (see CLI.MD "Global install (for compositor shortcuts)"). pynput's global listener is X11-only.
- Tray: `pystray` is optional and `TrayService.available` degrades gracefully. Gtk-family backends must run via `Icon.run()` in a daemon thread; `win32`/`xorg`/`darwin` use `run_detached()`. Tray callbacks fire on the backend thread — marshal them to the tkinter main thread via `root.after(0, ...)`.
- `start_in_tray` withdraws the window **before any setup work, so it never maps even once** (no flash); fullscreen wins over start-in-tray when both are requested.
- Details, the Wayland tray-backend guard, `is_wayland_session()`, and restore-from-tray limits: docs/environment.md.

## Threading and the pynput listener

- **Quit path**: never join the pynput listener thread. Teardown calls `kc.stop_listener(wait=False)` — a `wait=True` join burns the full timeout and is only acceptable on the Settings→toggle-listener path (≤0.5 s). See `app_controller`/`MainWindow.cleanup()` for the established pattern.
- **Background work** (keybind flows, thumbnail downloads, playlist import/reload, updater, tray) runs in daemon threads. Worker threads must not touch tkinter widgets — they hand results to the UI thread via `root.after(0, ...)`.
- **Guard every `root.after(0, ...)` from a worker thread**: after `root.destroy()` it raises `TclError("application has been destroyed")`, and from a non-main thread with no running mainloop it raises `RuntimeError("main thread is not in main loop")` — an uncaught raise kills the daemon thread with a traceback at quit. Follow the repo's established guards: `_schedule_ui` (`keybind_controller.handle_keybind`), `_async_ui` (`playlist_controller`), `_tray_after` (`App._start_tray`), or the inline try/except in `main_window` `on_done`.
- **Thumbnails**: `ThumbnailService.fetch_image()`/`fetch_song_image()` (network + PIL) are thread-safe; `to_photoimage()`/`ImageTk.PhotoImage` is **main thread only** — never create a `PhotoImage` in a worker.
- **SQLite**: `DatabaseManager` caches one connection per thread per DB — use `get_db_connection`, never share connections across threads. Before a reload deletes a DB file, close thread-cached connections; `delete_playlist_db` is the canonical deletion path (closes cached conns, removes `-wal`/`-shm` sidecars). `PRAGMA busy_timeout=30000` is set on every connection.

Full rules and reasoning: docs/threading.md.

## Config

`DEFAULT_SETTINGS` in `utils/config.py` defines the **entire** settings surface (booleans + values, optional-section defaults included): window/geometry, listener/tray, confirmation dialogs, showcase/log/pin/stats rows, Last.fm side effects (`like_button` + `scrobble_on_add` + `scrobble_keybind` — all default **off**; don't flip their defaults, scrobbling is privacy-relevant by design), plus the value sections (`ui_scale`, `showcase`, `layout`, `scrobble`, `window`, `duplicate_check`, `remove_playlist`, `soundcloud`, `grid_sort`, `thumbnails`). **Do not assume the INI contains only those sections** — user files can carry legacy sections; settings writers and readers must tolerate and preserve unknown sections. Theme settings live in `cfg/theme.ini` (see Theme system). Full setting-by-setting reference: docs/config.md.

## Adding a new integration

Follow docs/plugins.md "Adding a platform": create `integrations/<name>/` with a `plugin.json`, subclass `BaseIntegration` from `services/integration.py` (+ `BaseFlowController` in `flow.py` for add-flow platforms, plus a receiver module and `capture()` for CLI batch mode where appropriate), declare auth/login/logo manifest keys as needed, restart the app — no core registration step exists.

## Version

Defined in `app/_version.py` as `__version__`. `pyproject.toml` reads it dynamically via `setuptools`. Keep both in sync if adding a build.