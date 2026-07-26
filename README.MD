Add songs that you're currently listening to your chosen playlists or liked tracks using keybinds.

![screenshot1](https://github.com/d1pl6/PlaylistManager/blob/dev/assets/screenshot1.png)

# About
I couldn't find something similar so I did it. First PlaylistManager used TypeScript and Electron, but I didn't like 5 billion RAM usage so I asked AI to rewrite it on Python and ended up doing everything from 0.

I'll rewrite it so no auto-generated code will be in the code. Q: When? A: Uhhhhhhhhhhhh.....

# Features
- Easily add songs to playlists across multiple platforms
- Pluggable integration system for adding more platforms

# Install
Requires Python 3.10+

```
git clone https://github.com/d1pl6/PlaylistManager.git
```
or download zip

### Windows / Mac OS
```
pip install pillow requests python-dotenv platformdirs flask flask-cors cryptography pynput
```

### Linux
It's not recommended to install dependencies through `pip` because it can break system packages.

#### Arch
```
pacman -S tk python-pillow python-requests python-dotenv python-platformdirs python-flask python-flask-cors python-cryptography python-pynput
```

#### Integrations (optional)
```
pip install ytmusicapi
```

# Usage
```
cd PlaylistManager
python app/main.py
```

Add `--debug` for verbose logging.

# Integrations
For now only available
- YouTube Music
- Spotify

See [INTEGRATIONS.MD](INTEGRATIONS.MD)

# License
PlaylistManager is licensed under the GNU-GPL-2.0-or-later. If you did not get a copy, please see <https://www.gnu.org/licenses/>.