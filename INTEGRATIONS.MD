## Table of Contents
### [Added](#added-1)
- [Spotify](#spotify)
- [YouTube Music](#youtube-music)
- [Last.fm](#lastfm)
- [SoundCloud](#soundcloud)
- [Deezer](#deezer)
### [Planned](#planned-1)
- [Apple Music](#apple-music)
- [Amazon Music](#amazon-music-blocked)
- [Qobuz](#qobuz)
### [Others](#others)

---

# Installing integrations
- Directly from repos
- Through GUI `Login -> Manage`
- Through CLI `python -m app --install <spotify>` ([reference](CLI.md#plugins))

---

## Added
### [Spotify](https://developer.spotify.com)
**Requires premium**

Added without tests.
1. Login [here](https://developer.spotify.com/)
2. Follow [these](https://developer.spotify.com/documentation/web-api/concepts/apps) instructions
3. Pray, idk

### YouTube Music
Can't import playlists using OAuth so only browser’s Request Headers option is available.

Extension getting songs every 5 seconds, so you CAN skip songs fast but it's NOT RECOMMENDED to do so.

1. Install the dependency `pip install ytmusicapi`
2. Add the extension [here](about:debugging#/runtime/this-firefox) (i won't sign extension)
3. On [music.youtube.com](https://music.youtube.com) open Web Developers tools (F12 or Ctrl+Shift+I)
4. Open "Network" tab and search for "browse?". If it didn't appear, scroll down the page, reload tab, do whatever but it should appear.
5. Choose one of these methods:
    - GUI `Login -> YouTube Music`
    - CLI `cd ~/.config/playlistmanager/auth` or `AppData/{something}/playlistmanager/auth` and use `ytmusicapi browser` in console
6. Follow instructions given in console (`copy Request Headers -> paste them in console -> Enter -> Ctrl+Z/Ctrl+D -> Enter`). Use Ctrl+C to cancel

It doesn't matter when you load an extension.

### Lastfm
Lastfm not supporting playlists through API anymore (likes&scrobble songs are available)

1. Create an [API account](https://www.last.fm/api/account/create) (Callback URL is not needed)
2. Copy-paste API Key and Client Secret

- I wasn't able to to find API Key and Client Secret anywhere after creating account so save them in password manager or something

### [SoundCloud](https://soundcloud.com/you/apps/new)
**Requires Artist Pro**

1. Create an app at [soundcloud.com/you/apps/new](https://soundcloud.com/you/apps/new)
2. Get the refresh token via the SoundCloud OAuth flow (`secure.soundcloud.com/authorize`)

The extension is optional but recommended for exact captures:
1. Add the extension [here](about:debugging#/runtime/this-firefox) (i won't sign extension)
2. Enable "Hybrid" mode in settings (default)

Note: SoundCloud has no "append to playlist" endpoint - each add rewrites the whole playlist's track array (read-modify-write), so adds are a bit slower.

Note 2: "Now playing" comes from `/me/recently-played/tracks`[0] so it need confirmation that it can be used as "Now playing" (that's why extension is recommended).

### Deezer
Works poorly. You can't open 2+ deezer tabs, because extension won't be able to get currently-playing song.

Extension updates new song every 5 seconds, so you CAN skip songs fast but it's NOT RECOMMENDED to do so.

1. Add the extension [here](about:debugging#/runtime/this-firefox) (i won't sign extension)
2. On [deezer.com](https://www.deezer.com) open Web Developers tools (F12 or Ctrl+Shift+I)
3. Open `Storage -> Cookies -> https://www.deezer.com`
4. Filter 'arl' and copy clean value (not `arl="xyz"`, only `xyz`)

**Load an extension first**, then update your token.
---
## Planned
### [Apple Music](https://developer.apple.com/documentation/applemusicapi/generating-developer-tokens)

Won’t be added in the near future.

### AmAzOn mUsIc [blocked](https://community.amazondeveloper.com/t/request-for-amazon-music-api-access/24864)
[Unfortunately, we are not onboarding any new client to the API while in closed beta.](https://community.amazondeveloper.com/t/request-for-amazon-music-api-access/24864)
[Check this in the future](https://github.com/AmineSoukara/amazon-music)

### [Qobuz](https://github.com/BartolomeoRusso9/qobuz-rest-api)

No official API

# Others
You can request app/platform that you're using [here](https://github.com/d1pl6/PlaylistManager/issues/new?template=platform-integration-request.md)
