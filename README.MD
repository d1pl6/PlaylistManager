Add songs that you're currently listening to your chosen playlists or liked tracks using keybinds.

<img width="326" height="249" alt="screenshot1" src="https://github.com/d1pl6/PlaylistManager/blob/master/assets/screenshot1.png" />

## Table of Contents
- [Why?](#why)
- [Integrations](#integrations)
- [Install](#install)
  - [Windows / Mac OS](#windows--mac-os)
  - [Linux](#linux)
    - [Arch](#arch)
    - [Debian / Ubuntu](#debian--ubuntu)
- [Usage](#usage)
  - [CLI](CLI.MD)
- [Uninstall](#uninstall)
- [Help needed (real account required)](#help-needed)
- [License](#license)
- [Q&A](#q--a)

# Why?
I couldn't find something similar so i did it. First it used Electron, but i didn't like 5 billion (300mb) RAM usage.

P.S: Since I raerly using streaming platforms now, and I'm not interested in programming much, I started using AI to add new functions. So, some functions might work in a way that you don't expect them to work since I'm not able to test them much. If you found something wierd - please, open bug report or disscussion.

# Integrations
Currently supported:
- YouTube Music
- Spotify ([not tested](#help-needed))
- SoundCloud ([not tested](#help-needed))
- Deezer ([should be working fine](#help-needed))
- Tidal ([not tested](#help-needed))

Plus:
- Lastfm (scrobbles & likes)
- ListenBrainz (scrobbles)

See [INTEGRATIONS.MD](INTEGRATIONS.MD)

# Install
Requires Python 3.10+
```bash
git clone https://github.com/d1pl6/PlaylistManager.git --depth 1
```
Or Download clean ZIP from [Releases](https://github.com/d1pl6/PlaylistManager/releases/latest)

To Update:
- GUI: `Settings → Check for updates`
- CLI: `git pull --ff-only`

## Windows / Mac OS
```bash
# to install basic dependencies
pip install .

# to install all dependencies including optional
pip install ".[all]"

# to install basic & specify optional dependencies
pip install ".[tray, keyring]"
# or
pip install pystray keyring
```

## Linux
> [!NOTE]
> It's not recommended to install dependencies through `pip` because it can break system packages.

### Arch
```bash
pacman -S tk python-pillow python-requests python-platformdirs python-flask python-flask-cors python-pynput

# Optional
pacman -S python-pystray python-keyring
```

### Debian / Ubuntu
```bash
apt install -y python3-tk python3-pil python3-requests python3-platformdirs python3-flask python3-flask-cors python3-pynput

# Optional
apt install -y python3-pystray python3-keyring
```

> [!NOTE]
> For **keyring** you need secret-service daemon (gnome-keyring, kwallet).

> [!NOTE]
> **Wayland**: the AppIndicator backend is the only one that works; on GNOME enable the AppIndicator/KStatusNotifierItem extension.

# Usage
> [!NOTE]
> **Wayland**: Bind compositor shortcuts to the [CLI](CLI.MD#song-add) and [*install* playlistmanager](CLI.MD#global-install-for-compositor-shortcuts). If wayland devs add Global Shortcut Protocol - please open new [Issue](https://github.com/d1pl6/PlaylistManager/issues), I'll implement it
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

## CLI
See [CLI.MD](CLI.MD)

# Uninstall
- Delete `~/.config/playlistmanager`
- Uninstall python dependencies

# Help needed

Everyone here needs a real account on the platform. Report what worked,
what failed, and paste the exact error from the log/status row.

**Liked-songs virtual playlists** (Spotify "Liked Songs", SoundCloud
"Liked Tracks", Deezer "Loved Tracks", Tidal "Favorites"): none of these is a real playlist
- the app shows them in the add dialog as a regular entry and routes the
keybind add to the platform's *like* API. Things to test on each platform:

* Add dialog shows the virtual entry and selectable tiles; pick it, the
  card appears and registers a keybind (GUI picker only - these have no
  URL, so `-p add` cannot register them).
* Keybind/`-a` add of the currently playing song: does the track actually
  get liked on the platform (check in the platform app)? Does a second
  add of the same song get blocked as a duplicate?
* Card reload (right-click → reload): expected to FAIL - the reload path
  fetches playlist details/tracks by the sentinel id and the platform
  rejects it. Confirm the failure is a clean error, not a crash.
* Song remove on the card (right-click → remove a row): expected to FAIL
  with a platform error (fake playlist id) and the row stays; the local
  entry is only deleted when the platform call succeeds. Report the exact
  error line.

### Specific per platform:

#### Spotify (real account)
- RE-AUTH FIRST: `python main.py --login
  spotify ...` (the new `user-library-modify` scope only applies after a
  fresh login; an old token makes the like call 403, not a code bug).
  - "Liked Songs" shows in the add dialog; adding likes the song in the
    Spotify app (`PUT /v1/me/tracks`).
  - Followed playlists (ones you don't own) must NOT appear in the add
    dialog - currently guaranteed by the owned/collaborative filter;
    confirm the picker has no non-owned entries.
  - Initial card import shows "No tracks" (known gap - the virtual entry
    has no `/v1/playlists/<id>/tracks`).
#### SoundCloud (real account)
- playlists you follow but don't own must
  be absent from the add dialog (new `user.id` ownership filter - confirm
  a followed playlist is gone). "Liked Tracks" adds must like via
  `PUT /me/track-likes/<numeric id>` - confirm in the SoundCloud app.
#### Deezer
- "Loved Tracks" likes go through the public REST
  `PUT /user/me/tracks` with the ARL cookie - this endpoint is UNVERIFIED
  against the live API. Key test: does adding a song to "Loved Tracks"
  return 2xx and show up in the Deezer favorites? If it 4xxs - report the status code
  and response body. Also confirm no non-owned playlists appear
  (`me.playlists` is own-only, but verify nothing unexpected shows).

#### Tidal (real account + a browser extension)
- the least-tested platform here, and the one that needs the most setup. Register an app at
  https://developer.tidal.com/apps with the redirect URI
  `http://127.0.0.1:47831/callback` (exact string), then
  `python main.py --login tidal` (GUI `Login -> Tidal`, or the env vars from
  CLI.MD). Nothing is baked in - your own client id + secret.
  - **The login itself** is the first test: does the authorize page open, does
    it come back to the loopback address (a blank page saying the login
    succeeded is correct), and does the CLI/GUI print the username? A
    `redirect_uri mismatch` means the string registered in the dashboard is
    not byte-for-byte the one above.
  - **The extension is required** - Tidal has no now-playing endpoint and no
    Linux desktop app, so the web player is the only capture source. Load it
    from `integrations/tidal/tidal-extension/` (`about:debugging#/runtime/this-firefox`,
    "Load Temporary Add-on" -> pick `manifest.json`) and keep a
    listen.tidal.com tab open. Press a keybind with a song playing and
    confirm the song is added. If it reports that it could not identify the
    track, that is the search fallback failing to find your song - report the
    status line verbatim, the title/artist it saw would be the useful part.
  - Press the keybind **twice on the same song** and confirm the second one
    is reported as already present rather than adding a duplicate.
  - **"Favorites"** routes to Tidal's favourites API instead of a playlist
    add. Confirm the song shows up as a favourite in the Tidal web player.
  - **Removal** is the interesting write: Tidal deletes playlist items by
    position, so the app has to look the id up first. Right-click a row ->
    remove and confirm exactly that row disappeared from the playlist in the
    web player (a wrong row going missing would mean an off-by-one - report
    it with the playlist's contents).
  - A playlist you **follow but do not own** must not appear in the add
    dialog. Tidal also refuses writes to such a playlist; if one shows up
    anyway, report what the status line says.
  - Worth reporting even when everything works: whether a *paused* player
    still adds (the extension sends the loaded track either way), and
    whether a track that is unavailable in your country reports a failure
    instead of a silent success.

# License
PlaylistManager is licensed under the GNU-GPL-2.0-or-later. If you did not get a copy, please see <https://www.gnu.org/licenses/>.

# Q & A
Q: Current state?

A: Pseudo-release.

Q: Will _platform_ be added?

A: Open `Platform integration request` on [Issue](https://github.com/d1pl6/PlaylistManager/issues) page and I'll see what I can do.