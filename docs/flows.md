# End-to-end flows

Traces of the main call chains, named file by file. Threads crossing into
tkinter always marshal with guarded `root.after(0, ...)` calls; the guard
pattern and its reasons are documented in [threading.md](threading.md)
and are not repeated per step here.

## 1. Startup (`python main.py` or `python -m app`)

`main.py` -> `app/main.py:main()` -> `_import_app()` -> `App.__init__`:

1. `tk.Tk()`, then `utils.scaling.init(root)` before any widget exists,
   then the option-database default font.
2. `PluginRegistry().discover()` (JSON scan only).
3. `credential_store.restore_all()` re-materializes any credential file
   the OS keyring still holds, **before** the integrations read the files.
4. `auth_setup.audit_credential_permissions(plugin_registry)` - the
   read-only permission audit (a `stat()` per credential path, no network,
   so it runs inline). It lands *after* `restore_all()` so the verdict
   describes the files actually in use, and the report is kept on
   `self.credential_perm_report`. A non-`ok` report logs one
   `service.credential_perm_loose` `user_log` line plus a per-path
   `WARNING`; the same report is what the Settings credentials panel
   shows (flow #11). Nothing is chmod-ed.
5. Per plugin: `import_integration()` + `import_auth_attr()` ->
   `IntegrationRegistry.register`. ImportError disables that platform.
6. `_bootstrap_auth()`: YouTube Music authenticates synchronously (local
   file read). Spotify runs on a daemon worker and swaps its API object
   in via `root.after(0, ...)`; whichever of login/refresh lands first
   wins.
7. `KeybindController(plugin_registry, integrations)`,
   `AppController(self)`, background `_migrate_playlist_schema` thread.
8. `MainWindow(root, integrations, keybind_controller, app_controller)`,
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
   `_ensure_initialized` only *builds* the receiver (no socket yet); the
   bind happens later inside `execute_flow`. So between the two,
   `handle_keybind` pre-flights the manifest port
   (`services.receiver_port.conflict_for`, only for `flow_type` "extension"
   platforms, which are the ones guaranteed to bind): if it is already taken
   by another process, the add is aborted before the flow thread starts, with
   the `receiver_port_busy` status code on the card and status row plus a
   `duplicate_queue.record_error` entry naming the port and its holder in
   the activity window's Errors tab. Without that check the flow swallows
   `EADDRINUSE` and the user waits out the ~30 s capture timeout only to
   be told the extension sent nothing.
4. `flow.execute_flow(...)`: the platform API add runs FIRST. A False
   return or missing playlist id raises, fires `on_error`, and nothing is
   written locally (invariant in [plugins.md](plugins.md) "Integration quirks").
   Tidal adds one more way to be refused: `POST /playlists/{uuid}/items`
   answers **2xx with an empty `addedItemIds`** when the track is not
   available in the account's country, so the flow reads the empty list as a
   failure and aborts the same way - a 2xx is not proof the song was added.
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

Tidal's capture (extension-type, port 5003) is the one that has to *resolve*
what the extension sent: Tidal has no now-playing endpoint in either API
version, so `TidalFlow._resolve_track` uses the numeric `track_id` when the
extension caught the song starting, and otherwise falls back to a catalogue
search on `title` + `artist` with the album as a tiebreaker. The extension
itself degrades in the same spirit (Media Session -> playback-info traffic ->
player DOM -> `<audio>` state), because none of those is guaranteed to be
readable on every page. A search candidate whose title neither equals nor
contains the reported one is refused (`score_track`): the wrong song landing
in a playlist is worse than "could not identify the track".

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
join the pynput thread; [threading.md](threading.md)), tray stop,
`MainWindow.cleanup`, `root.destroy`. Worker threads holding pending
`root.after` calls swallow the resulting TclError/RuntimeError.

## 8. Scrobble / like (service backends: Last.fm, ListenBrainz)

