Add songs that you're currently listening to your chosen playlists or liked tracks using keybinds.

![screenshot1](https://github.com/d1pl6/PlaylistManager/blob/dev/assets/screenshot1.png)

![videoshowcase](https://github.com/d1pl6/PlaylistManager/blob/dev/assets/showcase_compressed_by_DZgas.mp4)

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

### Windows / Mac OS
```
# to install basic dependencies
pip install .

# to install all dependencies including optional
pip install ".[all]"

# to install basic & specify optional dependencies
pip install ".[youtubemusic]"
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

Q: Will _my patform_ will be added?

A: See [TODO](TODO.MD), if it doesn't exist there - open `Platform integration request` on `Issue`s page.
