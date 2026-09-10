"""
Window geometry utilities.

Extracted from ``utils/__init__.py`` (Issue #9) so the package init
file does not contain executable logic.
"""

import re
import tkinter as tk


def center_window(win: tk.Misc) -> None:
    """Centre *win* on screen.

    For unmapped windows (``winfo_width()`` is 1 before the first map -
    including a start-in-tray boot where the root is withdrawn before
    setup) the requested size is used, so the window is later shown
    centred correctly.
    """
    win.update_idletasks()
    try:
        w = win.winfo_width()
        h = win.winfo_height()
        if w <= 1 or h <= 1:  # not yet mapped - use the requested size
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
    except tk.TclError:
        return
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")


def fit_window_to_screen(win: tk.Misc, margin: int = 60) -> None:
    """Cap *win*'s size so it never exceeds the screen (minus *margin*).

    Content taller than the display (e.g. the theme dialog's swatch list)
    would otherwise open with its bottom rows unreachable; the window's
    ScrollableFrame then scrolls the overflow.  Never grows a window: a
    caller-set geometry smaller than the content is respected.

    For unmapped windows (``winfo_width()`` is 1 before the first map)
    the requested size is used.  Call after the content is laid out
    (``update_idletasks``) and before :func:`center_window`.
    """
    win.update_idletasks()
    try:
        w = win.winfo_width()
        h = win.winfo_height()
        if w <= 1:  # not yet mapped - use the requested size
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
    except tk.TclError:
        return
    max_w = max(win.winfo_screenwidth() - margin, 100)
    max_h = max(win.winfo_screenheight() - margin, 100)
    new_w = min(w, max_w)
    new_h = min(h, max_h)
    try:
        win.geometry(f"{new_w}x{new_h}")
    except tk.TclError:
        pass


def _geometry_size(win: tk.Misc) -> tuple:
    """Return (width, height) from the window's geometry string.

    Works for unmapped windows, where ``winfo_width()``/``winfo_height()``
    are not meaningful (they report 1x1 before the first map).
    """
    try:
        geo = win.geometry()  # "WxH+X+Y"
        if geo and "x" in geo:
            size = geo.split("+")[0]
            w, h = size.split("x", 1)
            return int(w), int(h)
    except Exception:
        pass
    return win.winfo_width(), win.winfo_height()


def resize_window(win: tk.Misc, grow_only: bool = False) -> None:
    """Auto-resize a main window to fit playlist frames laid out on a grid.

    The function inspects gridded children of ``win`` and calculates the
    required width/height by summing column widths and row heights for
    children placed on grid rows >= 1 (playlists).  The header (row 0)
    is accounted for in height.  The resulting size respects the current
    ``minsize`` of the window.

    When the window is mapped the new size is applied around its current
    center, so growing the window for a new playlist row (or shrinking it
    after a close) does not drift it down-right from a stale top-left
    corner.  A manually dragged window is left where the user put it -
    the center stays anchored.  Unmapped windows (e.g. the initial resize
    during startup, before ``mainloop()`` maps the root) keep the old
    position-preserving behaviour, since ``winfo_x()`` is meaningless
    before mapping.

    With ``grow_only`` the window is only ever enlarged to fit its
    content: a window the user sized larger (auto-resize off) is never
    shrunk.  Used for showcase-driven growth, where cards grow taller
    than the fixed window size.

    The content-driven height is capped just below the screen height -
    overflow beyond that is scrolled by the main window's cards canvas
    (auto-hidden scrollbar) instead of pushing the window off-screen.
    """
    win.update_idletasks()

    children = win.winfo_children()
    col_widths: dict[int, int] = {}
    row_heights: dict[int, int] = {}
    header_height = 0

    for child in children:
        try:
            gi = child.grid_info()
        except Exception:
            continue

        row = int(gi.get("row", 0)) if gi.get("row") is not None else 0
        col = int(gi.get("column", 0)) if gi.get("column") is not None else 0

        req_w = child.winfo_reqwidth()
        req_h = child.winfo_reqheight()

        if row == 0:
            header_height = max(header_height, req_h)
            continue

        col_widths[col] = max(col_widths.get(col, 0), req_w)
        row_heights[row] = max(row_heights.get(row, 0), req_h)

    if not row_heights:
        return

    total_w = sum(col_widths.get(c, 0) for c in sorted(col_widths.keys()))
    total_h = header_height + sum(
        row_heights.get(r, 0) for r in sorted(row_heights.keys())
    )

    total_w += 20
    total_h += 20

    try:
        min_w, min_h = win.minsize()
        total_w = max(total_w, min_w)
        total_h = max(total_h, min_h)
    except Exception:
        pass

    # Cap the content-driven height at the screen (minus a margin for WM
    # decorations/taskbars): a playlist grid taller than the display is
    # handled by the main window's scrollable cards area instead of
    # growing the window off-screen.
    try:
        total_h = min(total_h, win.winfo_screenheight() - 100)
    except Exception:
        pass

    if grow_only:
        if win.winfo_ismapped():
            cur_w, cur_h = win.winfo_width(), win.winfo_height()
        else:
            cur_w, cur_h = _geometry_size(win)
        total_w = max(total_w, cur_w)
        total_h = max(total_h, cur_h)

    if win.winfo_ismapped():
        # Anchor the current center so add/remove of frames doesn't drift
        # the window down-right from its old top-left corner.
        new_x = win.winfo_x() + (win.winfo_width() - total_w) // 2
        new_y = win.winfo_y() + (win.winfo_height() - total_h) // 2
        win.geometry(f"{total_w}x{total_h}+{new_x}+{new_y}")
    else:
        win.geometry(f"{total_w}x{total_h}")


