### List
[Spotify](#spotify---requers-premium)

[Apple Music](#apple-music)

[YouTube Music](#youtube-music---cant-import-playlists-using-oauth-so-only-browser-request-headers-option-is-avalible)

[SoundCloud](#soundcloud-requeires-artist-pro-to-create-app--use-api)

[Amazon Music](#amazon-muzik---unfortunately-we-are-not-onboarding-any-new-client-to-the-api-while-in-closed-beta)

[Deezer](#deezer---dead-since-2023)

[Qobuz](#qobuz)

[Others](#others)

---

### [Spotify](https://developer.spotify.com) - requers premium;
Added without tests.
1. Login [here](https://developer.spotify.com/)
2. Follow [these](https://developer.spotify.com/documentation/web-api/concepts/apps) instructions
3. Pray


### [Apple Music](https://developer.apple.com/documentation/applemusicapi/generating-developer-tokens)

Won't be added in nearest future.


### Youtube Music - can't import playlists using OAuth so only browser Request Headers option is avalible.
1. Install [extension](https://github.com/d1pl6/youtube-music-integration) (only Firefox)
1. Add extension [here](about:debugging#/runtime/this-firefox) (i won't sign it)
1. Open https://music.youtube.com
1. Open Web Developers tools (F12 or Ctrl+Shift+I)
1. Open "Network" tab and search for "browse?". If it didn't appear, scroll down the page, reload, do whatever. I HAVE NO IDEA HOW IT'S WORKING.
1. Open console inside config/appdata  folder and use `ytmusicapi browser` in console
1. Follow instructions (copy Request Headers and paste them in console, then hit Enter, Ctrl+Z or Ctrl+D, Enter). Use Ctrl+C to cancel


### [SoundCloud](https://soundcloud.com/you/apps/new) requeires Artist Pro to create app & use API

Will be added. Without tests (i don't have money)


### [AmAzOn mUzIk](https://community.amazondeveloper.com/t/request-for-amazon-music-api-access/24864) - [Unfortunately, we are not onboarding any new client to the API while in closed beta.](https://community.amazondeveloper.com/t/request-for-amazon-music-api-access/24864) 

[check this in the future](https://github.com/AmineSoukara/amazon-music)



### [Deezer](https://developers.deezer.com/myapps) - dead since 2023
[We're not accepting new application creation at this time. Please check again later.](https://en.deezercommunity.com/features-feedback-44/apis-to-enable-3rd-party-apps-ndk-not-available-javascript-sdk-not-working-76108?postid=222090#post222090)

[Check](https://foxxmd.github.io/multi-scrobbler/configuration/sources/deezer/)

### [Qobuz](https://github.com/BartolomeoRusso9/qobuz-rest-api)

I'll read that in the future

### Others
You can request integrations with your app/platform [here](https://github.com/d1pl6/PlaylistManager/discussions)
If I'll find popular app/platform that don't have plugins or native support for what PlaylistManager is doing (mpv, as example don't have it), if it's popular, i'll try to make integration through api/extension/web