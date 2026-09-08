# End-to-end flows

Traces of the main call chains, named file by file. Threads crossing into
tkinter always marshal with guarded `root.after(0, ...)` calls; the guard
pattern and its reasons are documented in AGENTS.md (threading section)
and are not repeated per step here.

## 1. Startup (`python main.py` or `python -m app`)

`main.py` -> `app/main.py:main()` -> `_import_app()` -> `App.__init__`:

1. `tk.Tk()`, then `utils.scaling.init(root)` before any widget exists,
   then the option-database default font.
2. `PluginRegistry().discover()` (JSON scan only).
3. Per plugin: `import_integration()` + `import_auth_attr()` ->
   `IntegrationRegistry.register`. ImportError disables that platform.
4. `_bootstrap_auth()`: YouTube Music authenticates synchronously (local
   file read). Spotify runs on a daemon worker and swaps its API object
   in via `root.after(0, ...)`; whichever of login/refresh lands first
   wins.
5. `KeybindController(plugin_registry, integrations)`,
   `AppController(self)`, background `_migrate_playlist_schema` thread.
6. `MainWindow(root, integrations, keybind_controller, app_controller)`,
   `PlaylistStore.ensure_playlists_file()`, optional `center_window`.

## 2. Hotkey add-song (GUI)

1. pynput callback on the listener thread enters
   `KeybindController.handle_keybind(key)`.
2. `KeybindRegistry` lookup returns (playlist_name, platform,
   playlist_id).
3. `_ensure_initialized(platform_id, callbacks)` builds the platform flow
   once via `plugin.import_flow()`; extension-type (and receiver-declaring)
   flows also start the receiver and set waiting state. SoundCloud adds a
   capture step before the API add: its `flow.py` branches on
   `[soundcloud] capture_mode` - `api` reads `/me/recently-played`[0],
   `hybrid` polls for a browser-extension URL and falls back to
   recently-played on a receiver miss, `extension` waits for the extension
   only. A POST with `playing: false` is a paused-skip no-op and never
   falls back to recently-played.
4. `flow.execute_flow(...)`: the platform API add runs FIRST. A False
   return or missing playlist id raises, fires `on_error`, and nothing is
   written locally (invariant in AGENTS.md "Add-flow invariant").
5. Success writes through `SongManager` into
   `db/platform/<sanitized>_<hash8>.db`, then callbacks reach the UI via
   `root.after(0, ...)`.

Duplicate-check branch (only when `[duplicate_check] is_true`): between
the exact-`song_exists` check and step 4, both flows call the shared
`duplicate_check.resolve_near_duplicate(...)` (same artist + similar
title + close duration against the local mirror). Outcomes:
no match / pair already `"added"` or `"not_duplicate"` -> proceed with
step 4; `"dismissed"` -> success-status "exists" ("skipped similar"),
nothing added anywhere; fresh match -> the song record is parked in
`db/extra.json` and the flow finishes with success-status `"duplicate"`
("Dup?" card) WITHOUT touching the platform - nothing is half-added.
Resolution happens later in the Activity window: Add re-runs the real
flow (`MainWindow._pending_add_worker` via `KeybindController.get_flow`,
`skip_duplicate_check=True`), Don't add records the dismissal. The Add
worker acquires `KeybindController.flow_busy()` (non-blocking; a busy
flow re-queues the record with an error) so its capture cannot race an
in-flight keybind/scrobble flow's receiver on the same local port.
Flow errors append to `db/extra.json`'s error log (`on_error` ->
`duplicate_queue.record_error`) and surface in the window's Errors tab.

Flows with a receiver (extension-type, or hybrid SoundCloud) insert a
wait between steps 3 and 4: the receiver holds until the browser extension
POSTs the current URL, or the flow falls back / times out per its
capture-mode policy.

## 3. Headless add-song (`-a 1,2,3`)

`app/main.py` -> `cli.run_add(spec)` -> `resolve_targets` ->
`cli._init_platform(plugin_registry, pid)` (import + authenticate) ->
shared `flow.capture(timeout)` per platform batch -> `parse_playlist_url`
when the capture yields a URL -> playlist id resolution through the
integration -> `SongManager` write. No tkinter import executes anywhere
on this path.

