# Key data paths

Everything except `theme.txt` lives under the platformdirs root
``platformdirs.user_config_dir("playlistmanager")`` (OS-specific:
`~/.config/playlistmanager/` on Linux, `~/Library/Application Support/PlaylistManager/` on macOS, etc.) — `db/`, `cfg/`, and `auth/` are all siblings there. On 0.3.1+ launches, legacy repo-root `db/`/`cfg/` data is migrated once into this root (gated by sentinel files; the repo dirs are no longer read).

| What | Path |
|---|---|
| Playlist registry | `<root>/db/playlists.json` |
| Per-playlist SQLite DBs | `<root>/db/platform/<sanitized>_<md5(playlist_id)[:8]>.db` (legacy: `<sanitized>.db`) |
| App settings (INI) | `<root>/cfg/settings.ini` |
| Theme settings (INI) | `<root>/cfg/theme.ini` |
| i18n catalogs (user) | `<root>/cfg/i18n/<lang>.ini` (self-healing; overlaid on bundled `app/i18n/<lang>.ini`; code-side English `DEFAULT_STRINGS` in `utils/i18n.py`) |
| Thumbnail cache | `platformdirs.user_cache_dir("playlistmanager")` — `~/.cache/playlistmanager/` on Linux (NOT the config root; **not profile-aware** — a thumbnail is the same bytes whichever profile fetched it): `playlists/` (URL-keyed covers, `md5(url|WxH).png` — with `[thumbnails] max_size` set, the digest also folds the cap in as `md5(url|WxH|c256).png`, so entries from a different cap are never served and cache-mode pruning drops them as unreferenced; the uncapped digest is unchanged, so an existing cache survives the upgrade), `songs/` (URL-keyed or — in `dedupe` mode — identity-keyed song thumbs plus `index.json`), `full/` (original images, cleared on image-view close) |
| Palette spec (docs) | `theme.txt` |
| Auth credentials | `<root>/auth/` |
| Active profile + metadata | `<root>/cfg/profile.json` (name), `<root>/db/profiles.json` (bucket capture per profile) |

## Credentials at rest (OS keyring)

The auth-dir JSON files are the **plugin-facing contract**: platform
plugins read (and some flows write) them directly, and the
`ytmusicapi` terminal flow writes `browser.json` itself outside the
app. On top of those files, `services/credential_store.py` keeps an
**encrypted mirror in the OS keyring** (service `playlistmanager`,
accounts `"<profile>:<basename>"` — profiles are isolated from each
other):

- core-originated writes (`save_*` / `save_and_verify_*` in
  `services/auth_setup.py`, plus the first observation of a complete
  terminal-written `browser.json`) mirror the payload into the keyring;
- a missing file is restored from the mirror on load
  (`load_spotify_credentials`) and by `restore_all()` — run in
  `App.__init__` before integrations build their auth managers, and in
  the headless CLI `main()` before any platform-touching command;
- logout/uninstall (`delete_platform_credentials`, plus the
  Manage-dialog uninstall's own unlink loop in
  `integration_manager.uninstall_platform_data`) clears the keyring copy
  alongside the file;
- **profile lifecycle**: deleting a profile sweeps its entries
  (`delete_profile`) and renaming one moves them (`rename_profile`).
  Accounts are `"<profile>:<basename>"`, so a profile rename that left
  the mirror behind would both strand a live credential under the old
  prefix and leave the renamed profile unable to restore from it. Both
  sweeps run regardless of the `credential_keyring` toggle and can only
  cover the filenames `auth_setup.PLATFORM_CREDENTIAL_FILES` declares —
  `keyring` has no portable entry-enumeration API.

Restore resolves every file through the profile-aware `auth_file()`
resolver, not through the import-time `PLATFORM_CREDENTIAL_FILES` paths:
those are bound with whatever profile was active at import, so filtering
on them would skip every file once the active profile captures the
Logins bucket into its own `auth/<profile>/` slot.

Scope note (honest): the mirror **reduces** the "credentials at rest"
risk—backups/home-dir copies no longer contain the only working
credentials—but the plaintext files remain because the plugins read
them. Removing the files is a per-plugin-repo follow-up: have each
plugin's load/save delegate to `credential_store` (or a future core
API) and delete the on-disk copy. The `[security] credential_keyring`
setting (default `on`) is the documented toggle; see config.md.
Without a usable OS backend the app degrades to file-only storage
(0600 files, the historical behavior) and logs the degradation once —
availability is probed once per process (`keyring_available()`), the
Settings → Integrations → Stored credentials panel shows the live
state, and CI exercises only the file-fallback path.

