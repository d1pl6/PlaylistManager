# Plugin system contract

A platform plugin is a directory under `integrations/<dir>/` containing a
`plugin.json` manifest. `app/plugin_loader.py` scans manifests at startup
without importing any plugin Python. Class references resolve lazily, per
`PluginInfo` instance, and each resolution failure degrades only that
platform.

Imports key on the DIRECTORY name (`integrations.youtube_music.flow`),
never on the manifest `id`. The loader inserts the repo root and `app/`
on `sys.path`.

## plugin.json schema

| Key | Required | Read by | Purpose |
|---|---|---|---|
| `id` | yes | registry keys everywhere: `IntegrationRegistry.get(pid)`, `KeybindController._ensure_initialized(platform_id)`, CLI platform args | stable platform id, e.g. `youtube_music` |
| `display_name` | yes | login dialog tiles, log lines | human-readable name |
| `integration_class` | yes | `PluginInfo.import_integration()` -> `App.__init__` | class in `integration.py` subclassing `BaseIntegration`; built as `cls(auth_manager=...)`. The module is fixed (`integration.py`, hardcoded in the loader), only the class name is a manifest field |
| `flow_type` | yes | `KeybindController._ensure_initialized`, `cli.run_add` | `"extension"` = local HTTP receiver fed by a browser extension; `"api"` = direct REST polling, no receiver. A `flow_type: "api"` plugin that ALSO declares a `receiver_class` (SoundCloud) gets the receiver injected too, and its `flow.py` decides when to consult it via a per-platform capture-mode config (`[soundcloud] capture_mode` = api / hybrid / extension) |
| `flow_module` / `flow_class` | yes | `PluginInfo.import_flow()` -> `KeybindController` (and CLI) | `BaseFlowController` subclass in `flow.py` |
| `auth_module` / `auth_attr` | no | `PluginInfo.import_auth_attr()` | module file + attribute holding the auth manager; omit for credential-free platforms |
| `auth_file` | no | `PluginInfo.auth_path` / `auth_paths` -> `services.auth_setup.delete_platform_credentials()`, `ui.login_ui` | credential filename inside the platformdirs auth dir, e.g. `browser.json` |
| `auth_file_fallbacks` | no | `PluginInfo.auth_paths` -> `delete_platform_credentials()`, `integration_manager.uninstall_platform_data()` | extra repo-root-relative paths that may hold the credential (legacy fallback copies). Deleted on logout/uninstall so a stale copy can't re-authenticate the platform. |
| `login_module` / `login_class` | no | `PluginInfo.import_login()` -> `ui.login_ui` | a callable `(parent, on_success)` that renders the platform's login tile; lets a downloaded plugin get a login tile with no core change. Without it the login dialog uses its built-in handler for the three bundled platforms. |
| `login_logo` | no | `PluginInfo.login_logo_path` / `logo_path` -> `ui.login_ui`, `ui.manage_integrations_ui` | logo file relative to the plugin directory, overriding the standard `logo.png`; see the logo convention below. Software that ships the standard `logo.png` omits it. Falls back to a generic placeholder. |
| `receiver_module` / `receiver_class` | extension-type (and hybrid add-flow platforms) | `PluginInfo.import_receiver_class()` / `build_receiver(**kwargs)` | receiver manager handed to the flow. `KeybindController._ensure_initialized` builds it for ANY plugin that declares a `receiver_class` (not just `flow_type == "extension"`), so a `flow_type: "api"` platform like SoundCloud can still receive a browser-extension receiver for a hybrid capture path. |
| `receiver_port` | extension/hybrid | `PluginInfo.build_receiver()` | localhost port for the URL receiver. Pinned jointly with the browser extension's manifest; see AGENTS.md "URL receiver" |
| `url_hosts` | no | `services.playlist_url.parse_playlist_url()` | hosts whose URLs resolve to this platform. Hosts are matched against the shared URL shapes (query/path/URI); a platform with path-form URLs (every path on the host is a resource URL, SoundCloud) opts in via core's `_PATH_FORM_PLATFORMS` set, and the stored id is the URL path (`user/sets/slug`), resolved through the platform's `/resolve` on first use |
| `playlist_url_template` | no | `services.playlist_url.build_playlist_url()` | browseable playlist URL template with `{host}` / `{id}` placeholders. The manifest is the single source of URL shapes, no core fallback list exists |
| `song_url_template` | no | `services.playlist_url.build_song_url()` | browseable song URL template, same placeholders |
| `version` | no | `ui.manage_integrations_ui` | non-negative integer compared against GitHub releases for in-app updates; omit to disable update checks |

Logo convention: every plugin directory is expected to ship a standard
`integrations/<dir>/logo.png`. `PluginInfo.logo_path` resolves it (the
manifest `login_logo`, when declared, takes precedence); `ui.login_ui`
renders it on the login tile and `ui.manage_integrations_ui` on the
integration-manager row. A plugin without a logo falls back to the
generic placeholder.

