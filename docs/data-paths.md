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
| Thumbnail cache | `platformdirs.user_cache_dir("playlistmanager")` — `~/.cache/playlistmanager/` on Linux (NOT the config root; **not profile-aware** — a thumbnail is the same bytes whichever profile fetched it): `playlists/` (URL-keyed covers, `md5(url|WxH).png`), `songs/` (URL-keyed or — in `dedupe` mode — identity-keyed song thumbs plus `index.json`), `full/` (original images, cleared on image-view close) |
| Palette spec (docs) | `theme.txt` |
| Auth credentials | `<root>/auth/` |
| Active profile + metadata | `<root>/cfg/profile.json` (name), `<root>/db/profiles.json` (bucket capture per profile) |

## Profile awareness

**Every path above is profile-aware.** `services/profile_store.py` is the **single resolver** for all of them — `db_dir()`/`cfg_dir()`/`auth_dir()` return the shared dirs (table values) when the **active profile** doesn't capture that bucket, and the profile slot when it does: `db/profiles/<name>/`, `cfg/profiles/<name>/`, `auth/<name>/`. `utils/config.py` imports `profile_store` and calls `initialize()` at load (a deliberate upward edge — see [modules.md](modules.md) "Cross-layer exceptions"), so every module's path binds to the active profile automatically.

Never hardcode the shared dirs or build paths around them — always go through `profile_store` (or `global_db_dir()`/`global_cfg_dir()`/`global_auth_dir()` when you deliberately want the shared dir regardless). Rename/delete of a profile moves/purges the auth dir too (Logins bucket) — see `profile_store.rename`/`delete`.

## On-demand creation

The repo's `auth/`, `db/`, `cfg/`, and `log/` directories are gone — everything is under the platformdirs root. **`<root>/db/` and `<root>/cfg/` do not exist on a fresh install** — they are created on demand: `App.__init__` calls `ensure_settings_file()`/`ensure_theme_file()` (cfg/) and `PlaylistStore.ensure_playlists_file()` (creates `db/playlists.json`); `PlaylistStore._write()`, `DatabaseManager.get_playlist_db_path()`, and the store writers mkdir their parents defensively. Any new path under `db/` or `cfg/` must create its parent directory.