`keyring` is an **optional** dependency (the `keyring` extra). It is
imported lazily inside `credential_store._backend()` and every call
path is exception-guarded, so an absent package degrades exactly like
an absent daemon. The probe keeps the two apart (`_probe_backend()`
returns a reason: `missing` vs `unusable`) because the remedies differ —
`pip install "PlaylistManager[keyring]"` versus starting
gnome-keyring/kwallet. Note the test suite itself needs the package
regardless: `tests/conftest.py`'s autouse `_hermetic_keyring` imports
`keyring.backends.fail` to keep tests off the developer's real keyring,
which is why `keyring` is in the `test` extra too.

## Credential file permissions (startup audit)

The credential **write** paths set the access bits once and never re-check
them: the auth dir is created and `chmod`-ed `0700`
(`auth_setup.setup_ytmusic_auth`, the Settings "Open auth dir" button) and
every credential file is written `0600` (the plugin atomic writers, the
keyring-restore path). Those bits are the only thing keeping a live token
unreadable to other users on a shared machine, and they survive exactly
as long as nothing behind the app's back rewrites them — a backup
restored onto a filesystem that ignores POSIX modes (exFAT/NTFS shares,
some FUSE mounts), a copy made through one, a hand edit, a permissive
umask on a file a third-party flow wrote.

`services/auth_setup.audit_credential_permissions()` is the read-only
check, run **at startup** (`App.__init__`, immediately after
`restore_all()` so the verdict describes the files actually in use) and
again when the Settings → Integrations → Stored credentials panel is
built. It audits exactly the paths logout/uninstall delete — the
profile-aware auth dir (expected `0700`) plus every credential file
(expected `0600`): `PLATFORM_CREDENTIAL_FILES` resolved through
`_credential_paths` (so the manifest's `auth_file` **and** its
`auth_file_fallbacks` are covered) and the `auth_paths` of any other
installed plugin. The mode comparison is
`services/health_check.audit_path_modes()`: a path is a finding when it
grants any bit its expected mode does not (`mode & 0o777 & ~expected`),
so a *more* private mode is never flagged, while an unexpected owner
execute bit is.

Findings are **reported, never repaired** — the app does not silently
`chmod` files it did not write. A regression produces one
`service.credential_perm_loose` log line at startup plus a per-path
`WARNING`, and a single `settings.cred_perm_warning` line at the top of
the credentials panel; the per-path rows below it already show each
mode. Missing paths are not findings (nothing to verify; the panel
labels them "missing"), an unstattable path is skipped, and on Windows
the whole check is off: `st_mode` there is synthesized from the read-only
attribute and says nothing about who can read a file, so
`health_check.supports_posix_modes()` returns False and the report comes
back `supported=False` with no findings. The primitive is stdlib-only and
takes its paths as arguments, so it stays headless-testable.

## Profile awareness

**Every path above is profile-aware.** `services/profile_store.py` is the **single resolver** for all of them — `db_dir()`/`cfg_dir()`/`auth_dir()` return the shared dirs (table values) when the **active profile** doesn't capture that bucket, and the profile slot when it does: `db/profiles/<name>/`, `cfg/profiles/<name>/`, `auth/<name>/`. `utils/config.py` imports `profile_store` and calls `initialize()` at load (a deliberate upward edge — see [modules.md](modules.md) "Cross-layer exceptions"), so every module's path binds to the active profile automatically.

Never hardcode the shared dirs or build paths around them — always go through `profile_store` (or `global_db_dir()`/`global_cfg_dir()`/`global_auth_dir()` when you deliberately want the shared dir regardless). Rename/delete of a profile moves/purges the auth dir too (Logins bucket) — see `profile_store.rename`/`delete`.

## On-demand creation

The repo's `auth/`, `db/`, `cfg/`, and `log/` directories are gone — everything is under the platformdirs root. **`<root>/db/` and `<root>/cfg/` do not exist on a fresh install** — they are created on demand: `App.__init__` calls `ensure_settings_file()`/`ensure_theme_file()` (cfg/) and `PlaylistStore.ensure_playlists_file()` (creates `db/playlists.json`); `PlaylistStore._write()`, `DatabaseManager.get_playlist_db_path()`, and the store writers mkdir their parents defensively. Any new path under `db/` or `cfg/` must create its parent directory.

**No log path exists** — the old repo-root `log/` was dropped rather than relocated: `utils/logging_config.py` configures a single stderr `StreamHandler` (`basicConfig` with no `filename`), so the app writes no log file anywhere, under the config root or otherwise. Nothing rotates and nothing is retained; diagnostics live in the terminal (or the compositor shortcut's terminal) for the lifetime of the process. Adding a persisted log would be a new feature, and it would need a path decision here plus a retention policy.