## Lifecycle

1. Scan. `PluginRegistry().discover()` globs `*/plugin.json` and parses
   JSON. A malformed manifest logs a warning and is skipped; nothing else
   happens.
2. Build integrations. `App.__init__` resolves `integration_class` and
   the auth manager per plugin inside try/except. An ImportError (missing
   optional dependency such as ytmusicapi) disables that one platform and
   startup continues.
3. Flows are never built at startup. The first keybind dispatch for a
   platform triggers `KeybindController._ensure_initialized`, which calls
   `import_flow()` once. Extension-type flows additionally receive the
   receiver and own its start/stop around the ~30 s wait window; any plugin
   declaring a `receiver_class` (including `flow_type: "api"` SoundCloud)
   gets the receiver injected the same way.
4. Credential rotation: `update_credentials(refreshed_ids)` swaps live
   clients into existing flows. A failed refresh leaves the flow in
   place; it does not rebuild it.
5. CLI uses the same lazy resolution via `cli._init_platform` and the
   `get_default_registry()` singleton (also the fallback inside
   `parse_playlist_url` when no registry is passed).

## Service-only plugins (no add-flow)

A plugin may be a *service* rather than a playlist-add platform: it
declares **no** `flow_type` / `flow_module` / `flow_class`, so the core
never builds it as an add-flow target. It contributes a capability the
core duck-types. The only current example is `integrations/lastfm/`
(Last.fm), which implements the `ScrobbleCapable` interface
(`services/integration.py`):

| Method | Used by | Purpose |
|---|---|---|
| `scrobble(song_data)` → `int \| None` | scrobble-on-add (keybind flow success, CLI batch add), CLI `-s`, `_scrobble_current_action` | record a scrobble for a song (does NOT love); returns the backend-ACCEPTED scrobble's Unix timestamp (seconds), or `None` when Last.fm ignored/rejected it or the round trip failed. The caller records that timestamp in the scrobble ledger (`services/scrobble_log.py`) so remove-song can delete exactly this scrobble |
| `love(artist, track)` | like button (`_on_like_toggle`) | add to the user's loved list (independent of scrobbling) |
| `unlove(artist, track)` | like button | remove from loved list |
| `is_loved(artist, track)` → `bool \| None` | show like state; cached (TTL'd LRU), fetched once per (artist, track) on a worker thread | True/False, or None when unknown/unauthenticated; only deterministic "not found" answers are cached, transient failures are not |
| `get_loved_set()` → `dict \| None` | showcase heart population (batched) | one paginated `user.getLovedTracks` fetch replacing N per-track `track.getInfo` calls; `{("artist", "track") -> True}`; also warms the per-track cache. `None` when unauthenticated or the fetch failed |
| `delete_scrobble(artist, track, timestamp=None)` | remove-song (lookup from the scrobble ledger; skipped when no record exists) | delete a scrobble; best-effort. *timestamp* is the ledger-recorded value: deleting without one removes the track's MOST RECENT scrobble, which may be one the user actually listened to, so the core never calls it timestamp-less |

A service plugin's `plugin.json` declares `id`, `display_name`,
`auth_file`, and `integration_class` (optionally `auth_module`/`auth_attr`);
`integration_class` subclasses `BaseIntegration` (`authenticate` /
`is_authenticated` / `refresh_auth`) plus the capability methods. Core
callers detect the capability with `getattr(integration, "<method>", None)`
- absence degrades gracefully (no like button, no scrobbling).

Because it is a service (no flow), the keybind controller's
`_scrobble_current_action` and `cli.run_scrobble` iterate the *playlist*
platforms to capture the currently-playing song, then hand its
`song_data` to the service's `scrobble()`. A service plugin itself is
never a capture source.

A service integration must set `supports_playlists = False` on its
`BaseIntegration` subclass (default is `True`). `IntegrationRegistry.
get_active()` filters on it, so the plugin is excluded from the
"Choose platform" add-playlist picker while still being returned by
`get_all()` for its service actions (like button, scrobbling).

## Adding a platform

1. Create `integrations/<name>/` with `plugin.json` following the
   schema above.
2. Subclass `BaseIntegration` (`integration.py`) and
   `BaseFlowController` (`flow.py`). Extension-type platforms also need a
   receiver module and a `capture(timeout)` implementation for CLI batch
   mode.
3. Declare the auth manager only if the platform needs credentials.
4. If the platform needs a login-dialog tile, declare `login_module` /
   `login_class` (a callable `(parent, on_success)`) and optionally
   `login_logo`; otherwise it is discoverable for keybinds but hidden
   from the login dialog.
5. Restart the app. No core registration step exists.

Per-platform internals are documented in each plugin's own repository,
not here.
