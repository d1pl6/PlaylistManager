## Table of Contents
### [Added](#added-1)
- [Spotify](#spotify)
- [YouTube Music](#youtube-music)
- [Last.fm](#lastfm)
- [ListenBrainz](#listenbrainz)
- [SoundCloud](#soundcloud)
- [Deezer](#deezer)
- [Tidal](#tidal)
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

- NOTE: I wasn't able to to find API Key and Client Secret anywhere after creating account so save them in password manager or something

### ListenBrainz
No playlists (only scrobbles) - the same deal as Last.fm.

1. Log into [listenbrainz.org](https://listenbrainz.org/)
2. Open [Settings](https://listenbrainz.org/settings/) and copy the **User token**
3. Paste it in:
    - GUI `Login -> ListenBrainz`
    - CLI `PLAYLISTMANAGER_LISTENBRAINZ_TOKEN=<token> python -m app --login listenbrainz`

Notes:
- A logged-in ListenBrainz starts receiving listens immediately, same as Last.fm. Both are switched on
  and off on their own in `settings.ini`, in their own section - `[listenbrainz] scrobbles = false`
  stops ListenBrainz while Last.fm keeps working, and Settings -> Integrations -> Scrobbling does the
  same with a checkbox per service plus Select all / Deselect all. With both on, every listen goes to
  both services.
- Removing a song from a playlist deletes the matching listen from every service that received it, but
  only when ListenBrainz can match the listen back to a recording (it assigns the id the delete needs
  on submit). If it can't, the listen stays and so does the app's record of it - it never guesses and
  deletes a different listen.

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

### Tidal
Nothing is baked in - you register the app yourself, and the login is a normal
OAuth page in your browser.

1. Register an app at [developer.tidal.com/apps](https://developer.tidal.com/apps)
2. Add this **redirect URI**, exactly as written: `http://127.0.0.1:47831/callback`
3. Add the extension [here](about:debugging#/runtime/this-firefox) (i won't sign extension)
4. Paste the client id + secret in:
    - GUI `Login -> Tidal`
    - CLI `PLAYLISTMANAGER_TIDAL_CLIENT_ID=<id> PLAYLISTMANAGER_TIDAL_CLIENT_SECRET=<secret> python -m app --login tidal`
      (no secret flags for Tidal, same as the Deezer ARL - argv is world-readable
      through `/proc` and ends up in your shell history; unset the variables and the
      CLI asks instead, with the secret hidden)
5. Approve the authorize page that opens, then press a keybind

Notes:
- **The extension is required, not optional.** Tidal has no now-playing or
  recently-played endpoint in either API version, and there is no Linux desktop
  app to read - the web player is the only capture source. When a capture comes
  up short (the numeric id is only readable when the page was open as the song
  started), the flow resolves the song by title + artist through catalogue
  search instead of failing, with the album breaking ties.
- Tidal's **v1** API is what the plugin talks; the v2 endpoints sit behind
  developer access tiers an app you register yourself is not granted.
- A playlist write carries that playlist's ETag, so an add cannot land on a
  playlist that changed underneath it. If Tidal reports a conflict the add is
  reported as a failure, and the next keybind press re-reads the playlist and
  goes through.
- A track that is not available in your subscription's country comes back as a
  2xx with nothing added; the app reports that as a failure instead of
  pretending the song landed.
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