## 4. Headless playlist management (`-p add <URL>`, `--list`, refresh)

`run_add_url(url)`: `parse_playlist_url(registry)` picks the platform
from `url_hosts`, API fetches playlist details and tracks, then
`PlaylistStore.add_playlist` dedups by (platform, playlist_id) first.
`run_list` prints the store without touching any API. `run_refresh(spec)`
re-imports tracks through `PlaylistSyncService` helpers.

## 5. Add-playlist (GUI "+" button)

1. `MainWindow` handler -> `PlaylistController.open_playlist_dialog(attempt)`
   -> `ui.playlist_dialog.PlaylistDialog`.
2. Dialog lists `integration.get_library_playlists()` fetched on a worker;
   covers download via `ThumbnailService.fetch_image`, convert via
   `to_photoimage` on the main thread.
3. Selection -> `PlaylistSyncService.import_tracks` worker: fetch tracks,
   `DatabaseManager.get_playlist_db_path` creates the DB file,
   `SongManager` bulk-inserts, `PlaylistStore.add_playlist` registers.
4. Done-callback inserts the card on the UI thread;
   `CardGridManager.create_main_frame` renders it.

Each browse-compatible integration also exposes a *virtual* "Liked songs"
entry (Spotify "Liked Songs", SoundCloud "Liked Tracks", Deezer "Loved
Tracks") even though none of them is a real playlist — the platform keeps
these under a library/like collection, not a playlist object.  The picker
shows them with a sentinel `playlistId` (``__liked__``); selecting one
registers it in the store like any other playlist, and the keybind flow
routes the Sync step to the platform's *like/save track* API
(``PUT /me/tracks`` / ``PUT /me/track-likes/{id}`` / ``PUT /user/me/tracks``)
instead of a playlist-add call, keeping the add-flow invariant (platform
first, abort on failure).  The local per-playlist DB still mirrors the
liked set so the duplicate/remove paths work unchanged.  YouTube Music
is the exception — its "Liked songs" (id ``LM``) is a real playlist.

## 6. Reload playlist card

Card menu -> `PlaylistSyncService.reload_database_sync`: main thread
closes cached connections, `DatabaseManager.delete_playlist_db(name,
platform, playlist_id)` removes file + sidecars and drops every thread's
cached handle, worker re-fetches and rebuilds songs, UI refreshes via
`root.after`.

## 7. Quit

`App.quit_app` -> cleanup chain: `kc.stop_listener(wait=False)` (never
join the pynput thread; AGENTS.md threading section), tray stop,
`MainWindow.cleanup`, `root.destroy`. Worker threads holding pending
`root.after` calls swallow the resulting TclError/RuntimeError.

## 8. Scrobble / like (Last.fm service)

Last.fm is a *service* plugin (no add-flow); the shared actions reach it
through the duck-typed `ScrobbleCapable` capability
(`services/integration.py`). Init: `App._bootstrap_auth` authenticates it
synchronously by reading `lastfm.json` (no network); a login/refresh
(`refresh_auth`) reloads the file and rebuilds the client.

- **Scrobble current song** - three triggers, one capture loop:
  GUI keybind (`kind="action"` -> `_handle_action_keybind` ->
  `_scrobble_current_action`), CLI `-s`
  (`cli.run_scrobble`), and silent auto-scrobble on add
  (`keybind_controller.on_success` / `cli._run_flow`, gated by the
  `scrobble_on_add` setting - defaults to **no**, like `like_button`).
  Each iterates the *playlist* platforms'
  capture paths (YT extension 30 s / Spotify API) off the main thread,
  then hands `song_data` to `service.scrobble()`, which returns the
  backend-ACCEPTED timestamp (an ignored/rejected scrobble returns
  `None` and is a silent no-op). The add paths record that timestamp in
  the scrobble ledger (`services/scrobble_log.py`,
  `db/scrobbles.json`, keyed by platform + playlist_id + local song_id).
  `-s` prints the outcome and returns 0/1; the keybind path logs only.
- **Like button** (`showcase_manager._on_like_toggle`, shown only when the
  `like_button` setting is on) toggles love/unlove in a worker thread;
  `love()` and `scrobble()` are distinct actions - scrobbling never loves.
  Heart glyphs are populated with ONE batched `get_loved_set()` fetch per
  showcase refresh (a single `user.getLovedTracks` call instead of one
  `track.getInfo` per row), which also warms `is_loved()`'s TTL'd cache.
- **Remove song** deletes the EXACT scrobble the row's add-flow created:
  the showcase remove worker looks up the ledger record by
  (platform, playlist_id, song_id) and calls
  `delete_scrobble(artist, title, timestamp)` - a timestamp-less delete
  would remove the track's most recent scrobble, possibly a legitimate
  listen, so it is never used. A missing record (auto-scrobble was off,
  row re-imported by a reload, ledger pruned) means "leave the scrobble
  alone". Deleting a playlist (`card_grid.close_main_frame`, `cli -p del`)
  or uninstalling a platform (`integration_manager`)
  prunes the ledger's matching records. Playlist delete also runs
  `duplicate_queue.purge_playlist(platform, playlist_id, name)` so pending
  duplicate decisions, pair-memory entries and error-log lines for the
  playlist do not linger in `db/extra.json` and resurface on re-add.

## 9. Profile switch / create / delete

Profile data paths are module-level constants bound on first import, so a
profile change **must** restart the app (see ``services/profile_store.py``
and AGENTS.md "Key data paths"). The active
name is read by `services/profile_store.initialize()` during
`utils/config.py` import, before `App.__init__` runs.

- **Switch**: Settings -> Profiles -> pick another profile in the
  Combobox -> confirm -> the app warns it must restart and offers
  **Restart now** / **Later**. `profile_store.set_active(name)` (persists
  `cfg/profile.json`) runs **only** on **Restart now**, then
  `AppController.restart_app()` runs `App.cleanup()` (stops the pynput
  listener / receiver, drops thread-cached SQLite conns), destroys the
  root, and re-launches `sys.executable main.py` (or the `playlistmanager`
  command). Choosing **Later** leaves the profile inactive and deletable.
  The new process re-reads `active_profile()` at import and rebuilds
  everything from the new profile's paths: `_bootstrap_auth` (new
  `auth_dir()`), `MainWindow.setup()` (new `db/playlists.json` + cards +
  keybinds), `load_theme()` (new `cfg/theme.ini`), settings toggles (new
  `cfg/settings.ini`).