# ---------------------------------------------------------------------------
# Geometry persistence (settings.ini "[window] geometry = WxH+X+Y")
#
# Tk grammar note: in a geometry string, "-X" means "X pixels from the
# RIGHT edge" (absolute = screenwidth - X - W), while "+-X" means the
# absolute negative coordinate.  {x:+d} would emit "-X" for negatives and
# silently restore to the wrong place, so positions are formatted as
# "+N" / "+-N" explicitly (see _format_pos; verified empirically).
# ---------------------------------------------------------------------------

_POS_NUM_RE = re.compile(r"[+-]?\d+")


def _parse_geometry(text):
    """Parse ``WxH+X+Y`` (any sign) into ``(w, h, x, y)`` or None.

    Handles Tk's absolute-negative form (``WxH+-X+Y``).  ``-X`` right-edge
    convention strings are not produced by :func:`save_window_geometry`
    and are rejected.  Garbage input returns None.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        size, sep, rest = text.partition("x")
        if not sep:
            return None
        w = int(size)
        sign = re.search(r"[+-]", rest)
        if sign is None:
            return None  # no position part ("WxH" alone) - not ours
        h = int(rest[: sign.start()])
        nums = [int(n) for n in _POS_NUM_RE.findall(rest[sign.start() :])]
        if len(nums) != 2:
            return None
    except (ValueError, IndexError):
        return None
    if w <= 0 or h <= 0:
        return None
    return w, h, nums[0], nums[1]


def _clamp_rect(rect, screen_w, screen_h, margin=60, min_size=100):
    """Clamp the size part of ``(w, h, x, y)`` into the screen.

    Never grows a window beyond the screen minus *margin* (a saved
    geometry from a taller monitor would otherwise leave the bottom
    unreachable - the same rule ``fit_window_to_screen`` applies).
    """
    w, h, x, y = rect
    max_w = max(screen_w - margin, min_size)
    max_h = max(screen_h - margin, min_size)
    return (max(min_size, min(w, max_w)), max(min_size, min(h, max_h)), x, y)


def _visible(rect, screen_w, screen_h):
    """True when the window rect overlaps the primary screen.

    Deliberately an intersection test: windows parked on a monitor left of
    or above the primary (negative coords) stay valid, while rects thrown
    entirely off-screen by a resolution change / unplugged monitor are
    rejected (caller falls back to centering).  A window fully on a
    right/bottom secondary monitor is *not* visible by this test and gets
    re-centered once - Tk cannot enumerate monitors, so the primary
    screen is all we know.
    """
    w, h, x, y = rect
    return x + w > 0 and y + h > 0 and x < screen_w and y < screen_h


def _format_pos(num):
    """"+N" for N >= 0, "+-N" for N < 0 (absolute, see module note)."""
    return f"+{num}" if num >= 0 else f"+-{-num}"


def _should_save_geometry(win) -> bool:
    """Only persist a sane, non-fullscreen, non-maximized window.

    Skipped states: iconic/withdrawn (position may be stale or unmapped),
    "zoomed" (a maximized rect would reopen the app maximized - avoid,
    and this Tk build cannot reliably read the un-maximized rect), and
    fullscreen (screen-sized rect).  The previous good value stays saved.
    """
    try:
        return (
            win.state() == "normal"
            and win.winfo_ismapped()
            and not bool(win.attributes("-fullscreen"))
        )
    except (tk.TclError, AttributeError, Exception):
        return False


def save_window_geometry(win) -> str:
    """Build ``WxH+X+Y`` from the window's RESOLVED geometry.

    Reads ``winfo_width/height/x/y`` (resolved integers; the geometry()
    string getter round-trips negative positions incorrectly).  Returns
    "" when the window state should not be persisted.
    """
    if not _should_save_geometry(win):
        return ""
    try:
        w, h, x, y = (
            win.winfo_width(),
            win.winfo_height(),
            win.winfo_x(),
            win.winfo_y(),
        )
    except Exception:
        return ""
    if w <= 0 or h <= 0:
        return ""
    return f"{w}x{h}{_format_pos(x)}{_format_pos(y)}"


def restore_window_geometry(win, geometry_text: str) -> bool:
    """Apply a saved ``WxH+X+Y`` to *win*; True when applied.

    Validate + clamp + visibility-checked before touching the window.
    Position is best-effort: a WM/compositor (Wayland especially) is free
    to place the window itself; the size request is usually honored.
    """
    if not geometry_text:
        return False
    rect = _parse_geometry(geometry_text)
    if rect is None:
        return False
    try:
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
    except Exception:
        return False
    rect = _clamp_rect(rect, screen_w, screen_h)
    if not _visible(rect, screen_w, screen_h):
        return False
    w, h, x, y = rect
    try:
        win.geometry(f"{w}x{h}{_format_pos(x)}{_format_pos(y)}")
        return True
    except Exception:
        return False