A scrobble backend is a *service* plugin (no add-flow); the shared actions
reach it through the duck-typed `ScrobbleCapable` capability
(`services/integration.py`). Init: `App._bootstrap_auth` authenticates it
synchronously by reading its credentials (no network); a login/refresh
(`refresh_auth`) reloads the file and rebuilds the client. Two exist -
`integrations/lastfm/` and `integrations/listenbrainz/`.

**Which services** is decided in one place,
`services/scrobble.scrobble_backends(integrations, required)`, never by
registry order: candidates are the installed integrations exposing every
capability the call site needs and reporting themselves authenticated,
ordered by platform id, and then filtered by that service's own switch in
`settings.ini` - `[lastfm] scrobbles = true`, `[listenbrainz] scrobbles =
false`, one boolean per platform in its **own** section (absent = enabled).
**Every** survivor receives the listen, so two services out of three is an
ordinary configuration and a third plugin needs no new setting; the
resolver logs one info line naming the set when more than one is selected.
A configured-but-unavailable service is skipped, not fatal: the switch is a
filter, not an assertion. Because the capability is part of the query,
ListenBrainz (scrobble + delete only - it has no love/hate feedback) is
eligible for the scrobble paths and ineligible for the heart.

- **Scrobble current song** - three triggers, one capture loop:
  GUI keybind (`kind="action"` -> `_handle_action_keybind` ->
  `_scrobble_current_action`), CLI `-s`
  (`cli.run_scrobble`), and silent auto-scrobble on add
  (`keybind_controller.on_success` / `cli._run_flow`, gated by the
  `scrobble_on_add` setting - defaults to **no**, like `like_button`).
  Each iterates the *playlist* platforms'
  capture paths (YT extension 30 s / Spotify API) off the main thread,
  then hands `song_data` to every selected backend's `scrobble()`, each of
  which returns the timestamp the service ACCEPTED (an ignored/rejected
  scrobble returns `None` and is a silent no-op for that service only - one
  dead service never costs the others their listen). The loop has three
  distinct outcomes, and the callers branch on them: `None` = nothing was
  playing, `(song, None)` = a song WAS playing and every backend refused
  it, `(song, ts)` = scrobbled. A *capture* failure (no song from a
  platform) falls through to the next source platform; a captured song
  every backend refused does not - re-capturing could only report a
  different song, or scrobble the same one twice, and reporting `None`
  would tell the user their player is idle when their scrobble service is
  what failed. The add paths record
  that timestamp - plus the **ids of the services that accepted it** - in
  the scrobble ledger (`services/scrobble_log.py`, `db/scrobbles.json`
  schema v3, keyed by platform + playlist_id + local song_id). `-s` prints
  the outcome and returns 0/1; the keybind path logs only.
- **Like button** (`showcase_manager._on_like_toggle`, shown only when the
  `like_button` setting is on) toggles love/unlove in a worker thread;
  `love()` and `scrobble()` are distinct actions - scrobbling never loves.
  The heart resolves with `resolve_scrobble_backend(integrations, required)`,
  which ignores the scrobble switch on purpose, so the row keeps using
  Last.fm while the scrobbles go to ListenBrainz (or to both).
  Heart glyphs are populated with ONE batched `get_loved_set()` fetch per
  showcase refresh (a single `user.getLovedTracks` call instead of one
  `track.getInfo` per row), which also warms `is_loved()`'s TTL'd cache.
- **Remove song** deletes the EXACT scrobble the row's add-flow created:
  the showcase remove worker looks up the ledger record by
  (platform, playlist_id, song_id) and calls
  `delete_scrobble(artist, title, timestamp)` on **every service the record
  names**, each addressed by id through
  `services.scrobble.scrobble_backend_for(integrations, required, id)`.
  That lookup deliberately ignores the scrobble switch: the switch governs
  NEW scrobbles, and a listen recorded before the user switched a service
  off is still sitting on it, so the cleanup has to be able to reach it
  (and addressing a service that never got the listen would delete nothing
  while stranding the real one). A schema-v1 record names no service at
  all - that is genuinely unknown, and the scrobble is left alone rather
  than guessed at. A missing record (auto-scrobble was off, row re-imported
  by a reload, ledger pruned) means the same. A timestamp-less delete would
  remove the track's most recent scrobble, possibly a legitimate listen, so
  it is never used. Deleting a playlist (`card_grid.close_main_frame`, `cli -p del`)
  or uninstalling a platform (`integration_manager`)
  prunes the ledger's matching records. Playlist delete also runs
  `duplicate_queue.purge_playlist(platform, playlist_id, name)` so pending
  duplicate decisions, pair-memory entries and error-log lines for the
  playlist do not linger in `db/extra.json` and resurface on re-add.