- **Create**: Settings -> Profiles -> Add -> `profiles_ui` dialog (name +
  three bucket checkboxes) -> `profile_store.create(name, logins,
  playlists, settings)` = validate + persist `db/profiles.json` +
  `_copy_bucket_data` (copies, never moves, the current shared
  `db/`/`cfg/`/auth contents into the new `profiles/<name>/` slot so the
  profile starts from today's state). `create()` does **not** activate;
  the dialog fires `on_created(name)`, Settings prompts to restart and
  calls `set_active(name)` only on **Restart now** (so a declined restart
  leaves the new profile deletable).
- **Edit buckets**: `profiles_ui` dialog -> `set_bucket(name, bucket, on)`.
  Flipping a bucket from off to on snapshots the current shared data into
  the slot; off leaves the slot on disk but hidden (no data destroy).
- **Rename**: `profile_store.rename(old, new)` - validates, moves the
  metadata entry and the on-disk `profiles/<name>/` dirs, and updates the
  active pointer if the renamed profile was active.
- **Delete**: Settings -> Profiles -> Delete -> confirmation dialog ->
  `profile_store.delete(name)`. Refuses `default` and the active profile.
  For an owned Playlists bucket it removes `db/profiles/<name>/` wholesale
  (playlists.json, extra.json, scrobbles.json, every `<platform>/*.db`);
  a shared bucket removes nothing (the shared data is owned by everyone).
  Immediate, no restart needed (the deleted profile is never active).
