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
| `auth_file` | no | `PluginInfo.auth_path` / `auth_paths` -> `services.auth_setup.delete_platform_credentials()`, `ui.login_ui` | credential filename inside the platformdirs auth dir, e.g. `yt_music.json`. Plugins must READ/WRITE the same profile-aware path (lazy `profile_store.auth_dir()` resolver with a shared-dir fallback for standalone use + one-time migration of a legacy shared copy, see `lastfm/lastfm.py` / `deezer/deezer.py` / `soundcloud/soundcloud.py` / `spotify/spotify.py` / `youtube_music/youtube_music.py`) — a hardcoded shared dir diverges from the app's delete path under a captured profile and lets credentials survive logout/uninstall |
| `auth_file_fallbacks` | no | `PluginInfo.auth_paths` -> `delete_platform_credentials()`, `integration_manager.uninstall_platform_data()` | extra repo-root-relative paths that may hold the credential (legacy fallback copies). Deleted on logout/uninstall so a stale copy can't re-authenticate the platform. |
| `login_module` / `login_class` | no | `PluginInfo.import_login()` -> `ui.login_ui` | a callable `(parent, on_success)` that renders the platform's login tile; lets a downloaded plugin get a login tile with no core change. Without it the login dialog uses its built-in handler for the three bundled platforms. |
| `login_logo` | no | `PluginInfo.login_logo_path` / `logo_path` -> `ui.login_ui`, `ui.manage_integrations_ui` | logo file relative to the plugin directory, overriding the standard `logo.png`; see the logo convention below. Software that ships the standard `logo.png` omits it. Falls back to a generic placeholder. |
| `receiver_module` / `receiver_class` | extension-type (and hybrid add-flow platforms) | `PluginInfo.import_receiver_class()` / `build_receiver(**kwargs)` | receiver manager handed to the flow. `KeybindController._ensure_initialized` builds it for ANY plugin that declares a `receiver_class` (not just `flow_type == "extension"`), so a `flow_type: "api"` platform like SoundCloud can still receive a browser-extension receiver for a hybrid capture path. |
| `receiver_port` | extension/hybrid | `PluginInfo.build_receiver()` | localhost port for the URL receiver. Pinned jointly with the browser extension's manifest; see [URL receiver protocol](#url-receiver-protocol) |
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

## Integration quirks

Core-relevant behavior of the bundled platforms. Per-platform internals
beyond these stay in each plugin's own repository.

- **YouTube Music auth**: Logs in via the in-app dialog (paste Authorization + Cookie, VERIFY-FIRST — `auth_setup.save_and_verify_ytmusic_auth`) which writes the primary `yt_music.json` headers file, or via the terminal `ytmusicapi browser` flow which writes legacy `browser.json`. Both names (plus a dev fallback) are searched by the auth manager — resolved profile-aware through `profile_store.auth_dir()` (`_resolve_auth_dir`, shared-dir fallback for standalone use) with a one-time migration of legacy shared `yt_music.json`/`browser.json` into the profile slot on load; the app's auth-ready poll (`login_ui._browser_file_ready`) mirrors the plugin's `browser_file_candidates()` order. The manifest declares both names so logout/uninstall removes them. The `ytmusicapi.get_library_playlists` method gets an instance-level MethodType patch with a fallback implementation installed on each client at auth time (`youtube_music.py`, NOT at import time). On macOS, "open terminal" uses AppleScript (`utils/platform.py`) — do not change it back to `open -a Terminal <dir>`, which opens Finder.
- **Spotify auth**: Expects `spotify.json` with `client_id`, `client_secret`, `refresh_token`. Tokens are auto-refreshed; new refresh tokens are persisted back to disk. **All writes go through `save_spotify_credentials_file()` in `spotify.py`** (600 perms) — don't duplicate the write logic in new code.
- **Add-flow invariant**: the keybind flows (`integrations/*/flow.py`) add to the **platform API first** and abort on failure — a `False` return or missing playlist ID raises, `on_error` fires, and the song is **never** written only to the local DB. Keep this ordering; a platform failure must not leave a "successful" local entry.
- **Uninstall ordering (login window → Manage)**: `ui/manage_integrations_ui.py` orchestrates, `services/integration_manager.uninstall_platform_data()` does the disk work ("with database, etc."). The order is fixed: close the platform's playlist cards first (per-card keybind unregister + store entry + song DB via `close_main_frame`), then stop the flow/receiver (`KeybindController.update_credentials(refreshed_ids=[platform])`), then unregister the live `PluginRegistry`/`IntegrationRegistry` objects, and only then delete credentials, registry entries, `db/<platform>/`, and duplicate-queue records. Reversing it lets a live listener or flow resurrect the platform mid-cleanup.
- **Spotify flow**: Uses `GET /me/player/currently-playing` instead of URL receiver — no local server needed.
- **ytmusicapi is optional**: the plugin flow modules use `from __future__ import annotations` so they import without `ytmusicapi` installed (the import happens lazily inside methods). Don't add a top-level `from ytmusicapi import ...` in service-layer files either.
- **Manage-dialog auth probe**: the Manage dialog's status refresh runs a live authenticated probe per installed integration (`services/health_check.probe_authenticated`). The TCP reachability banner only proves the endpoint accepts connects, not that the OAuth token is alive. The core probes the public handles it already uses for the bundled platforms (`spotify_api.get_me()`, `yt_client.get_library_playlists(limit=1)`, `soundcloud_api.get_me()`, `deezer_api.get_me()`, `client.get_loved_tracks(max_pages=1)` when a username is set). A plugin may instead implement its own `probe_authenticated()` method (no arguments; duck-typed, absence degrades gracefully to no verdict): return `True` (auth OK), `False` (auth failed), `None` (no opinion / no credentials), or an `(ok, detail)` tuple with a short best-effort reason.

