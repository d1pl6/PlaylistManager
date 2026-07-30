[-] Auto resize window setting

[-] Theme settings

[ ] Make integrations optional (now - hard dependency)
    [ ] ytmusicapi: top-level import in music_youtube.py crashes app if package missing — move to lazy imports

[/] Theme change

[ ] Show last N added songs in playlist and option in settings:

album img	| name		| add to playlist img

album img	| artists	| remove from playlist img

[ ] Make Flask receiver port configurable (not hardcoded 5000)

[ ] Last.fm integration to add song to likes

[ ] Deezer integration

[ ] SoundCloud integration

[ ] Use (platform, playlist_id) as unique key in playlists.json instead of name alone
    Currently add_playlist doesn't check for duplicates (can add same playlist twice),
    and delete_playlist without platform wipes entries across all platforms.
    Switch to (platform + playlist_id) for add/delete/update operations.
    Requires: schema migration for existing playlists.json entries without playlist_id.