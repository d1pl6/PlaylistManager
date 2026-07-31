[x] Auto resize window setting

[x] Theme settings

[x] Theme change

[ ] Closing app takes too long
    One note for later: on this system the pynput listener thread stays a zombie after stop() even with time to unwind — it holds an X record context until process exit. That's inherent to the pynput/Xlib combo and harmless for a quitting app; if you ever switch global listeners without quitting in a long-running session, it's worth a comment in the pynput issue tracker as a known quirk.

[ ] Tray & Hide in tray

[ ] Show last N added songs in playlist and option in settings:

album img	| name		| add to playlist img

album img	| artists	| remove from playlist img

[ ] Make Flask receiver port configurable (not hardcoded 5000)
    little better, but still hardcoded, will be fixed after integration changes

[ ] Last.fm integration to add song to likes

[ ] Deezer integration

[ ] SoundCloud integration