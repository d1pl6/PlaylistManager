# Plan — Playlist showcase: show last N added songs + hideable log row

Status: **plan only** — no code changed yet. Written 2026-08-14. All layout
references below are against the current `app/ui/main_window.py` card
(`create_main_frame`, `CARD_W_BASE = 320`, `CARD_H_BASE = 96`).

## 1. Problem statement

Today each playlist card shows a header (cover, name, close, keybind, reload)
and one log row (artist / song / status). The user wants each card to also
show the **last N added songs** — a small "showcase" list with thumbnail, song
name, artists, and a remove button — plus a setting to hide the log row. The
feature must default to **off** (N = 0) so existing users see a pixel-identical
card until they enable it.

Target card layout (N = 1 shown):

```
playlist img | playlist name        | close btn (img)
playlist img | playlist keybind     | reload btn (img)
log_artist   | helper (-) log_name  | helper (|) log_log      <- hideable
song thumb   | song name            | remove btn (img)        <- showcase row 1
song thumb   | song artists                                    <- showcase row 2
```

(With N > 1, the song thumb/name/artists/remove block repeats for each song,
newest first.)

## 2. Settings

Two new keys in `cfg/settings.ini`, added to `DEFAULT_SETTINGS` in
`app/utils/config.py` so `ensure_settings_file()` merges them into existing
user files (unknown/legacy sections stay untouched — the repo contract):

```python
"showcase":      {"count": "0"},      # int, 0 = off, N = show last N songs
"showcase_log":  {"is_true": "yes"},  # show the log_artist/log_name/log_log row
```

- `count` reuses the existing value API: `get_setting_value("showcase",
  "count", "0")` / `set_setting_value("showcase", "count", str(n))`.
- the log flag reuses the existing boolean API: `get_setting("showcase_log",
  True)` / `set_setting("showcase_log", bool)` — same `is_true` pattern as the
  five existing boolean sections, so `_toggle_setting()` works unchanged.
- Values are read once into `MainWindow` at `__init__` (the
  `_read_auto_resize_setting` pattern): `_showcase_count` (int, clamped to
  `[0, 20]`, non-numeric → 0) and `_show_log` (bool).

## 3. Design decisions

### D1 — Ordering: newest song on top (user-confirmed)
`SongManager` gains `get_latest_songs(playlist_name, limit, platform)`:

```python
"SELECT id, title, artists, thumbnail_url, duration, track_id "
"FROM songs ORDER BY id DESC LIMIT ?"
```

Row mapping (with a 6-song history, N = 4):

| Row | Song |
|---|---|
| 1 (top) | **latest added** (song 6/6) |
| 2 | the one added before it (5/6) |
| 3 | the one added before that (4/6) |
| 4 (bottom) | the oldest shown (3/6) |

This is exactly `ORDER BY id DESC` — monotonic with `added_at` and immune to
same-second timestamp ties (identical ordering to the existing
`get_latest_song`, which uses `ORDER BY id DESC LIMIT 1`). A successful hotkey
add therefore surfaces the new song as the top showcase row and pushes the
older ones down.

**Explicitly NOT wanted** (rejected): `ORDER BY id ASC` or the first-added
song on top (row 1 = song 1/6 or 4/6, row 2 = 5/6, row 3 = 6/6 latest) — that
would bury the newest song at the bottom of the list. The plan and its
verification checklist assert the DESC order; the implementation must never
"helpfully" reverse it.

### D2 — Card stays a fixed-size box; height becomes a function of N and log visibility
The card uses `grid_propagate(False)` (screen.md §5). Tk still gives each grid
row its *requested* height first and only distributes surplus by weight, so the
explicit card height must be the sum of the rows. **The showcase row's height
is MEASURED at runtime** (`showcase_frame.winfo_reqheight()`, after an
`update_idletasks()`) instead of a per-row constant: font metrics, UI scale
and any frame padding/border vary (a hardcoded 48 px under-reserves the real
~54 px block and starves the weighted header row — finding 13):

