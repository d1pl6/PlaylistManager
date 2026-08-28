"""ShowcaseManager - owns the last-N-songs showcase section of playlist cards.

Extracted from MainWindow to reduce its size.  Handles building,
refreshing, and removing song rows, song thumbnails, stats, log labels,
card height, and the playlist cover thumbnail pipeline.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
import webbrowser
from pathlib import Path

from PIL import Image

from services.playlist_store import PlaylistStore
from services.playlist_url import build_song_url
from services.song_manager import SongManager
from ui.tooltip import ToolTip
from utils.config import get_setting
from utils.icons import IconService
from utils.scaling import px, ui_font
from utils.theme import C, btn_colors
from utils.thumbnail import ThumbnailService

logger = logging.getLogger(__name__)

assets_dir = Path(__file__).resolve().parents[2] / "assets"


class ShowcaseManager:
    def __init__(
        self,
        root,
        card_grid,
        song_manager,
        *,
        song_placeholder_img=None,
        close_playlist_img=None,
        heart_empty_img=None,
        heart_full_img=None,
        make_keybind_callbacks=None,
        on_reload_requested=None,
        get_search_results_height=None,
        on_remove_done=None,
        integrations=None,
        card_index_fn=None,
        search_results=None,
        playlist_cover_img_path=None,
    ) -> None:
        self.root = root
        self._card_grid = card_grid
        self._song_manager = song_manager
        self._playlist_cover_img_path = playlist_cover_img_path
        self.song_placeholder_img = song_placeholder_img or self._load_song_placeholder()
        self._close_playlist_img = close_playlist_img
        self._heart_empty_img = heart_empty_img
        self._heart_full_img = heart_full_img
        self._make_keybind_callbacks = make_keybind_callbacks
        self._on_reload_requested = on_reload_requested
        self._get_search_results_height = get_search_results_height
        self._on_remove_done = on_remove_done
        self.integrations = integrations
        self._card_index_fn = card_index_fn
        self._search_results = search_results or {}
        self.frame_img_refs: dict = {}

    def _card_index(self, card) -> int | None:
        if self._card_index_fn:
            return self._card_index_fn(card)
        try:
            return self._card_grid.cards.index(card)
        except ValueError:
            return None

    def _load_song_placeholder(self):
        album_path = assets_dir / "album_img.png"
        try:
            return IconService.get(album_path, 40)
        except FileNotFoundError:
            logger.debug(
                "album_img.png placeholder missing; falling back to playlist_image.png"
            )
            if self._playlist_cover_img_path is not None:
                return IconService.get(self._playlist_cover_img_path, 40)
            raise

    def _set_playlist_cover(self, cover_label: tk.Label, thumb_url: str, *, card=None) -> None:
        def fetch() -> None:
            img = ThumbnailService.fetch_image(thumb_url, size=(px(64), px(64)))
            if img is not None:
                try:
                    self.root.after(0, lambda: self._apply_cover(cover_label, img))
                except Exception:
                    logger.debug("Window closed during cover download", exc_info=True)

        threading.Thread(target=fetch, daemon=True).start()
        # Attach metadata for double-click-to-open-image.
        try:
            cover_label._orig_thumb_url = thumb_url
            if card is not None:
                cover_label._owning_card = card
            cover_label.bind("<Double-Button-1>", lambda e: self._open_image_window(cover_label))
        except Exception:
            pass

    def _apply_cover(self, cover_label: tk.Label, img) -> None:
        try:
            if not cover_label.winfo_exists():
                return
            tk_img = ThumbnailService.to_photoimage(img)
        except Exception as e:
            logger.error("Failed to create cover PhotoImage: %s", e)
            return
        try:
            cover_label.configure(image=tk_img)
        except tk.TclError:
            return
        self.frame_img_refs[cover_label] = [tk_img]

    def _prune_frame_imgs(self, container) -> None:
        for child in container.winfo_children():
            if isinstance(child, tk.Label):
                refs = self.frame_img_refs.pop(child, None)
                if refs:
                    refs.clear()
            elif isinstance(child, tk.Frame):
                self._prune_frame_imgs(child)

    def _fetch_song_thumb(self, thumb_label: tk.Label, thumb_url: str, *, card=None) -> None:
        def fetch() -> None:
            img = ThumbnailService.fetch_image(thumb_url, size=(px(40), px(40)))
            if img is not None:
                try:
                    self.root.after(0, lambda: self._apply_cover(thumb_label, img))
                except Exception:
                    logger.debug(
                        "Window closed during song thumb download", exc_info=True
                    )

        threading.Thread(target=fetch, daemon=True).start()
        # Attach metadata for double-click-to-open-image.
        try:
            thumb_label._orig_thumb_url = thumb_url
            if card is not None:
                thumb_label._owning_card = card
            thumb_label.bind("<Double-Button-1>", lambda e, t=thumb_label: self._open_image_window(t))
        except Exception:
            pass

    def _refresh_showcase(
        self, frame_idx: int, playlist_name: str, platform: str
    ) -> None:
        try:
            card = self._card_grid.cards[frame_idx]
        except IndexError:
            return

        old_showcase = card.showcase_frame
        if old_showcase is not None:
            self._prune_frame_imgs(old_showcase)
            old_showcase.destroy()
            card.showcase_frame = None

        rows = []
        if self._card_grid._showcase_count > 0:
            rows = self._song_manager.get_latest_songs(
                playlist_name,
                self._card_grid._showcase_count,
                platform=platform,
                playlist_id=card.playlist_id,
            )

        card.showcase_rows = len(rows)
        if rows:
            showcase_frame, thumb_jobs = self._build_showcase_frame(card.frame, rows)
            if frame_idx not in self._search_results:
                showcase_frame.grid(row=3, column=0, sticky="nsew")
            card.showcase_frame = showcase_frame
            for thumb_label, url in thumb_jobs:
                self._fetch_song_thumb(thumb_label, url, card=card)

        self._card_grid._update_card_height(frame_idx, layout=True)

    def _refresh_stats(
        self, frame_idx: int, playlist_name: str, platform: str
    ) -> None:
        try:
            card = self._card_grid.cards[frame_idx]
        except IndexError:
            return

        playlist_id = card.playlist_id

        sm = self._song_manager
        song_count = sm.get_song_count(
            playlist_name, platform=platform, playlist_id=playlist_id
        )
        total_seconds = sm.get_total_duration(
            playlist_name, platform=platform, playlist_id=playlist_id
        )

        store_entry = PlaylistStore.find_playlist(
            playlist_name, platform=platform, playlist_id=playlist_id
        )
        follower_count = (store_entry or {}).get("followerCount", 0) if store_entry else 0

        if card.stats_songs is not None:
            card.stats_songs.config(text=f"{song_count} song{'s' if song_count != 1 else ''}")
        if card.stats_duration is not None:
            card.stats_duration.config(text=self._format_duration(total_seconds))
        if card.stats_followers is not None:
            if follower_count > 0:
                card.stats_followers.config(
                    text=f"{follower_count} follower{'s' if follower_count != 1 else ''}"
                )
            else:
                card.stats_followers.config(text="")

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds <= 0:
            return "\u2014"
        h, remainder = divmod(int(seconds), 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{h}h {m:02d}m"
        if m > 0:
            return f"{m}m {s:02d}s"
        return f"{s}s"

    def _build_showcase_frame(self, main_frame: tk.Frame, songs: list) -> tk.Frame:
        frame_playlist_bg = C["frame_playlist_bg"]
        label_playlist_log_bg = C["label_playlist_log_bg"]
        label_playlist_log_fg = C["label_playlist_log_fg"]
        remove_cols = btn_colors(C["button_playlist_bg"], C["button_playlist_fg"])

        showcase = tk.Frame(main_frame, background=frame_playlist_bg)
        showcase.grid_columnconfigure(1, weight=1)

        # Resolve the owning card once — constant for the entire showcase.
        card = next(
            (c for c in self._card_grid.cards if c.frame is main_frame), None
        )
        platform = card.platform if card is not None else None

        jobs = []
        for row_idx, song in enumerate(songs):
            grid_row = row_idx * 2

            thumb = tk.Label(
                showcase,
                image=self.song_placeholder_img,
                background=label_playlist_log_bg,
            )
            song_name = tk.Label(
                showcase,
                text=song.get("title", ""),
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                anchor="w",
            )
            artists = song.get("artists", [])
            artists_str = (
                ", ".join(artists[:2]) if isinstance(artists, list) else str(artists)
            )
            song_artists = tk.Label(
                showcase,
                text=artists_str,
                font=ui_font(12),
                background=label_playlist_log_bg,
                foreground=label_playlist_log_fg,
                anchor="w",
            )
            remove_btn = tk.Button(
                showcase,
                image=self._close_playlist_img,
                cursor="hand2",
                **remove_cols,
                highlightthickness=0,
                relief="raised",
                command=lambda f=main_frame, sid=song.get("id"), tid=song.get("track_id"),
                         title=song.get("title"), artists=song.get("artists"): (
                    self._on_remove_song(f, sid, tid, title, artists)
                ),
            )
            ToolTip(remove_btn, "Remove from playlist")

            thumb.grid(
                row=grid_row, column=0, rowspan=2, sticky="nsew",
                padx=(0, 2), pady=(2, 0),
            )
            song_name.grid(row=grid_row, column=1, sticky="nsew")
            remove_btn.grid(row=grid_row, column=2, sticky="ne")
            song_artists.grid(row=grid_row + 1, column=1, sticky="nsew")

            # Like button (♥/♡) — shown only if like_button setting is enabled
            if get_setting("like_button"):
                like_btn = tk.Button(
                    showcase,
                    image=self._heart_empty_img,
                    cursor="hand2",
                    **btn_colors(C["button_playlist_bg"], C["button_playlist_fg"]),
                    highlightthickness=0,
                    relief="raised",
                )
                # Command set AFTER construction: the default arg ``b=like_btn``
                # is evaluated eagerly (at lambda creation), so referencing
                # ``like_btn`` inside the constructor would read an unbound
                # variable (UnboundLocalError).  Doing it here captures the
                # per-iteration value in the loop.
                like_btn.config(
                    command=lambda t=song.get("title"), a=song.get("artists", []), b=like_btn: (
                        self._on_like_toggle(t, a, b)
                    )
                )
                ToolTip(like_btn, "Like on Last.fm")
                like_btn.grid(row=grid_row + 1, column=2, sticky="ne")

                # Load like state asynchronously
                artist = song.get("artists", [])[0] if song.get("artists") else ""
                title = song.get("title", "")
                if artist and title:
                    def load_like_state(btn=like_btn, a=artist, t=title):
                        scrobble_cap = next(
                            (
                                getattr(integ, "is_loved", None)
                                for integ in (self.integrations.get_all().values() if self.integrations else [])
                                if getattr(integ, "is_loved", None) is not None
                            ),
                            None,
                        )
                        if scrobble_cap is None:
                            return
                        is_loved = scrobble_cap(a, t)
                        if is_loved is not None:
                            try:
                                self.root.after(0, lambda: btn.config(image=self._heart_full_img if is_loved else self._heart_empty_img))
                            except Exception:
                                logger.debug("Window closed while loading like state", exc_info=True)

                    threading.Thread(target=load_like_state, daemon=True).start()

            track_id = song.get("track_id") or ""
            if track_id and platform:
                song_url = build_song_url(platform, track_id)
                if song_url:
                    song_name.bind(
                        "<Button-1>", lambda _e, u=song_url: webbrowser.open(u)
                    )
                    song_name.configure(cursor="hand2")

            # Attach metadata for double-click-to-open-image and YouTube
            # maxres lookup.
            try:
                thumb._track_id = song.get("track_id")
                if card is not None:
                    thumb._owning_card = card
            except Exception:
                pass
            thumb_url = song.get("thumbnail_url") or ""
            if thumb_url:
                jobs.append((thumb, thumb_url))

        return showcase, jobs

    def _open_image_window(self, widget: tk.Label) -> None:
        """Open a new window showing the highest-quality image available.

        Strategy:
          - If the widget has a bound _track_id and the card's platform is
            YouTube Music, try standard YouTube maxres/sd/hq urls.
          - Otherwise use the attached _orig_thumb_url and fetch the full image.
        """
        # Resolve the owning card from the attached attribute, falling back
        # to a widget-tree walk for legacy widgets that lack it.
        card = getattr(widget, "_owning_card", None)
        if card is None:
            try:
                parent = widget
                while parent is not None:
                    for c in self._card_grid.cards:
                        if c.frame is parent:
                            card = c
                            break
                    if card:
                        break
                    parent = getattr(parent, "master", None)
            except Exception:
                card = None
        platform = card.platform if card is not None else None

        track_id = getattr(widget, "_track_id", None)
        orig_url = getattr(widget, "_orig_thumb_url", None)

        candidates = []
        if platform == "youtube_music" and track_id:
            # try YouTube image candidates from best -> fallback
            candidates = [
                f"https://i.ytimg.com/vi/{track_id}/maxresdefault.jpg",
                f"https://i.ytimg.com/vi/{track_id}/sddefault.jpg",
                f"https://i.ytimg.com/vi/{track_id}/hqdefault.jpg",
                f"https://i.ytimg.com/vi/{track_id}/mqdefault.jpg",
                f"https://i.ytimg.com/vi/{track_id}/default.jpg",
            ]
        if orig_url:
            candidates.append(orig_url)

        # Try candidates until one fetches successfully.
        def worker():
            img = None
            used_url = None
            for url in candidates:
                try:
                    img = ThumbnailService.fetch_full_image(url)
                except Exception:
                    img = None
                if img is not None:
                    used_url = url
                    break
            if img is None:
                return

            def show():
                try:
                    win = tk.Toplevel(self.root)
                    win.title("Image")
                    # Fit window to image, but clamp to screen size.
                    screen_w = win.winfo_screenwidth()
                    screen_h = win.winfo_screenheight()
                    img_w, img_h = img.size
                    max_w = int(screen_w * 0.9)
                    max_h = int(screen_h * 0.9)
                    display = img
                    if img_w > max_w or img_h > max_h:
                        display = img.copy()
                        display.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                    photo = ThumbnailService.to_photoimage(display)
                    lbl = tk.Label(win, image=photo)
                    lbl.image = photo
                    lbl.pack()

                    def _cleanup():
                        try:
                            ThumbnailService.clear_cache_for(used_url, None)
                        except Exception:
                            logger.debug("Failed clearing cached image %r", used_url, exc_info=True)

                    def _close():
                        _cleanup()
                        win.destroy()

                    win.protocol("WM_DELETE_WINDOW", _close)

                    def _close_on_key(ev):
                        try:
                            if getattr(ev, "keysym", "").lower() in ("escape", "q"):
                                _close()
                        except Exception:
                            pass

                    win.bind("<Key>", _close_on_key)
                    try:
                        win.focus_set()
                    except Exception:
                        pass
                except Exception:
                    logger.debug("Failed to show image window", exc_info=True)

            try:
                self.root.after(0, show)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_remove_song(
        self, main_frame: tk.Frame, song_id: int, track_id: str, title: str = "", artists: list = None
    ) -> None:
        if artists is None:
            artists = []
        card = next((c for c in self._card_grid.cards if c.frame is main_frame), None)
        if card is None:
            return
        playlist_name = card.name_label.cget("text")
        platform = card.platform
        status_label = card.log_status

        if card.removing or card.syncing:
            return
        if not track_id or not song_id:
            status_label.config(
                text="Error", background=C["label_playlist_error_bg"]
            )
            return
        card.removing = True

        status_label.config(text="Removing", background=C["label_playlist_warn_bg"])

        buttons = self._frame_buttons(main_frame)
        for btn in buttons:
            try:
                btn.config(state="disabled", cursor="arrow")
            except tk.TclError:
                pass

        playlist_data = PlaylistStore.find_playlist(
            playlist_name,
            platform,
            playlist_id=card.playlist_id,
        )
        playlist_id = playlist_data.get("playlist_id", "") if playlist_data else ""
        integration = self.integrations.get(platform) if self.integrations else None

        def work() -> None:
            ok = False
            if not playlist_id:
                logger.error(
                    "No playlist_id for '%s' (%s); cannot remove track %s",
                    playlist_name, platform, track_id,
                )
            elif integration is None or not integration.is_authenticated():
                logger.error(
                    "Integration %s not authenticated; cannot remove track %s",
                    platform, track_id,
                )
            else:
                ok = integration.remove_track(playlist_id, track_id)
                if ok:
                    self._song_manager.delete_song(
                        playlist_name,
                        song_id,
                        platform=platform,
                        playlist_id=playlist_id,
                    )
                    # Delete scrobble if Last.fm integration is available and auto-scrobble is on
                    if get_setting("scrobble_on_add"):
                        scrobble_integ = next(
                            (integ for integ in (self.integrations.get_all().values() if self.integrations else [])
                             if getattr(integ, "delete_scrobble", None) is not None),
                            None,
                        )
                        if scrobble_integ is not None and title and artists:
                            artist = artists[0] if isinstance(artists, list) else str(artists)
                            try:
                                scrobble_integ.delete_scrobble(artist, title)
                            except Exception as e:
                                logger.debug("Failed to delete scrobble for %s: %s", title, e)

            def done() -> None:
                card.removing = False
                try:
                    if not card.frame.winfo_exists():
                        return
                except tk.TclError:
                    return
                for btn in buttons:
                    try:
                        btn.config(state="normal")
                    except tk.TclError:
                        pass
                if ok:
                    status_label.config(
                        text="Removed", background=C["label_playlist_good_bg"]
                    )
                    cur_idx = self._card_index(card)
                    if cur_idx is not None:
                        pname = card.name_label.cget("text")
                        self._refresh_showcase(cur_idx, pname, card.platform)
                        self._refresh_stats(cur_idx, pname, card.platform)
                else:
                    status_label.config(
                        text="Error", background=C["label_playlist_error_bg"]
                    )

            try:
                self.root.after(0, done)
            except Exception:
                logger.debug(
                    "App is shutting down; dropped remove-done update",
                    exc_info=True,
                )

        threading.Thread(target=work, daemon=True).start()

    def _on_like_toggle(
        self, title: str, artists: list, like_btn: tk.Button | None = None
    ) -> None:
        """Toggle the like status of a song on Last.fm.

        The love/unlove round trip runs in a daemon thread (it is a Last.fm
        API call).  When *like_btn* is given, its glyph is flipped on the
        main thread to match the new loved state once the API confirms.
        """
        if not artists or not title:
            return

        # Find a ScrobbleCapable integration
        scrobble_integ = next(
            (integ for integ in (self.integrations.get_all().values() if self.integrations else [])
             if getattr(integ, "unlove", None) is not None and getattr(integ, "is_loved", None) is not None),
            None,
        )
        if scrobble_integ is None:
            return

        artist = artists[0] if isinstance(artists, list) else str(artists)

        def work() -> None:
            try:
                is_loved = scrobble_integ.is_loved(artist, title)
                new_state = None  # leave the glyph alone on uncertainty
                if is_loved is True:
                    scrobble_integ.unlove(artist, title)
                    new_state = False
                elif is_loved is False:
                    scrobble_integ.love(artist, title)
                    new_state = True
                if like_btn is not None and new_state is not None:
                    self._apply_like_glyph(like_btn, new_state)
            except Exception as e:
                logger.debug("Failed to toggle like for %s: %s", title, e)

        threading.Thread(target=work, daemon=True).start()

    def _apply_like_glyph(self, like_btn: tk.Button, loved: bool) -> None:
        """Flip a like button's glyph on the main thread (guarded).

        Called from a worker thread, so the update must be marshalled to the
        tkinter main thread via ``after`` and guarded in case the window was
        destroyed or the button no longer exists.
        """
        try:
            self.root.after(
                0,
                lambda: (
                    like_btn.config(
                        image=self._heart_full_img if loved else self._heart_empty_img
                    )
                    if like_btn.winfo_exists()
                    else None
                ),
            )
        except Exception:
            logger.debug("Window closed while updating like glyph", exc_info=True)

    @staticmethod
    def _frame_buttons(main_frame: tk.Frame) -> list:
        buttons: list[tk.Button] = []
        def _walk(w):
            for child in w.winfo_children():
                if isinstance(child, tk.Button):
                    buttons.append(child)
                elif isinstance(child, tk.Frame):
                    _walk(child)
        _walk(main_frame)
        return buttons

    # -- Delegated from CardGridManager ---------------------------------

    def update_card_height(self, frame_idx: int, *, layout: bool = False) -> None:
        self._card_grid._update_card_height(frame_idx, layout=layout)

    def apply_log_visibility(self, frame_idx: int) -> None:
        self._card_grid._apply_log_visibility(frame_idx)

    def apply_stats_visibility(self, frame_idx: int) -> None:
        self._card_grid._apply_stats_visibility(frame_idx)

    def update_log_labels_from_db(
        self, frame_idx: int, playlist_name: str, platform: str
    ) -> None:
        self._card_grid._update_log_labels_from_db(frame_idx, playlist_name, platform)

    # -- Aliases for callers that use the un-prefixed public name -------

    refresh = _refresh_showcase
    refresh_stats = _refresh_stats
    set_playlist_cover = _set_playlist_cover
    apply_cover = _apply_cover
    fetch_song_thumb = _fetch_song_thumb
    load_song_placeholder = _load_song_placeholder
    prune_frame_imgs = _prune_frame_imgs
    on_remove_song = _on_remove_song