### URL receiver protocol

The YouTube Music flow starts a **plain HTTP** Flask server on `localhost`
(port from `plugin.json` `"receiver_port"`, fallback `DEFAULT_RECEIVER_PORT` in
the receiver module). Short-lived (only while waiting for a URL, ~30 s).
Pull-based protocol, **token-authenticated**:

1. Flow controller starts the server and calls `set_waiting(True)`; the server generates a fresh per-flow token.
2. Extension polls `GET /status` → `{"ready": true, "token": "..."}`.
3. Extension POSTs `/receive-url` with header `X-PM-Token: <token>`.
4. Server validates the token (403 otherwise), consumes the URL, shuts down.

CORS is restricted to `https://music.youtube.com`. **The port is pinned in two
places that must stay in sync**: `integrations/youtube_music/plugin.json`
`"receiver_port"` (Python side; `PluginInfo.build_receiver()` passes it to the
receiver) and
`integrations/youtube_music/youtube-music-extension/manifest.json`
`host_permissions` (extension side). `content.js` derives its candidate server
URLs from the manifest at runtime (`chrome.runtime.getManifest()`) — no
hardcoded port there.

The same two-file pin applies to every extension platform, and the ports do
not repeat: YouTube Music 5000, SoundCloud 5001, Deezer 5002, Tidal 5003
(`plugin.json` `"receiver_port"` + the extension manifest's
`http://127.0.0.1:<port>/*` host permission).

Every receiver hardens the local server on two more axes:

- **Loopback bind only**. `PluginInfo.build_receiver()` pins
  `host="127.0.0.1"` from the core side (an explicit caller kwarg still
  overrides it, e.g. for tests), so a receiver can never default to
  `0.0.0.0` and expose the token-protected endpoints to the LAN.
- **Host-header allowlist**. Each receiver's Flask app refuses any request
  whose `Host` header is not `127.0.0.1[:port]` / `localhost[:port]` with
  HTTP 403 in a `before_request` hook (before any route code, CORS
  preflights included). This closes the DNS-rebinding window: a crafted
  page whose attacker domain resolves to 127.0.0.1 would otherwise be a
  well-formed (to werkzeug) `Host` value that passes both the token check
  machinery's browser CORS and the server — CORS only stops browsers from
  *reading* cross-origin responses, it never filters what reaches the
  server.

