# Settings reference (`cfg/settings.ini`)

`DEFAULT_SETTINGS` in `utils/config.py` defines the **entire** settings surface (booleans + values, optional-section defaults included): `center_windows`, `auto_resize`, `remember_geometry` (restore last window size/position on launch; saved via a debounced `<Configure>` binding + a flush in `App.cleanup()`, skipped while maximized/fullscreen — see `utils/window.py`), `fullscreen` (start the window fullscreen; `--fullscreen` overrides per run; window-local F11 toggles it, no global grab), `global_listener`, `hide_to_tray` (the last enables hide-to-tray via `services/tray.py`), `start_in_tray` (launch hidden in the tray when the tray actually starts — falls back to a visible window otherwise), `confirm_on_song_remove` (ask before the showcase ✕ deletes a song — a plain yes/no dialog; full removal is the only action, there is no "keep db" for songs), `confirm_on_playlist_remove` (ask before a playlist card is closed — gates the existing Cancel/Keep-DB/Confirm dialog), `confirm_on_quit` (ask before quitting — shows the Cancel / Hide to tray / Quit dialog on both quit sources, the window ✕ and the tray "Quit" item; only consulted when `hide_to_tray` is on **and** a tray backend actually started, since otherwise there is nothing to hide into — resolved by `controllers.app_controller.should_confirm_quit`, dialog in `ui.dialogs.confirm_quit`), `showcase_log` (the per-card log row), `show_pin_buttons` (per-card pin/unpin buttons next to the playlist ✕ — toggled live from Settings), `show_playlist_numbers` (the registry number before each card's name — the same 1-based number `playlistmanager --list` prints: raw registry order, so it skips cards hidden for a missing platform plugin and ignores the grid sort/pin order; rendered as a separate header label, never inside the name label's text, which is the card's identity lookup key — default **off**, toggled live from Settings → Appearance, numbering resolved by `services/playlist_store.registry_numbers` via `ui/card_grid.refresh_card_numbers`), `playlist_stats` (song count/followers/duration row), `like_button` + `scrobble_on_add` + `scrobble_keybind` (Last.fm side effects — all default **off**, and don't flip their defaults; scrobbling is privacy-relevant by design), plus value sections: `ui_scale` (`value` key), `showcase` (`count` — last-N-added-songs per card, "0" = off), `layout` (`columns` — grid columns, default "2", clamped 1-4, applied live from Settings), `scrobble` (`platforms` — per-platform auto-scrobble sources, comma-separated enabled platform ids; empty/absent = ALL installed, literal `"none"` = NONE, else an explicit list — the Settings checklist under "Scrobble added songs" writes it, listing only installed music platforms (lastfm excluded — it's the scrobble target) and showing "Install integration first" when no music platform is installed; read via `services/scrobble.scrobble_enabled_for`, excluding any platform whose manifest declares no flow so a service backend is never a scrobble *source*; while the scrobble *targets* are switched per platform — see the dynamic-section note below), `window` (`geometry` — last main-window "WxH+X+Y", validated on restore: size clamped to the screen, position must intersect it; best-effort under Wayland where the compositor owns placement), `duplicate_check` (`is_true` off + `title_threshold` "0.85" / `duration_tolerance` "5" — read via `services/duplicate_check.read_settings()`), `remove_playlist` (`default`: `remove`/`keep_db` — the playlist card-✕ behaviour when `confirm_on_playlist_remove` is off, resolved by `ui.card_grid.resolve_playlist_removal_mode`; `keep_db` deletes the registry entry but keeps the per-playlist SQLite file, mirroring the close dialog's Keep-DB button; the dialog itself is always used when the ask is on), `soundcloud` (`capture_mode`: `api`/`hybrid`/`extension` — see [modules.md](modules.md) SoundCloud row), `grid_sort` (`key` `name`/`platform`/`added`/`used` + `direction` `asc`/`desc` — written live by the sort combobox/arrow in the Settings dialog (`ui/settings_ui.py`), resolved by `services/playlist_sort.sort_playlists`; `added` = registry insertion order, `used` = per-playlist `last_used_at` stamps from `PlaylistStore.mark_used`, entries that were never used sort to the end; pinned playlists (`pinned: true` on the registry entry, toggled by the card pin button → `PlaylistStore.set_pinned`) float above the rest, the active key applying within each group), `update_check` (`interval`: `off`/`launch`/`12h`/`24h`/`7d`/`14d`/`30d`, default `launch` — how often a non-forced check runs, read by `utils/updater.py`; `off` = never (the archived boolean `is_true=no` is migrated to it once), `launch` = every startup, timed values check at most once per interval spaced by the `last_check` epoch stamp, written after each attempt; the Settings dialog picker (`ui/settings_ui.py`) writes `interval`, the manual "Check for updates now" button always bypasses it via `force=True`), and `thumbnails` (`mode`: `off`/`download`/`dedupe`/`cache`/`max` — data-saver modes, see [data-paths.md](data-paths.md); `--data-saver` forces `max` per run; plus `max_size` — the longest-edge pixel cap applied to every fetched image, `0` (default) = no cap, otherwise one of `128`/`256`/`512`/`1024`/`2048`, resolved by `utils.config.resolve_thumbnail_size_cap` and read per fetch by `ThumbnailService._cap()`; the cap is part of both cache keys, so changing it re-fetches instead of serving entries written under the previous value, and values outside the tuple fall back to `0`), `notification_bar` (`enabled` yes/no — show the fixed status row at all, default yes; `song_align` `left`/`center` — Artist - Song segment alignment, default center (`utils.config.resolve_notification_align`); `position` `top`/`below_header`/`bottom` — bar location, default below-header (`utils.config.resolve_notification_position`; `compute_notification_rows` assigns the root-grid rows per position); `duplicates` yes/no — surface queued near-duplicate prompts as inline Add / Don't-add actions in the bar, default yes; see [flows.md](flows.md) section 12). All booleans are read with `ConfigParser.getboolean()` (accepts `yes/no/true/false/1/0`); defaults are applied by `utils/config.py:ensure_settings_file()` (which merges missing sections/keys into existing user files without touching unknown legacy sections).

The scrobble ledger records which services accepted each scrobble (`db/scrobbles.json`, schema v3, `backends` — a v2 single `backend` string is read as a one-element list), and the remove-song path deletes the listen on each of them, so switching services between an add and a remove cannot address the wrong account.

### The one dynamic section: `[<platform id>] scrobbles`

Which services a scrobble goes to is a **set**, not a single value, so it is not one key under `[scrobble]`. It is a boolean in each service platform's *own* section:

```ini
[lastfm]
scrobbles = true
[listenbrainz]
scrobbles = false
```

`utils.config.SCROBBLES_OPTION` ("scrobbles") with `get_setting_bool(pid, option, fallback=True)` /
`set_setting_bool(pid, option, value)`; the option name is stable, the *section* is a plugin id, so
this is deliberately **absent from `DEFAULT_SETTINGS`** — the table cannot enumerate sections for
plugins that are not installed, and shipping one would leave a dead section behind on uninstall.
The same shape as the plugin-owned `[soundcloud] capture_mode`; the difference is that the core
reads this one, because "which services should my listens go to" is core policy, not a platform
capability. Read via `services/scrobble.scrobble_backend_enabled()`, used by
`services/scrobble.scrobble_backends(..., only_enabled=True)`.

Semantics, and why each part:

- **Absent = enabled.** A user who never touched the switch wants their listens recorded, and a
  backend that is installed and logged in has already said yes by being there. Turning one off is
  therefore always an explicit act, and a second backend starts working the moment it is logged
  into (no login-time "did you mean to switch?" write, which would also have resurrected a service
  the user had deliberately switched off).
- **A filter, not an assertion.** An enabled-but-unavailable id (uninstalled, logged out) is
  skipped along with the rest; it never turns into a refusal, so a stale `[lastfm] scrobbles = true`
  on a machine with Last.fm uninstalled cannot stop ListenBrainz from scrobbling.
- **Ignored on the delete path**, deliberately: the ledger records what actually received the
  listen, and a listen recorded before the user switched a service off is still on it. The switch
  governs *new* scrobbles, not cleanup of recorded ones.
- **Ignored on the love paths**, for the same kind of reason: a loved list is a read plus a write,
  not a scrobble, so a ListenBrainz-only user still gets a working heart on Last.fm.
- Writers/reader tolerate a hand-edited file: any spelling `ConfigParser` accepts
  (`yes/no/true/false/1/0`) reads correctly, and a value that is not a boolean falls back to
  enabled rather than raising or silently stopping scrobbling.

Settings → Integrations → Scrobbling is the UI for this: one checkbox per installed backend
("Scrobble to:") plus **Select all** / **Deselect all**, writing the key live (the resolver reads
`settings.ini` per scrobble, so a toggle applies to the next song with no restart). The checkbox
list is fed by `main_window` from `scrobble_backends(..., only_enabled=False)` — the same
resolver call the scrobble paths make, so the UI cannot drift from the real rules, and a switched-off
service still shows up (unchecked) so it can be switched back on.

## Plugin download signatures (`[security]`)

`verify_plugin_signatures` — plugin download authenticity
(release-asset signing, see docs/plugins.md "Release signing"):
- `on` — the default, **fail-closed**. Catalog installs/updates
  (`--install`, Manage dialog) refuse an archive whose detached
  `.minisig` signature does not verify against the pinned `minisign.pub`
  (missing signature, tampered archive, wrong key, missing key file, or
  no verifier backend available — `py-minisign` package or `minisign`
  CLI), with a user-facing error and nothing written to disk.
- `off` — restore the historical unverified installs. A **documented
  admin-visible bypass only**; there is no silent path. Logs warn on
  every unverified install.

`credential_keyring` — OS-keyring mirror for stored credentials
(services/credential_store.py, see docs/data-paths.md "Credentials at
rest (OS keyring)"):
- `on` — the default. Every core-originated credential write is also
  mirrored into the OS keyring (service `playlistmanager`, per-profile
  accounts), missing credential files are restored from the mirror at
  startup and on load, and logout/uninstall clears both copies. See
  data-paths.md for the plugin-file contract and the honest scope.
- `off` — never write to or read from the keyring (credentials stay in
  the 0600 auth-dir files, the historical behavior). Existing keyring
  entries are still cleared on logout/uninstall, so a previously
  mirrored entry can never survive a logout.

The toggle governs mirroring and restoring only. Credential-destroying
operations clear the mirror whatever it is set to: logout/uninstall,
profile delete (`delete_profile`) and profile rename
(`rename_profile`).

Only *usable* OS backends are trusted: `keyring`'s own fail backend
and the entire `keyrings.alt` package (plaintext reimplementations)
count as unavailable, and a functional write/read/delete probe must
succeed. Machines without a backend (headless Linux without a
secret-service daemon) degrade to file-only storage and log the
degradation once; the Settings → Integrations → Stored credentials
panel shows the live state.

`keyring` is an optional dependency (extra: `keyring`). The setting is
`on` by default but the library may simply be absent — the mirror then
stays off and credentials remain in the 0600 auth-dir files, exactly as
with `off`. Setting `on` does not install anything; the log names the
missing package separately from a missing daemon so the fix is obvious.

## Unknown sections

**Do not assume the INI contains only the sections above**: user files can carry legacy sections (e.g. a stale `toggle_frameless`). Settings writers (`_toggle_setting`) and readers must tolerate and preserve unknown sections.

## Theme settings

Theme settings live in `cfg/theme.ini` (see [theming.md](theming.md)). The theme picker dialog (`settings_theme_ui.py`) opens as a separate scrollable Toplevel from the main Settings dialog and writes the INI directly.