## 9. Profile switch / create / delete

Profile data paths are module-level constants bound on first import, so a
profile change **must** restart the app (see ``services/profile_store.py``
and [data-paths.md](data-paths.md)). The active
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
  active pointer if the renamed profile was active. Moves the profile's
  `auth/<old>/` dir along, and calls `credential_store.rename_profile(old,
  new)` so the OS-keyring mirror follows: accounts are `"<profile>:<file>"`
  (`services/credential_store.py`), so a mirror left under the old prefix
  would strand a live credential and be unreachable from the new name.
- **Delete**: Settings -> Profiles -> Delete -> confirmation dialog ->
  `profile_store.delete(name)`. Refuses `default` and the active profile.
  For an owned Playlists bucket it removes `db/profiles/<name>/` wholesale
  (playlists.json, extra.json, scrobbles.json, every `<platform>/*.db`);
  a shared bucket removes nothing (the shared data is owned by everyone).
  An owned Logins bucket purges `auth/<name>/`, and
  `credential_store.delete_profile(name)` sweeps the OS-keyring entries
  too - the mirror lives in the platform credential store, not on disk,
  so the rmtree cannot reach it. Immediate, no restart needed (the
  deleted profile is never active).

## 10. Manage dialog status refresh (authenticated probe)

`login_ui` "Manage" -> `ui/manage_integrations_ui.show_manage_dialog`
renders one row per platform. For every **installed** row whose
integration is live in the registry, `_refresh_rows` fires
`_background_auth_probe(pid)` (guarded per platform by
`_auth_checking` / `_auth_cache`, so a refresh storm probes each platform
at most once):

1. A daemon worker calls
   `services.health_check.probe_authenticated(integration)`.
2. The probe runs on an internal watchdog thread (bounded by
   `_AUTH_PROBE_TIMEOUT_S`, because the plugin HTTP layers do not all set
   socket timeouts) and makes a cheap authenticated API call:
   `get_me()` for Spotify / SoundCloud / Deezer / Tidal,
   `get_library_playlists(limit=1)` for YouTube Music (the same call the
   login verification uses), one page of `user.getLovedTracks` for
   Last.fm.  A plugin may supply its own `probe_authenticated()` method
   instead (duck-typed, see plugins.md).
3. The verdict `(ok, detail)` lands back via `_after_main` and re-renders
   the row: "Installed · auth OK" (green), "Installed · auth failed"
   (red), or plain "Installed" when the probe did not apply (`ok` is
   None - e.g. never authenticated this session).

The verdict catches the case a TCP probe cannot: the platform endpoint
accepts connections while the OAuth token is revoked/expired.  Download /
update / uninstall completion pops the per-platform verdict, so the next
refresh re-probes the changed installation.

## 11. Settings -> Stored credentials (paths & permissions, Integrations tab)

`main_window` opens `ui/settings_ui.show_settings_dialog` and hands in the
live `plugin_registry` (new `plugin_registry=` kwarg; without it the
section falls back to the hardcoded `auth_setup.PLATFORM_CREDENTIAL_FILES`
map).  The section renders, at dialog build time:

1. The profile-aware auth dir (`profile_store.auth_dir()`) as its own
   first row, plus an "Open auth dir" button calling
   `utils.platform.open_directory` (creates the dir with 0o700 first when
   absent - the same enforcement the YT auth flow applies).
