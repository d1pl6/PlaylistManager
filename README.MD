Add songs that you're currently listening to your chosen playlists or liked tracks using keybinds.

<img width="326" height="249" alt="screenshot1" src="https://github.com/user-attachments/assets/e628af22-43fc-4a05-ab8e-a9b0083ad697" />
<img width="326" height="249" alt="screenshot2" src="https://github.com/user-attachments/assets/06ec2ba7-d826-4484-8fba-136ae36493d8" />
<img width="326" height="249" alt="screenshot3" src="https://github.com/user-attachments/assets/18873a88-2a09-4d1c-b881-5f50e6933d65" />

# Content
- [Why?](#why)
- [Integrations](#integrations)
- [Install](#install)
  - [Windows / Mac OS](#windows--mac-os)
  - [Linux](#linux)
    - [Arch](#arch)
    - [Debian / Ubuntu](#debian--ubuntu)
  - [Optional dependencies](#optional-dependencies)
- [Usage](#usage)
  - [CLI](#cli)
    -[Global install (linux)](#global-install-for-compositor-shortcuts)
- [License](#license)
- [Q&A](#q--a)

# Why?
I couldn't find something similar so i did it. First it used Electron, but i didn't like 5 billion (300mb) RAM usage. Since i'm not using streaming platforms much, i'm using ai to create same app but on pyton.

# Integrations
Currently supported:
- YouTube Music
- Spotify

See [INTEGRATIONS.MD](INTEGRATIONS.MD)

# Install
Requires Python 3.10+
```
git clone https://github.com/d1pl6/PlaylistManager.git && cd PlaylistManager
```
Or Code > Download ZIP

Update `git pull`

## Windows / Mac OS
```
# to install basic dependencies
pip install .

# to install all dependencies including optional
pip install ".[all]"

# to install basic & specify optional dependencies
pip install ".[youtubemusic,tray]"
```

## Linux
> [!TIP]
> It's not recommended to install dependencies through `pip` because it can break system packages.

### Arch
```
pacman -S tk python-pillow python-requests python-platformdirs python-flask python-flask-cors python-pynput
```

### Debian / Ubuntu
```
apt install -y python3-tk python3-pil python3-requests python3-platformdirs python3-flask python3-flask-cors python3-pynput
```

## Optional dependencies
**Windows / macOS**:
```
# Integrations
pip install ytmusicapi

# Tray
pip install pystray
```
**Linux**:

Arch
```
# Integrations
pacman -S python-ytmusicapi

# Tray
pacman -S python-pystray
```
Debian / Ubuntu
```
# Integrations
apt install -y python3-ytmusicapi

# Tray
apt install -y python3-pystray
```

> [!NOTE]
> `python3-ytmusicapi` is only packaged for Debian *testing/unstable* (not in stable); `python3-pystray` has been packaged since bookworm. On Debian stable, `apt install python3-pystray` and `pip install ytmusicapi` (or `pip install ".[all]"`) instead.

> [!NOTE]
> **Wayland**: the AppIndicator backend is the only one that works; on GNOME enable the AppIndicator/KStatusNotifierItem extension.

> [!NOTE]
> **Tray behavior**: restoring the window from the tray ("Open PlaylistManager") is best-effort - compositors may refuse unsolicited raise/focus requests (focus-stealing prevention), so the window can appear unfocused or behind other windows. A maximized window may also restore unmaximized after hide-to-tray (the compositor clears the maximized state when the window is hidden). Both are compositor policy, may be fixed in the future.

# Usage
> [!NOTE]
> **Wayland**: applications cannot grab global keyboard events (the compositor owns input - a security design decision), so pynput keybinds only capture XWayland apps. Bind compositor shortcuts to the CLI instead - see [Command line](#command-line) and [Global command](#global-command-for-compositor-shortcuts).
```
python -m main

# Or
python main.py
```

- `--verbose / -v` for verbose logging.
- `--debug / -vv`  DEBUG and above (everything)
- `--trace / -vvv` TRACE and above + third-party debug loggers (requests / urllib3 / ytmusicapi / etc)

2. Click on `Login` button (top left corner)
3. See [INTEGRATIONS](INTEGRATIONS.MD) for detailed steps for your platform
4. Click on `Add Playlist` button (`+`) and wait for a while
5. Choose playlist
6. Wait database creation (might take longer if you have a lot of songs in playlist)
7. Create keybind (click on 2nd line after playlist name)

## CLI
See [Global command](#global-command-for-compositor-shortcuts)

Authentication (`--login` / `--logout`) - the headless equivalent of the GUI login tiles:

```
python -m app --login youtube_music       # open auth folder + terminal ('ytmusicapi browser')
python -m app --login spotify --client-id <ID> --client-secret <SECRET> --refresh-token <TOKEN>
python -m app --logout youtube_music      # delete browser.json + registry entries + db/youtube_music/
python -m app --logout spotify            # delete spotify.json + registry entries + db/spotify/
```

- `--login youtube_music` opens the auth folder and a terminal for the `ytmusicapi browser`
  flow; manual instructions are printed when no terminal emulator is installed. `--login
  spotify` takes the three credential options (or prompts for them interactively when
  omitted; an empty refresh-token prompt reuses the stored token). Credentials are
  verified against the Spotify API first and only persisted on success - a failed login
  leaves previously working auth untouched.
- `--logout` is **local-only** and idempotent: it deletes the platform's credential file(s),
  unregisters all of its playlists from the registry and removes the local song databases
  under `db/<platform>/`. Nothing is revoked on the platform itself; re-running it with
  nothing left prints "no credentials found" and exits 0. After re-login, re-add playlists
  with `-p add <URL>`.
- Credentials passed on the command line may appear in your shell history, so don't share
  your screen during login.
- Unknown platform names exit with code 2.

Playlist management (`-p` / `--playlist`, verbs: `add`, `del`/`delete`, `ref`/`refresh`):

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
  Song URLs (`.../watch?v=...`) are rejected with a hint - use `-a` for the current song.
- A playlist name that is pure digits (e.g. `"123"`) is always read as an order number,
  and names containing commas cannot be addressed by name - use `--list` numbers or the
  playlist URL for those.
- Exit codes: `0` if every requested operation succeeded, `1` on runtime failures (bad
  auth, playlist not found on the platform, import/refresh errors), `2` on bad input
  (malformed/out-of-range/unknown targets, unrecognized URLs or verbs).
- `-p` consumes the rest of the command line: put it after any other options.

Song add:

```
python -m app -a 1,2,3            # playlists #1, #2 and #3
python -m app -a 1-3              # range
python -m app --add-song "Chill Mix"   # by name
python -m app -a 1,"Chill Mix",3  # mixed numbers and names
python -m app --list              # show the numbered playlist list
```

Scrobble (Last.fm backend, independent of any playlist):

```
python -m app -s                   # scrobble the currently-playing song
python -m app --scrobble           # (long form)
```

`-s` scrobbles the current song without adding it to any playlist — `api`-type
platforms (Spotify) read the currently-playing track, `extension`-type
platforms (YouTube Music) capture via the browser extension. It requires the
Last.fm plugin and credentials to be configured (see the Settings dialog's
Last.fm section); otherwise it prints an error and exits non-zero.

### Global install (for compositor shortcuts)
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


# License
PlaylistManager is licensed under the GNU-GPL-2.0-or-later. If you did not get a copy, please see <https://www.gnu.org/licenses/>.

# Q & A
Q: Current state?

A: Beta.

Q: Why AI?

A: I hate programming.

Q: Will _patform_ be added?

A: See [TODO](TODO.MD), if it doesn't exist there - open `Platform integration request` on [Issue](https://github.com/d1pl6/PlaylistManager/issues) page.

Q: 1.0.0 when?

A: After todo and more tests for each usecase