```
CARD_H_BASE     = 96   (header ≈ 66 px cover + log row ≈ 23 px, existing)
LOG_ROW_H_BASE  = 23   (font-12 linespace, measured in screen.md §2.1)

card_height = px(CARD_H_BASE)
            + showcase_frame.winfo_reqheight()   -- measured, not estimated
            - (px(LOG_ROW_H_BASE) if log hidden else 0)
```

- `shown_rows = min(_showcase_count, len(songs))` — a card never reserves
  empty space for songs that aren't imported yet; it grows when a song is
  added and shrinks when one is removed. (Alternative — always reserve N rows —
  rejected: blank space on first load.)
- `main_frame.config(height=...)` then re-fit the window; `resize_window()`
  already anchors the current center (memory #76) so growth stays balanced.
  See finding 11 for the grow-only fit when `auto_resize` is off.
- The showcase lives in a third row of `main_frame`
  (`grid_rowconfigure(2, weight=0)`; keep rows 0/1 at weight 1). Hiding the
  log uses `grid_remove()` (preserves the grid options for re-show) and
  subtracts its height.

### D3 — Remove button: platform-first, mirroring the add invariant
A local-only `SongManager.delete_song` would be **futile**: the track is still
in the platform playlist, so the next reload re-imports it via
`INSERT OR IGNORE` and the "removed" song resurrects. The repo's add invariant
(AGENTS.md "Add-flow invariant": platform API first, abort on failure, never a
local-only "success") must be mirrored for removal:

1. Disable the frame's remove buttons, set status `"Removing"`.
2. Worker thread (never block the main thread — memory #62):
   `integration.remove_track(playlist_id, track_id)`.
3. On platform failure → status `"Error"`, buttons re-enabled, local row kept.
4. On success → `SongManager().delete_song(name, song_id, platform)`, then
   marshal back via `root.after(0, ...)` (guarded with try/except — memory #75)
   → status `"Removed"`, `_refresh_showcase()`.

New integration surface (service layer, not UI):

- `BaseIntegration.remove_track(self, playlist_id, track_id) -> bool` — default
  returns `False` (interface addition, both platforms implement).
- `YouTubeMusicIntegration.remove_track` → `yt_client.remove_playlist_items(
  playlist_id, [{"videoId": track_id}])` (ytmusicapi `remove_playlist_items`;
  verify against a real account — some playlists may need `setVideoId` too;
  our DB only stores the `videoId`).
- `SpotifyIntegration.remove_track` → new `SpotifyAPI.remove_track_from_playlist(
  playlist_id, track_id)` → `DELETE /v1/playlists/{id}/tracks` with body
  `{"tracks": [{"uri": f"spotify:track:{track_id}"}]}` (mirror the 401
  refresh+retry and `>= 400 → None` handling of `_request`/`add_tracks_to_playlist`).

Edge cases: no `playlist_id` in the store (legacy entry) or integration
missing/unauthenticated → status `"Error"` with a log line, no DB write.

### D4 — Refresh triggers
The showcase is DB-driven and cheap to re-read; refresh it whenever song data
changes:

1. `setup()` — once per playlist after `_update_log_labels_from_db`.
2. `_on_import_done()` — after import/reload finished (also covers the
   `_on_reload_done` path, which routes through it).
3. Hotkey add — new optional `KeybindCallbacks.on_song_added` (default no-op),
   called from `KeybindController.handle_keybind._apply()` only when
   `status == "added"` (an "exists" add changes nothing). Wired in
   `MainWindow._make_keybind_callbacks` to a showcase refresh — **capturing the
   `main_frame` widget and resolving `self.frames.index(frame)` at callback
   time** (memory #56; the callbacks object can outlive a frame renumbering).
4. After a song removal (D3 step 4).
5. Settings change (D7).

### D5 — Thumbnails: fetch on workers, `album_img.png` placeholder
- Each song row fetches its thumbnail via
  `ThumbnailService.fetch_image(url, size=(px(40), px(40)))` on a daemon
  worker — same `_set_playlist_cover`/`_apply_cover` pattern (worker fetch,
  main-thread `to_photoimage`, `winfo_exists()` guard, `frame_img_refs[label]
  = [tk_img]` replace-not-append, `root.after` wrapped in try/except).
  Generalize `_set_playlist_cover` to take a size (or add a parallel
  `_set_song_thumb`); only the size differs.
- Missing URL or failed fetch → `assets/album_img.png` placeholder, loaded on
  the main thread via `IconService.get(path, 40)`. **Note:** `album_img.png`
  is in `.gitignore` — a fresh clone won't have it, so load it defensively and
  fall back to `playlist_image.png` (or an empty image) when it's absent.
- The card already clips long text (fixed box, weighted name column, no wrap),
  so song names/artists need no manual truncation — clipping handles them.

### D6 — Theme: reuse existing palette keys, no theme.txt changes
Showcase name/artist labels use `label_playlist_log_bg/fg`, thumb labels
inherit through `apply_theme`'s existing "remaining labels" loop (it already
walks `frame.winfo_children()` two levels deep, which covers the showcase
frame's direct children), remove buttons use `button_playlist_*`. **No new
`THEME_MAP` keys** → the four-place theme contract (theme.txt / THEME_MAP /
DEFAULT_THEME / theme.ini, memory #16) is untouched. Verify by changing the
theme with a showcase active.

### D7 — Settings apply live, without rebuilding frames
Count/log changes only affect the card height, log-row visibility and the
showcase section — the header, hotkeys and callbacks never move. So the
Settings dialog passes two new callbacks (mirroring `on_auto_resize_toggle` /
`on_tray_toggle`):

- `MainWindow.set_showcase_count(n)` → cache, then for each frame re-run
  `_refresh_showcase` + `_update_card_height`, then `_auto_resize()`.
- `MainWindow.set_showcase_log(show)` → cache, then per frame
  `main_log_frame.grid_remove()` / re-grid + `_update_card_height`, then
  `_auto_resize()`.

A full frame rebuild is explicitly **not** needed and must not use
`close_main_frame` (it deletes the playlist from the store).

### D8 — Out of scope (explicit)
- No CLI surface (`playlistmanager` headless adds don't touch the GUI).
- No "undo remove" and no multi-select remove.
- No dragging/reordering of showcase rows.
- Remove is permanent on the platform — no confirmation dialog in V1
  (note: a confirm dialog is a trivial follow-up if the user wants one).

## 4. Full change list (per file)

### `app/utils/config.py`
- `DEFAULT_SETTINGS` += `"showcase": {"count": "0"}`, `"showcase_log":
  {"is_true": "yes"}`. Nothing else — both existing helper pairs
  (`get_setting_value`/`set_setting_value`, `get_setting`/`set_setting`)
  already cover the new keys.

### `app/services/song_manager.py`
- `get_latest_songs(playlist_name, limit, platform=...) -> list[dict]` —
  `ORDER BY id DESC LIMIT ?`, artists JSON-decoded (D1). Returns `[]` on
  sqlite errors (match `get_all_songs`).

### `app/services/integration.py`
- `BaseIntegration.remove_track(self, playlist_id, track_id) -> bool` default
  `False`.
- `YouTubeMusicIntegration.remove_track` (ytmusicapi call, try/except → log +
  `False`).
- `SpotifyIntegration.remove_track` → delegates to `SpotifyAPI`.

### `app/integrations/music_spotify/music_spotify.py`
- `SpotifyAPI.remove_track_from_playlist(playlist_id, track_id)` → DELETE with
  401 refresh-retry (reuse the `_request` convention or copy the
  `add_tracks_to_playlist` retry shape).

### `app/ui/main_window.py` (bulk of the work)
- `__init__`: read + cache `_showcase_count`, `_show_log`; new
  `SONG_UNIT_H_BASE = 46`, `LOG_ROW_H_BASE = 23` constants; `set_showcase_count`
  / `set_showcase_log` live-appliers; pass both to the Settings dialog
  (see `settings_ui.py`).
- `create_main_frame`:
  - add `showcase_frame = tk.Frame(main_frame, background=frame_playlist_bg)`
    gridded at `row=2` (never populated here; `_refresh_showcase` fills it).
  - keep header/log rows exactly as today.
- New `_update_card_height(frame_idx)` (D2 formula).
- New `_refresh_showcase(frame_idx, playlist_name, platform)`:
  - `rows = min(self._showcase_count, len(songs))` via
    `SongManager().get_latest_songs(...)` (main-thread SQLite read, like
    `_update_log_labels_from_db`).
  - destroy any existing `showcase_frame`, rebuild `rows` blocks of
    thumb(40, rowspan=2) / name / remove-btn / artists; per-song row layout
    mirrors the header: `grid_columnconfigure(1, weight=1)` absorbs/clips.
  - remove button command captures **`main_frame` widget + `song_id` +
    `track_id`**, resolves name/platform/index at click time (D3).
  - kick off async thumbnail fetches; `_update_card_height` + `_auto_resize`.
- New `_on_remove_song(main_frame, song_id, track_id)` — the D3 worker flow.
- New `_apply_log_visibility(frame_idx)` — `grid_remove()` / re-grid.
- `setup()` / `_on_import_done()`: call `_refresh_showcase` after
  `_update_log_labels_from_db`.
- `_make_keybind_callbacks`: add `on_song_added` → showcase refresh via live
  frame-index resolution.
- `_set_playlist_cover`: take a size param (or add `_set_song_thumb`);
  placeholder fallback for song rows (D5).
- `close_main_frame`: **prune `frame_img_refs` for the showcase thumb labels
  before `frame.destroy()`** — the current code only pops the cover label's
  refs; showcase PhotoImages keyed by their labels would otherwise leak for
  the process lifetime (same bug class as the old reload leak, memory #74).
  Either walk the frame tree and pop every `Label`, or track the thumb labels
  in a per-frame registry.
- `apply_theme`: verify the existing two-level loop re-themes showcase labels;
  expect **no change** (D6).

### `app/controllers/keybind_registry.py`
- `KeybindCallbacks.__init__` gains `on_song_added: Callable[[], None] =
  lambda: None` (D4 item 3).

### `app/controllers/keybind_controller.py`
- `handle_keybind._apply()`: after the existing `on_song_info` call, invoke
  `callbacks.on_song_added()` when `status == "added"`.

### `app/ui/settings_ui.py`
- New "Showcase" block after the existing checkbox rows:
  - `ttk.Combobox` "Show last N added songs:" values `0,1,2,3,5,10`,
    initialised from `cfg.getint("showcase", "count", fallback=0)`; on select
    → `set_setting_value("showcase", "count", value)` + `on_showcase_count_change(int)`.
  - Checkbutton "Show log row (artist / song / status)" → `_toggle_setting(
    "showcase_log", var)` + `on_showcase_log_change(bool)`.
- `show_settings_dialog(...)` gains the two optional callbacks (the
  `on_auto_resize_toggle` pattern).

### `assets/` / README / AGENTS.md
- Reuse `assets/close_playlist.png` for the per-song remove button in V1 (it's
  tracked and 16 px like the other card buttons). A dedicated
  `remove_song.png` can replace it later.
- AGENTS.md "Config" section: after implementation, update the
  "five boolean sections" sentence (`showcase_log` makes six, plus the
  `[showcase] count` value option).

## 5. Edge cases

1. **Default off:** `count = 0` → no showcase frame, no height delta, log row
   as today — card must be pixel-identical to the current build (regression
   check below).
2. **Fewer songs than N:** show what exists; height tracks actual rows.
3. **Empty DB / before first import:** no rows, base card height.
4. **Log hidden + count = 0:** card is just the header (shorter) — allowed.
5. **Log hidden:** status messages are invisible by user choice; the remove
   button's disabled state is the only feedback — acceptable.
6. **Race: remove vs reload:** a reload starting mid-remove can re-import the
   track after the local delete (`INSERT OR IGNORE`). V1: per-frame
   `_removing` flag also disables the reload button while a remove is in
   flight; a mid-reload remove is prevented by the same flag. (Known residual
   race, documented — full serialization is a follow-up.)
7. **Remove with no `playlist_id` / unauthenticated integration:** error
   status, no DB write, row kept (D3 edge case).
8. **In-flight thumbnail after frame close or showcase rebuild:** the
   `winfo_exists()` + try/except `root.after` guards already cover both.
9. **`album_img.png` missing** (gitignored, fresh clone): placeholder
   fallback chain (D5).
10. **Setting changes while a remove/import is running:** refresh rebuilds the
    showcase section; any in-flight `after` targets destroyed widgets → guards
    from 8 apply.
11. **Hotkey add during recording of a new keybind on another frame:** no
    interplay — flows only touch their own frame's showcase via `on_song_added`.
12. **exFAT / case-insensitive filesystem:** use the exact filename casing
    `playlist_showcase.md` and `album_img.png` in all references.

## 6. Implementation order

1. `utils/config.py` — new DEFAULT_SETTINGS keys (nothing else depends on them
   yet; safe).
2. `services/song_manager.py` — `get_latest_songs`.
3. Integration layer — `remove_track` on `BaseIntegration`,
   `YouTubeMusicIntegration`, `SpotifyIntegration` + `SpotifyAPI`.
4. `keybind_registry.py` + `keybind_controller.py` — `on_song_added`.
5. `main_window.py` — showcase frame, card-height math, refresh triggers,
   remove flow, ref pruning, settings callbacks.
6. `settings_ui.py` — showcase row + callbacks.
7. Documentation (AGENTS.md config note) and asset decision.

## 7. Verification checklist

- `python -m compileall -q app/`.
- `python -m app --list` — headless smoke (settings parsing with the new
  sections present must not disturb the boolean/value paths).
- Regression: with `count=0` and log shown, the card is pixel-identical to
  today (width/height, no stray frames).
- Height math (headless Tk measurement if possible): count=3 → card height ≈
  `px(96 + 3*48)`; log hidden → `px(96 - 23 + rows*48)`; row heights actually
  show the full 40 px thumbnail and both text lines without clipping the cover.
- Newest-first ordering: add three songs via hotkey (A, then B, then C) → the
  showcase reads C on top, B under it, A at the bottom; card grows by one row
  per add. **Not** the reverse (first-added on top).
- Reload: rows and thumbnails update; a track no longer on the platform
  disappears.
- Remove: click a song's remove button → status Removing → Removed, row gone,
  card shrinks, and the track is actually gone from the platform playlist
  (verify in YT Music / Spotify); offline → Error, row kept, buttons
  re-enabled.
- Theme change with showcase active: song labels/buttons re-colored.
- Close a playlist frame with showcase rows and pending thumbnails → no
  crash, no traceback at quit, no PhotoImage leak (repeat open/close under
  `--debug` if desired).
- Scale sanity at 175 % (the user's real session): card heights, 40 px
  thumbnails and 16 px remove icons scale via `px()`/`IconService`.
- Settings: count combobox and log checkbox apply live; a restart persists
  the same values; unknown legacy sections in `cfg/settings.ini` survive.

## 8. Implementation status (2026-08-15)

**Implemented across 8 files** (`+639/-11`): `utils/config.py`,
`services/song_manager.py`, `services/integration.py`,
`integrations/music_spotify/music_spotify.py`,
`controllers/keybind_registry.py`, `controllers/keybind_controller.py`,
`ui/main_window.py`, `ui/settings_ui.py`. AGENTS.md "Config" section updated
(six boolean sections + the `showcase` value section). No new files; no new
theme keys; `count=0` renders exactly today's card.

Headless verification passed: `python -m compileall -q app/`; `python -m app
--list`; `get_latest_songs` exercised against a real playlist DB (newest
first: ids 9, 8, 7; empty DB → `[]`; `limit=0` → `[]`). The display was
available from 2026-08-15, so the REAL MainWindow was also exercised live:
setup with the user's config, card heights (563/617/401/617 at count=10),
keybind fully visible, log toggle grid_remove/restore with ±23 px height,
and grow-only window fitting (660x1289 requested, KWin-capped to 1000).
Still pending on the user's machine: remove round trip (platform
confirmation + row removal) and visual acceptance of the final layout.

### Findings that deviated from this plan

1. **`SONG_UNIT_H_BASE` is 48, not 46.** Each song block is two font-12
   lines (~46 px) **plus the thumbnail's 2 px top padding** = 48 px. With 46,
   content per row exceeds the reserved height by 2 px, and the original 7 px
   card slack only absorbs it up to N=3; N=10 clips ~13 px off the last row.
   Formula still `px(CARD_H_BASE) + rows * px(SONG_UNIT_H_BASE)` with the
   constant now 48, keeping 7 px slack at every N.
2. **No initial empty `showcase_frame` in `create_main_frame`.** The plan
   sketched gridding a placeholder frame at row 2; it is never referenced by
   `_refresh_showcase` (which builds and grids its own frames), so it would
   have sat as a 1 px strip overlapping the real showcase and could swallow
   clicks on the first row of remove buttons. Only
   `grid_rowconfigure(2, weight=0)` remains.
3. **`_prune_frame_imgs` is an instance method**, not `@staticmethod` — it
   must reach `self.frame_img_refs`.
4. **Loop variable in `create_main_frame` is `i`**, not `frame_idx` — the
   plan's code sketch used the latter; the real call is
   `_update_card_height(i)`. (A first draft used `frame_idx` and raised
   `NameError` on every frame creation; caught in review.)
5. **`status_label` is resolved before the guard checks** in
   `_on_remove_song`. Draft had the `track_id`/`song_id` guard writing to
   `status_label` before it was assigned (`NameError` on the legacy-row
   path); fixed by hoisting `status_label = labels["status"]` above the
   guards.
6. **Remove is refused when `track_id` or `song_id` is falsy** (legacy DB
   rows) with an Error status — nothing to remove platform-side, so nothing
   may be deleted locally.
7. **`_load_song_placeholder` falls back to a 40 px cover image**, not the
   64 px one — a 64 px image in the 40 px slot would inflate the thumbnail
   column on a fresh clone (where gitignored `album_img.png` is absent).
8. **Song thumbnails reuse `_apply_cover` unchanged** — it is fully generic
   (winfo_exists guard, TclError guard, `frame_img_refs[label] = [tk_img]`
   replace-not-append); `_set_playlist_cover` stays as the 64 px wrapper.
9. **`ensure_settings_file()` confirmed to merge new sections** — existing
   user `cfg/settings.ini` files gain `[showcase]`/`[showcase_log]` on the
   next read; unknown legacy sections are untouched.
10. **`fetch_image(thumb_url, size=(px(40), px(40)))`** — cover-fits the
    square thumb slot (memory #74 contract), so 16:9 video thumbnails are
    center-cropped, not stretched.
11. **Window growth (fix for the 2026-08-15 user report).** With
    `auto_resize` OFF (the default — and the user's setting), the window
    never re-fitted after showcase height changes, so cards grew past the
    650x460 window and content was clipped ("with 5 songs I only see the
    playlist name, close button and half the playlist img"). Fix:
    `_update_card_height` now re-fits the window on every showcase height
    change — full `_auto_resize()` when the setting is on, otherwise
    `resize_window(root, grow_only=True)` (new optional param, default
    False so existing callers are untouched). Grow-only never shrinks a
    manually sized window; unmapped/startup windows compare against the
    parsed geometry string via `_geometry_size()` (winfo_* reports 1x1
    pre-map). Guarded by `_showcase_count > 0` — `count=0` stays
    pixel-identical and never calls resize.
12. **Screen-height ceiling (known limit, documented).** At 175% UI scale
    (this user) one card with count=N is `px(96 + N*48)` physical px, so
    two card rows with count=10 need ~2100 px — taller than the screen;
    the window then exceeds the display and the bottom cards are
    unreachable (the root grid has no scrolling). Practical ceiling on
    this machine is ~count=4 with 4 playlists (fits in 1440 px). If more
    songs per card are wanted, the next step is a per-card scrollable
    showcase region instead of growing the card.
13. **`SONG_UNIT_H_BASE` is gone — the showcase height is now MEASURED,
    not computed** (second user report, 2026-08-15: "with 5 songs I only
    see the playlist name, close button and 1/10 of the playlist img").
    The real block height on this machine is ~54 px per song (DejaVu Sans
    12 double-row ≈ 52 + thumb padding), not 48 — the constant
    under-reserved the card, and Tk starves the WEIGHTED header row when
    the grid is short: header req 66 vs allocated 43 → the keybind row
    (at y=33, h=33) had only 10 px inside the header and overflowed it,
    and the showcase (drawn later, weight 0, keeps its requested 270)
    covered the overflow. Slack = 7 − 6·N: N=1-2 "slightly overflows",
    N=5 → −23 px, exactly the reported rendering. `_update_card_height`
    now reads `showcase_frame.winfo_reqheight()` (self-correcting across
    fonts/scales/borders — also absorbs the user's
    `padx=2, borderwidth=2, relief="solid"` frame) and sets
    `card = px(CARD_H_BASE) + showcase_h − px(LOG_ROW_H_BASE) when log
    hidden`. Verified on the real MainWindow with the display available:
    header always 69 px, keybind fully visible, log row gets its 23 px
    when shown, N=0 stays base.
14. **`winfo_reqheight()` on a freshly built showcase returns stale 1x1
    until the layout idle callbacks run once** — `_update_card_height`
    calls `self.root.update_idletasks()` (guarded) before measuring.
15. **Live log toggle verified working** on the real MainWindow (display
    available 2026-08-15): `set_showcase_log` → `_apply_log_visibility`
    grid/grid_remove + height ±23 px, card grows/shrinks accordingly.
    The user's "toggle does nothing" report predates fix 13 — with the
    starved layout the log row was squeezed to ~11 px and looked the same
    in both states.
16. **KWin caps the window at screen height** (observed in the live test:
    requested 660x1289, actual 660x1000 on a 1080p display) — the
    grow-only fit works; the ceiling is the WM, reinforcing finding 12.
17. **Window shrink on showcase decrease** (user request, 2026-08-15):
    the fit became a two-mode `_fit_window(allow_shrink)`.  Explicit
    showcase changes - `set_showcase_count` / `set_showcase_log` - call
    `_fit_window(allow_shrink=True)` (full `resize_window`, grow AND
    shrink, regardless of the `auto_resize` setting), so decreasing the
    count or hiding the log row shrinks the window back to the cards.
    Passive changes (song add/remove, reload) keep grow-only
    (`allow_shrink=False`) so a manually sized window is never shrunk
    behind the user's back.  Verified live on the real MainWindow:
    count 3→1→0 shrank the window 660x554 → 660x338 → 660x222, and the
    log toggle re-fits ±px(23) per row both ways.  Startup with a
    persisted count>0 grows the window during setup (660x770 for
    count=5, log hidden); count=0 leaves it pixel-identical to the
    legacy 650x460 (verified: the fit is skipped entirely).

### Remaining GUI verification (user machine)

- Card height at count=3 ≈ `px(96 + 3*54)`; log hidden → `px(96 - 23 +
  rows*54)`; full 40 px thumbnail + both text lines visible, no clipping,
  keybind row fully visible (header never starved — heights are measured).
- **Window growth/shrink (2026-08-15):** with `auto_resize` OFF, raising
  the count in Settings must grow the window until all cards fit; lowering
  it must shrink the window back (finding 17); restarting with count=2 must open the
  window sized to fit. With `auto_resize` ON it grows and shrinks as before.
- Newest-first rows; live count/log toggles; remove round trip (platform
  confirmation + row removal + card shrink); close-frame / quit without
  PhotoImage leaks; 175 % scale sanity.
