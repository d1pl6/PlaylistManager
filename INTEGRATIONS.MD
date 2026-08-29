## List
### Added
- [Spotify](#spotify)
- [YouTube Music](#youtube-music)
- [Last.fm](#lastfm)
### Planned
- [Apple Music](#apple-music)
- [SoundCloud](#soundcloud)
- [Amazon Music](#amazon-music)
- [Deezer](#deezer)
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
2. Install the [browser extension](https://github.com/d1pl6/youtube-music-integration) (Firefox only)
3. Add the extension [here](about:debugging#/runtime/this-firefox) (i won't sign extension)
4. Open https://music.youtube.com
5. Open Web Developers tools (F12 or Ctrl+Shift+I)
6. Open "Network" tab and search for "browse?". If it didn't appear, scroll down the page, reload, do whatever but it should appear.
7. Open console (cd) in `~/.config/playlistmanager/auth` or `AppData/{something}/playlistmanager/auth` folder and use `ytmusicapi browser` in console
8. Follow instructions (copy Request Headers > paste them in console > Enter > Ctrl+Z or Ctrl+D > Enter). Use Ctrl+C to cancel

### Lastfm
Lastfm not supporting playlists through API anymore

1. Install the [integration](https://github.com/d1pl6/lastfm-integration)
2. Create an [API account](https://www.last.fm/api/account/create) (Callback URL is not needed)
3. Copy-paste API key and Client Secret (you're not able to get them on lastfm for the second time so save them in password manager or something)

### [SoundCloud](https://soundcloud.com/you/apps/new)
Requires Artist Pro to create app & use API

Will be added. Without tests (I don't have money)

### [Deezer](https://developers.deezer.com/myapps)
Dead since 2023: [We're not accepting new application creation at this time. Please check again later.](https://en.deezercommunity.com/features-feedback-44/apis-to-enable-3rd-party-apps-ndk-not-available-javascript-sdk-not-working-76108?postid=222090#post222090)

[Check this in the future](https://foxxmd.github.io/multi-scrobbler/configuration/sources/deezer/)

---

### [Apple Music](https://developer.apple.com/documentation/applemusicapi/generating-developer-tokens)

Won’t be added in the near future.

### AmAzOn mUsIc [blocked](https://community.amazondeveloper.com/t/request-for-amazon-music-api-access/24864)
[Unfortunately, we are not onboarding any new client to the API while in closed beta.](https://community.amazondeveloper.com/t/request-for-amazon-music-api-access/24864)
[Check this in the future](https://github.com/AmineSoukara/amazon-music)

### [Qobuz](https://github.com/BartolomeoRusso9/qobuz-rest-api)

No official API

### Others
You can request your app/platform [here](https://github.com/d1pl6/PlaylistManager/issues/new?template=platform-integration-request.md)
