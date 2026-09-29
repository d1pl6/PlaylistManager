## Content
- [Debugging](#debugging)
- [Authentication](#authentication)
- [Plugins](#plugins)
- [Playlist management](#playlist-management)
- [Song add](#song-add)
- [Scrobble](#scrobble-lastfm-backend-independent-of-any-playlist)
- [Data saver, fullscreen](#other)
- [Global install](#global-install-for-compositor-shortcuts)

### Debugging:
```
--verbose / -v  # for verbose logging.
--debug / -vv   # DEBUG and above (everything)
--trace / -vvv  # TRACE and above + third-party debug loggers (requests / urllib3 / ytmusicapi / etc)
```


### Authentication:
```bash
playlistmanager --login youtube_music       # open terminal ('ytmusicapi browser')
playlistmanager --login spotify             # follow prompt
playlistmanager --login spotify --client-id <ID> --client-secret <SECRET> --refresh-token <TOKEN> # alternative

# keep the secret off the command line:
PLAYLISTMANAGER_SPOTIFY_CLIENT_ID=<ID> \
PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET=<SECRET> \
PLAYLISTMANAGER_SPOTIFY_REFRESH_TOKEN=<TOKEN> playlistmanager --login spotify

# ...or read it from a file (chmod 600): nothing is echoed, stdout keeps only the result line
playlistmanager --login spotify --client-id <ID> < ~/.secrets/spotify

playlistmanager --login deezer              # hidden ARL prompt, or PLAYLISTMANAGER_DEEZER_ARL=<ARL>
playlistmanager --login listenbrainz        # hidden token prompt, or PLAYLISTMANAGER_LISTENBRAINZ_TOKEN=<TOKEN>

# Tidal: client id + secret of the app you registered, then a browser PKCE login.
# No secret flags here (argv is world-readable), and no --refresh-token: the login mints it.
PLAYLISTMANAGER_TIDAL_CLIENT_ID=<ID> \
PLAYLISTMANAGER_TIDAL_CLIENT_SECRET=<SECRET> playlistmanager --login tidal
playlistmanager --login tidal               # same, from prompts (secret hidden)

playlistmanager --logout youtube_music      # delete browser.json + registry entries + db/youtube_music/
playlistmanager --logout spotify            # delete spotify.json + registry entries + db/spotify/
```

- --login youtube_music opens a terminal for the ytmusicapi browser flow; If no terminal can
  be launched, it opens the folder instead and prints manual instructions if no terminal emulator is installed.
- --login spotify takes the three credential options, the three PLAYLISTMANAGER_SPOTIFY_* env
  vars, or prompts for whatever is still missing (secret and token hidden; an empty
  refresh-token prompt reuses the stored token).
- --login soundcloud works the same with PLAYLISTMANAGER_SOUNDCLOUD_*.
  Credentials are verified against the platform API first and only persisted on success,
  so a failed login leaves previously working auth untouched.
- --login listenbrainz takes PLAYLISTMANAGER_LISTENBRAINZ_TOKEN or a hidden prompt. There is
  deliberately **no** --token flag: argv is world-readable through /proc/<pid>/cmdline, so the
  token gets the env var / prompt tiers only. The token is the user token from
  https://listenbrainz.org/settings/ (see INTEGRATIONS.MD).
- --login tidal takes PLAYLISTMANAGER_TIDAL_CLIENT_ID / _CLIENT_SECRET or prompts (secret hidden).
  There are deliberately no --client-id / --client-secret flags for it, same as the Deezer ARL and the
  ListenBrainz token. It then runs the OAuth 2.0 + PKCE login itself: it prints the authorize URL
  (which carries the loopback redirect URI `http://127.0.0.1:47831/callback` - register exactly that
  string in your app at https://developer.tidal.com/apps, Tidal rejects any other), serves the
  callback on 127.0.0.1 for a few minutes, and exchanges the code for a refresh token. The
  credentials are verified against the platform before anything is written. Headless is fine - the
  URL is printed, so open it on whatever machine has a browser.
- Both Last.fm and ListenBrainz are scrobble backends. Each one is switched on and off on its own
  in settings.ini, in that platform's own section:

      [lastfm]
      scrobbles = true
      [listenbrainz]
      scrobbles = false

  The key is absent by default, meaning enabled, so a freshly logged-in backend starts receiving
  listens immediately and turning one off is always deliberate. Every enabled backend gets the
  same listen (one failing never costs the others theirs), and Settings -> Integrations ->
  Scrobbling exposes the same thing as a checkbox per backend plus Select all / Deselect all -
  the CLI reads exactly those keys.
- Passing a secret: each credential is read in one order, flag then env var then prompt. An
  explicit flag wins so a script can override an exported variable for a single run, and a
  blank env var counts as unset.
- --logout is local-only and idempotent: it removes the platform’s credentials, unregisters
  its playlists, and deletes db/<platform>/. Nothing is revoked on the platform. If nothing
  remains, it prints no credentials found and exits 0. Re-add playlists after re-login with -p add <URL>.
- Unknown platform names exit with code 2.

### Plugins:
```
python -m app --install spotify            # download + install the Spotify plugin
python -m app --install all                # install every supported platform
python -m app --uninstall spotify          # remove the plugin + all its local data
python -m app --uninstall all              # remove every installed platform
```

- `--install <platform>` downloads the plugin into `integrations/<platform>/` (catalog-driven,
  the plugin_manager sources). It does **not** authenticate or register playlists - do that with
  `--login` / `-p add`. Re-running on an installed platform reports "updated" (idempotent).
- `--uninstall <platform>` is the CLI mirror of the Manage dialog's Uninstall: it deletes the
  plugin's credential files (including manifest fallback copies), its playlist registry entries,
  its local song databases under `db/<platform>/`, its duplicate-queue records, and the plugin
  folder itself. Nothing is revoked on the platform. A platform that is not installed prints
  "not installed" and is not an error.
- Both accept `all` to act on every supported platform. Unknown platform names exit with code 2.

### Playlist management:
```
python -m app -p add "https://music.youtube.com/playlist?list=PL..."
python -m app -p add "https://open.spotify.com/playlist/<id>"
python -m app -p del 1,"Chill Mix"          # remove locally (registry + DB)
python -m app -p del "https://open.spotify.com/playlist/<id>"   # by URL
python -m app -p delete all                 # remove every playlist
python -m app -p ref 2,3                    # re-import all tracks from the platform
python -m app -p refresh all                # refresh every playlist
```

- `-p add` registers the playlist from its URL (title fetched from the platform, tracks
  imported automatically) and updates an existing entry in place (keybind preserved).
  The `del` command is local-only: it removes the registry entry and the local song
  database, never the playlist on the platform. `ref` deletes and re-imports the local
  database, refreshing the thumbnail too.
- Spotify URLs: `https://open.spotify.com/playlist/<id>[...]` or `spotify:playlist:<id>`.
  YouTube/YouTube Music URLs: `.../playlist?list=<id>` on music.youtube.com or youtube.com.
  Tidal URLs: `https://tidal.com/browse/playlist/<id>` (a plain `https://tidal.com/playlist/<id>`
  resolves to the same thing).
  Song URLs (`.../watch?v=...`) are rejected with a hint - use `-a` for the current song.
- A playlist name that is pure digits (e.g. `"123"`) is always read as an order number,
  and names containing commas cannot be addressed by name - use `--list` numbers or the
  playlist URL for those.
- Exit codes: `0` if every requested operation succeeded, `1` on runtime failures (bad
  auth, playlist not found on the platform, import/refresh errors), `2` on bad input
  (malformed/out-of-range/unknown targets, unrecognized URLs or verbs).
- `-p` consumes the rest of the command line: put it after any other options.

### Song add:
```
python -m app -a 1,2,3            # playlists #1, #2 and #3
python -m app -a 1-3              # range
python -m app --add-song "Chill Mix"   # by name
python -m app -a 1,"Chill Mix",3  # mixed numbers and names
python -m app --list              # show the numbered playlist list
```

### Scrobble (Last.fm backend, independent of any playlist):

```
python -m app -s                   # scrobble the currently-playing song
python -m app --scrobble           # (long form)
```

`-s` scrobbles the current song without adding it to any playlist - `api`-type
platforms (Spotify) read the currently-playing track, `extension`-type
platforms (YouTube Music) capture via the browser extension. It requires the
Last.fm plugin and credentials to be configured (see the Settings dialog's
Last.fm section); otherwise it prints an error and exits non-zero.

### Other:
```
python -m app --data-saver   # launch the GUI with thumbnail fetching disabled
                             # (the "max" thumbnail mode: nothing is shown, nothing
                             # is downloaded; overrides the [thumbnails] setting for
                             # the run)

python -m app --fullscreen   # launch app in fullcreen
```

## Global install (for compositor shortcuts)
`python -m app ...` only works from the repo root. To bind the command from anywhere
(i3 / hyprland / etc), install a global `playlistmanager` command.

The simplest way - a symlink (the app resolves its own real location, so any checkout
path works; nothing machine-specific is hardcoded in the code):

```sh
chmod +x /path/to/PlaylistManager/main.py
ln -s /path/to/PlaylistManager/main.py ~/.local/bin/playlistmanager
```

or a small wrapper (no symlink indirection):

```sh
#!/bin/sh
exec python3 /path/to/PlaylistManager/main.py "$@"
```

(`~/.local/bin` is on PATH on most Linux setups; otherwise use `/usr/local/bin`.)
Then bind e.g.:

i3 / Sway

`bindsym $mod+Shift+a exec playlistmanager -a 1`

Hyprland

`bind = MODSHIFT, A, exec, playlistmanager -a 1`

KDE Plasma

`System Settings → Shortcuts → Custom Shortcut → Command/URL`

GNOME

`Settings → Keyboard → Custom Shortcuts → Command`
