"""Reusable scrollable container: Canvas + auto-hiding Scrollbar + content Frame.

Consolidates the repeated Canvas+Scrollbar+mousewheel pattern found in
``main_window.py``, ``settings_ui.py``, ``settings_theme_ui.py``, and
``playlist_dialog.py`` into a single widget.

Usage::

    sf = ScrollableFrame(parent, bg=C["root_bg"])
    tk.Label(sf.content, text="Hello").pack()
    sf.update_scrollregion()  # after adding/removing children

Attributes:
    content  -- the inner Frame to pack/grid children into
    canvas   -- the Canvas (for direct yview calls if needed)
    scrollbar -- the auto-hiding Scrollbar
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk

from utils.theme import C, hover_bg

logger = logging.getLogger(__name__)


class ScrollableFrame(tk.Frame):
    """A scrollable container with an auto-hiding vertical scrollbar.

    Parameters
    ----------
    parent:
        Parent widget.
    bg:
        Background colour for the canvas, content frame, and scrollbar trough.
    show_scrollbar:
        When *True* (default), the scrollbar is always visible.  When
        *False*, it auto-hides when the content fits the viewport.
    bind_all_mousewheel:
        When *True*, mousewheel events are bound via ``bind_all`` with an
        ancestry gate (used by ``MainWindow`` so wheels over deep card
        children still scroll).  When *False*, mousewheel events are
        bound to ``self``, ``content``, and ``canvas`` only.
    scrollbar_width:
        Width of the scrollbar thumb in pixels.
    """

    def __init__(
        self,
        parent,
        *,
        bg: str = "#1A1A1A",
        show_scrollbar: bool = True,
        bind_all_mousewheel: bool = False,
        scrollbar_width: int = 10,
    ) -> None:
        super().__init__(parent, background=bg)

        # --- Canvas ---
        self.canvas = tk.Canvas(
            self, background=bg, highlightthickness=0, bd=0,
        )
        self.canvas.pack(side="left", fill="both", expand=True)

        # --- Content frame (children go here) ---
        self.content = tk.Frame(self.canvas, background=bg)
        self._content_window_id = self.canvas.create_window(
            (0, 0), window=self.content, anchor="nw",
        )

        # --- Scrollbar (auto-hiding) ---
        self._show_scrollbar = show_scrollbar
        self._scrollbar_visible = False
        self.scrollbar = tk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            highlightthickness=0,
            relief="flat",
            width=scrollbar_width,
        )
        self.canvas.configure(yscrollcommand=self._on_yscroll)

        # Stretch content frame to visible canvas width.
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        # --- Mousewheel ---
        self._bind_mousewheel(bind_all_mousewheel)

    # ------------------------------------------------------------------
    # Scrollbar drive + auto-hide
    # ------------------------------------------------------------------

    def _on_yscroll(self, first, last) -> None:
        """Keep the scrollbar thumb in sync; defer visibility check."""
        try:
            self.scrollbar.set(first, last)
        except (TypeError, ValueError, tk.TclError):
            return
        self._schedule_scrollbar_sync()

    def _schedule_scrollbar_sync(self) -> None:
        """Defer the show/hide decision until the layout settles."""
        try:
            self.after(0, self._sync_scrollbar_visibility)
        except tk.TclError:
            pass

    def _sync_scrollbar_visibility(self) -> None:
        """Show/hide scrollbar from real sizes once the canvas is laid out."""
        if not self._show_scrollbar:
            return
        try:
            if not self.canvas.winfo_ismapped():
                return
            content_h = self.content.winfo_reqheight()
            viewport_h = self.canvas.winfo_height()
            self._set_scrollbar_visible(content_h > viewport_h)
        except tk.TclError:
            logger.debug(
                "Scrollbar visibility sync failed (teardown?)", exc_info=True,
            )

    def _set_scrollbar_visible(self, needs: bool) -> None:
        if needs and not self._scrollbar_visible:
            self.scrollbar.pack(side="right", fill="y")
            self._scrollbar_visible = True
        elif not needs and self._scrollbar_visible:
            self.scrollbar.pack_forget()
            self._scrollbar_visible = False

    # ------------------------------------------------------------------
    # Canvas sizing
    # ------------------------------------------------------------------

    def _on_canvas_resize(self, event) -> None:
        """Stretch the content frame to the visible canvas width."""
        try:
            self.canvas.itemconfigure(
                self._content_window_id, width=event.width,
            )
        except tk.TclError:
            return
        self.update_scrollregion()

    def update_scrollregion(self) -> None:
        """Sync the canvas request size to its content and refresh the region.

        Call after adding/removing children or when the layout changes.
        The canvas width/height are set from the content frame's requested
        size so ``resize_window`` (which sizes the window from root
        children) gets accurate dimensions.
        """
        try:
            self.canvas.update_idletasks()
            self.canvas.configure(
                width=self.content.winfo_reqwidth(),
                height=self.content.winfo_reqheight(),
            )
            bbox = self.canvas.bbox("all")
            if bbox:
                self.canvas.configure(scrollregion=bbox)
        except tk.TclError:
            logger.debug(
                "Scroll region update failed (teardown?)", exc_info=True,
            )
        self._schedule_scrollbar_sync()

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def style_scrollbar(self, thumb_bg: str, trough_bg: str) -> None:
        """Theme the scrollbar thumb and trough from the current palette."""
        try:
            self.scrollbar.configure(
                bg=thumb_bg,
                activebackground=hover_bg(thumb_bg),
                troughcolor=trough_bg,
            )
        except tk.TclError:
            logger.debug(
                "Scrollbar style failed (teardown?)", exc_info=True,
            )

    # ------------------------------------------------------------------
    # Mousewheel
    # ------------------------------------------------------------------

    def _bind_mousewheel(self, bind_all: bool) -> None:
        if bind_all:
            target = self.winfo_toplevel()
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                target.bind_all(seq, self._on_mousewheel, add="+")
        else:
            for widget in (self, self.content, self.canvas):
                for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    widget.bind(seq, self._on_mousewheel)

    def _on_mousewheel(self, event) -> str | None:
        """Cross-platform scroll gated on the pointer being inside this frame.

        Button-4/5 (Linux), MouseWheel delta (macOS/Windows).  Returns
        ``"break"`` when the event was consumed so parent bindings don't
        double-scroll.
        """
        w = event.widget
        if isinstance(w, str):
            try:
                w = self.winfo_toplevel().nametowidget(w)
            except KeyError:
                return None
        # Gate: only scroll if the pointer is inside this ScrollableFrame.
        while w is not None and w is not self.winfo_toplevel():
            if w is self.canvas or w is self.content:
                break
            w = getattr(w, "master", None)
        else:
            return None
        try:
            if event.num == 4:
                self.canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self.canvas.yview_scroll(1, "units")
            else:
                delta = getattr(event, "delta", 0)
                if delta:
                    self.canvas.yview_scroll(
                        -1 if delta > 0 else 1, "units",
                    )
        except tk.TclError:
            return None
        return "break"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def grid_configure_children(self, **kw) -> None:
        """Pass ``grid_columnconfigure`` to the content frame."""
        self.content.grid_columnconfigure(**kw)

    def destroy_content(self) -> None:
        """Destroy all children inside the content frame."""
        for child in self.content.winfo_children():
            child.destroy()