**Port collisions are reported, not swallowed.** A flow's `_start_receiver`
catches the `OSError(EADDRINUSE)` from `start()` and carries on, so the
capture then waits out its full ~30 s timeout and blames the extension for
delivering nothing. Before the flow runs, `services/receiver_port.conflict_for()`
probes the manifest's port instead (loopback bind with `SO_REUSEADDR`, so it
matches what werkzeug would do and does not trip over our own `TIME_WAIT`
sockets). On a collision the add is aborted before any thread starts, with:

- the card status and the status row showing the `receiver_port_busy` code
  (`tr_status` row in `DEFAULT_STRINGS`);
- the full diagnostic logged as a warning and stored via
  `duplicate_queue.record_error`, so it lands in the activity window's
  Errors tab, naming the port and the holding process (best-effort
  `/proc` lookup, Linux only, skipped when it is our own already-running
  receiver);
- `on_reset` before `on_status`, so the card's status label is not wiped by
  the reset.

`conflict_for()` returns `None` (meaning "go ahead") when the platform
declares no port, has no receiver to start, its `flow_type` is not
`"extension"`, or its receiver is already running and therefore holds the
port on our behalf. The `flow_type` gate matters: a hybrid/api flow decides
for itself whether to consult the receiver (SoundCloud declares a receiver
on 5001 but reads the API directly under `[soundcloud] capture_mode =
api`), so a busy port does not imply a failed capture. Only `"extension"`
guarantees the bind, since `_build_flow` refuses to build such a flow
without a receiver. The bias is deliberate: a missed diagnostic falls back
to today's behaviour, while a false one would abort a working add.

The probe is a race by nature; a port grabbed between the probe and the
bind is still handled by the flow's own error path. Core reports the
collision, it does not fix it: the port cannot be moved from one place (see
the sync list above).

### Release signing (supply-chain integrity)

Catalog installs (`--install`, the Manage dialog's install/update) fetch
the plugin repo's **latest GitHub release asset** (`plugin.tar.gz` from
`/releases/latest/download/<asset>`) plus its detached minisign signature
(`plugin.tar.gz.minisig`) and verify the archive before anything is
extracted or staged. See `services/integration_manager.py` / the
`IntegrationRepo.asset_name` field and `services/plugin_signing.py`.

Policy is **fail-closed** (default `[security] verify_plugin_signatures =
on` in `cfg/settings.ini`, see docs/config.md):

- No release yet → HTTP 404 on the asset → install fails (normal).
- Release without the attached signature → HTTP 404 on the `.minisig` →
  reported as "no minisign signature" and refused.
- Signature fails verification (tampered archive, wrong key, corrupted
  file) → refused; nothing is written.
- Pinned key missing/unparseable, or no verifier backend available
  (`py-minisign` package or the `minisign` CLI) → refused.
- `verify_plugin_signatures = off` restores the historical unverified
  installs — a documented admin-visible bypass only; there is no silent
  path. Logs warn loudly on the unverified path.

Trust anchor: the public verification key is pinned in the core repo at
`minisign.pub` (tracked — that file is the trust anchor, treat it like a
dependency's hash). The matching **secret key lives offline with the
maintainer and never touches GitHub**: a fully compromised plugin repo or
GitHub account still cannot mint a valid signature. Rotating the anchor
is a one-file change (replace `minisign.pub`); releases signed with an
older key then fail verification until re-signed, so rotate deliberately.

Release process (per plugin repo, maintainer machine, once per release):

```bash
# 1. build the package: a tarball whose root is the plugin tree
#    (the loader accepts a single top-level wrapper dir)
tar -czf plugin.tar.gz -C <package-dir> .
# 2. sign it with the secret key (prompts for the key password)
minisign -Sm plugin.tar.gz -s <secret-key-file> -p minisign.pub
# 3. create the GitHub release (tag = integer per the version
#    convention) and attach BOTH files:
#      plugin.tar.gz
#      plugin.tar.gz.minisig
```

Verification in the app uses `py-minisign` (the `minisign` Python
package, a core dependency), falling back to the `minisign` CLI
(`minisign -Vm <archive> -p <pubkey> -x <sigfile>`).

Hand-dropped plugins (a directory copied into `integrations/` by hand)
are **not** covered by release signing: the local filesystem the app is
launched from is the trust boundary for local edits.

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
