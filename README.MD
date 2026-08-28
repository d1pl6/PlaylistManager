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
  - [CLI](CLI.MD#cli)
    - [Global install (linux)](CLI.MD#global-install-for-compositor-shortcuts)
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
pip install ytmusicapi

# Tray
apt install -y python3-pystray
```

> [!NOTE]
> **Wayland**: the AppIndicator backend is the only one that works; on GNOME enable the AppIndicator/KStatusNotifierItem extension.

> [!NOTE]
> **Tray behavior**: restoring the window from the tray ("Open PlaylistManager") is best-effort - compositors may refuse unsolicited raise/focus requests (focus-stealing prevention), so the window can appear unfocused or behind other windows. A maximized window may also restore unmaximized after hide-to-tray (the compositor clears the maximized state when the window is hidden). Both are compositor policy, may be fixed in the future.

# Usage
> [!NOTE]
> **Wayland**: applications cannot grab global keyboard events (the compositor owns input - a security design decision), so pynput keybinds only capture XWayland apps. Bind compositor shortcuts to the CLI instead - see [CLI](CLI.MD#cli) and [Global command](CLI.MD#global-install-for-compositor-shortcuts).
```
python -m main

# Or
python main.py
```

2. Click on `Login` button (top left corner)
3. Download integrations
4. See [INTEGRATIONS](INTEGRATIONS.MD) for detailed steps for your platform
5. Click on `Add Playlist` button (`+`) and wait for a while
6. Choose playlist
7. Wait database creation (might take longer if you have a lot of songs in playlist)
8. Create keybind (click on 2nd line after playlist name)

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

A: After todo and more tests