2. One row per installed platform (`plugin_registry.get_all()`) with a
   declared `auth_file`; each manifest-declared path
   (`PluginInfo.auth_paths`: auth-dir file + repo-root fallbacks) is
   listed with its on-disk status from
   `services.auth_setup.credential_path_status(path)`:
   - exists + private (no group/other bits; the 0600/0700 policy) ->
     the octal mode in green;
   - exists + loose (group/other bits set - hand edit, restoring umask,
     shared copy) -> "{mode} - open permissions" in warning color;
   - missing -> "missing" in the default label color.

Above those rows the section runs the same aggregate check as the startup
audit, `services.auth_setup.audit_credential_permissions(plugin_registry)`
-> `services.health_check.audit_path_modes`, and renders **one** warning
line (`settings.cred_perm_warning`, pluralized on the finding count) when
any path grants bits beyond its 0600/0700 contract.  It re-runs per dialog
build rather than reusing the startup report, so a mode fixed since launch
no longer warns.  Silent when clean, and on platforms where
`health_check.supports_posix_modes()` is False (Windows) it reports
nothing at all.

The panel is a read-only view - login/logout/uninstall remain the
mutating paths, and nothing here repairs a mode.  Credential *writes*
still go through the per-platform 0600 writers / 0700 dir enforcement
(see modules.md); this section only surfaces what is actually on disk.

## 12. Notification bar (status row + duplicate prompts)

A fixed row (default: directly under the header) renders the last
flow event as `Playlist | Artist - Song | Status`.  The widget is
`ui/notification_bar.py`; `main_window` owns it and feeds it.  There is
no pub-sub plumbing for the bar - the event feed rides the call chains
that already exist:

1. **Keybind add-song** (`_make_keybind_callbacks`): `on_song_info`
   remembers the last non-empty artist/song into `main_window._notif_song`
   (status callbacks fire *before* the song info in the flow, so the bar
   cannot read the card labels); `on_status` forwards terminal status
   codes as distinct event kinds (`duplicate`/`exists`/`error`/opaque
   messages) to `notification_bar.show_event(...)` - the transient
   `busy`/`loading` states and the `added` code are skipped (the added
   outcome is re-rendered authoritatively by `on_song_added`), so the bar
   shows results, not progress.
2. **Duplicate prompts** (inline **Add** / **Don't add** buttons): the
   service layer has no UI hooks (the CLI writes the same
   `db/extra.json`), so prompts are poll-driven - the existing 2 s
   `_poll_activity_stamp` badge poll (extra.json mtime) calls
   `notification_bar.on_store_changed(pending, duplicates_enabled)`.
   The bar takes over only when it is idle or already showing a
   duplicate/scan result - an "added"/"exists"/"error" event stays
   visible until the next event.  The buttons fire
   `on_action(record, "add"|"dismiss")` through the same
   `_handle_activity_song` dispatcher the Activity window uses (dismiss
   additionally calls `clear_prompt()` so the bar does not linger on a
   resolved record).
3. **Pending add outcomes** (`_pending_add_worker`): success
   (`added`/`exists`/`duplicate`) and errors render bar events from the
   queued record's own fields (colors resolved on the UI thread inside
   the scheduled call - worker threads never read theme state).
4. **Manual duplicate scan** (`_run_duplicate_scan._publish`): a found
   count renders a `notif.scan_found` plural-status event (and still
   opens the Activity window).

Settings (`[notification_bar]`, see config.md): `enabled` toggles the
bar itself (grid show/hide), `song_align` re-anchors the Artist - Song
segment, `position` re-assigns the root-grid rows of header/bar/search/
cards (`utils.config.compute_notification_rows`; the search bar follows
via `SearchManager.set_row`), `duplicates` gates the inline prompts
(checkbox also exposed in the Settings Duplicate Check section).

**Desktop Notification (future)**: `show_event(kind, ...)` is the single
choke point every notification funnels through, so a later desktop sink
plugs in here without re-plumbing - it needs a new `desktop` setting key
and a `show_event` branch, nothing else.
