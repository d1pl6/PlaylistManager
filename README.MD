Add songs that you're currently listening to your chosen playlists or liked tracks using keybinds.

![screenshot1](https://github.com/d1pl6/PlaylistManager/blob/dev/assets/screenshot1.png)

# About
I couldn't find something similar so i did it. First it used Electron, but i didn't like 5 billion (300mb) RAM usage. Since i'm not using streaming platforms much, i'm using ai to create same app but on pyton.

# Features
- Easily add songs to playlists across multiple platforms
- Pluggable integration system for adding more platforms

# Install
Requires Python 3.10+
```
git clone https://github.com/d1pl6/PlaylistManager.git && cd PlaylistManager
```
Or Code > Download ZIP

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

### Optional dependencies
#### Integrations
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
> `python3-ytmusicapi` and `python3-pystray` are only packaged for Debian *sid/testing* and recent Ubuntu releases (universe). On Debian stable or older Ubuntu, use `pip install ytmusicapi pystray` (or `pip install ".[all]"`) instead.

> [!NOTE]
> **Wayland**: the AppIndicator backend is the only one that works; on GNOME enable the AppIndicator/KStatusNotifierItem extension.

# Usage
> [!NOTE]
> **Wayland**: applications cannot grab global keyboard events (the compositor owns input - a security design decision), so pynput hotkeys only capture XWayland apps there. Bind compositor shortcuts to the CLI instead - see [Command line](#command-line).
```
python -m main

# Or
python main.py
```
Add `--debug` for verbose logging.

2. Click on `Login` button (top left corner)
3. See [INTEGRATIONS](INTEGRATIONS.MD) for detailed steps for your platform
4. Click on `Add Playlist` button (`+`) and wait for a while
5. Choose playlist
6. Wait database creation (might take longer if you have a lot of songs in playlist)
7. Create keybind (click on 2nd line after playlist name)

See [INTEGRATIONS](INTEGRATIONS.MD) if your platform requires optional dependencies

# Command line
Add the currently-playing song to playlists from a terminal.

```
python -m app add 1,2,3            # playlists #1, #2 and #3
python -m app add 1-3              # range
python -m app add "Chill Mix"      # by name
python -m app add 1,"Chill Mix",3  # mixed numbers and names
python -m app add --list           # show the numbered playlist list
python -m app -a 1                 # option style, same as `add 1`
python -m app --list               # option-style listing
```

- A playlist name that is pure digits (e.g. `"123"`) is always read as an order number,
  and names containing commas cannot be addressed by name - use `--list` numbers for those.
- Exit codes: `0` if at least one playlist was updated, `1` on fatal errors (bad auth,
  nothing playing, URL timeout), `2` on bad input (malformed/out-of-range/unknown targets).

## Global command (for compositor shortcuts)
`python -m app ...` only works from the repo root. To bind the command from anywhere
(i3 / sway / hyprland / KDE / GNOME shortcuts), install a global `playlistmanager` command.
The simplest way - a symlink (the app resolves its own real location, so any checkout
path works; nothing machine-specific is hardcoded in the code):

```sh
chmod +x /path/to/playlistmanager/main.py
ln -s /path/to/playlistmanager/main.py ~/.local/bin/playlistmanager
```

or a small wrapper (no symlink indirection):

```sh
#!/bin/sh
exec python3 /path/to/playlistmanager/main.py "$@"
```

(`~/.local/bin` is on PATH on most Linux setups; otherwise use `/usr/local/bin`.)
Then bind e.g.:

i3 / Sway

`bindsym $mod+Shift+a exec playlistmanager add 1`

Hyprland

`bind = MODSHIFT, A, exec, playlistmanager add 1`

KDE Plasma

`System Settings → Shortcuts → Custom Shortcut → Command/URL`

GNOME

`Settings → Keyboard → Custom Shortcuts → Command`

`playlistmanager add 1` / `playlistmanager -a 1` / `playlistmanager --add 1` can be used.

# Integrations
For now only available
- YouTube Music
- Spotify (not tested)

See [INTEGRATIONS.MD](INTEGRATIONS.MD)

# License
PlaylistManager is licensed under the GNU-GPL-2.0-or-later. If you did not get a copy, please see <https://www.gnu.org/licenses/>.

# Q & A
Q: Why AI?

A: I hate programming.

Q: Will _patform_ will be added?

A: See [TODO](TODO.MD), if it doesn't exist there - open `Platform integration request` on `Issue`s page.
