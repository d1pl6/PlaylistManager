## List
### Added
- [Spotify](#spotify)
- [YouTube Music](#youtube-music)
- [Last.fm](#lastfm)
- [SoundCloud](#soundcloud)
- [Deezer](#deezer)
### Planned
- [Apple Music](#apple-music)
- [Amazon Music](#amazon-music-blocked)
- [Qobuz](#qobuz)
- [Others](#others)

---

### [Spotify](https://developer.spotify.com)
Requires premium

Added without tests.
1. Install the [integration](https://github.com/d1pl6/spotify-integration)
2. Login [here](https://developer.spotify.com/)
3. Follow [these](https://developer.spotify.com/documentation/web-api/concepts/apps) instructions
4. Pray, idk

### YouTube Music
Can't import playlists using OAuth so only browser’s Request Headers option is available.

1. Install the [integration](https://github.com/d1pl6/youtube-music-integration)
2. Add the extension [here](about:debugging#/runtime/this-firefox) (i won't sign extension)
3. Open https://music.youtube.com
4. Open Web Developers tools (F12 or Ctrl+Shift+I)
5. Open "Network" tab and search for "browse?". If it didn't appear, scroll down the page, reload, do whatever but it should appear.
6. Open console (cd) in `~/.config/playlistmanager/auth` or `AppData/{something}/playlistmanager/auth` folder and use `ytmusicapi browser` in console
7. Follow instructions (copy Request Headers > paste them in console > Enter > Ctrl+Z or Ctrl+D > Enter). Use Ctrl+C to cancel

### Lastfm
Lastfm not supporting playlists through API anymore

1. Install the [integration](https://github.com/d1pl6/lastfm-integration)
2. Create an [API account](https://www.last.fm/api/account/create) (Callback URL is not needed)
3. Copy-paste API key and Client Secret (you're not able to get them on lastfm for the second time so save them in password manager or something)

### [SoundCloud](https://soundcloud.com/you/apps/new)
Requires Artist Pro to create app & use API

OAuth with a refresh token, like Spotify. Credentials (client id, client secret, refresh token) go in `auth/soundcloud.json`.

1. Install the [integration](https://github.com/d1pl6/soundcloud-integration)
2. Create an app at [soundcloud.com/you/apps/new](https://soundcloud.com/you/apps/new)
3. Get the refresh token via the SoundCloud OAuth flow (`secure.soundcloud.com/authorize`)

The extension is optional but recommended for exact captures:
1. Add the extension [here](about:debugging#/runtime/this-firefox) (i won't sign extension)
2. Enable "Hybird" mode in settings (default)

Note: SoundCloud has no "append to playlist" endpoint — each add rewrites the whole playlist's track array (read-modify-write), so adds are a bit slower.

Note 2: "Now playing" comes from `/me/recently-played/tracks`[0] so it need confirmation that it can be used as "Now playing" so i could remove extension.

# Deezer
Working poorly. You can't open 2+ deezer tabs, because extension won't be able to get currently-playing song. New song updates every 5 seconds, so you CAN skip songs fast but it's NOT RECOMMENDED to do so

1. Install the [integration](https://github.com/d1pl6/deezer-integration)
2. Add the extension [here](about:debugging#/runtime/this-firefox) (i won't sign extension)
3. Open https://www.deezer.com
4. Open Web Developers tools (F12 or Ctrl+Shift+I)
5. Open Storage -> Cookies -> https://www.deezer.com
6. Filter 'arl' and copy clean value (not `arl="xyz"`, only `xyz`)

---

### [Apple Music](https://developer.apple.com/documentation/applemusicapi/generating-developer-tokens)

Won’t be added in the near future.

### AmAzOn mUsIc [blocked](https://community.amazondeveloper.com/t/request-for-amazon-music-api-access/24864)
[Unfortunately, we are not onboarding any new client to the API while in closed beta.](https://community.amazondeveloper.com/t/request-for-amazon-music-api-access/24864)
[Check this in the future](https://github.com/AmineSoukara/amazon-music)

### [Qobuz](https://github.com/BartolomeoRusso9/qobuz-rest-api)

No official API

### Others
You can request app/platform that you're using [here](https://github.com/d1pl6/PlaylistManager/issues/new?template=platform-integration-request.md)
