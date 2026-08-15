"""
Window geometry utilities.

Extracted from ``utils/__init__.py`` (Issue #9) so the package init
file does not contain executable logic.
"""

import tkinter as tk


def center_window(win: tk.Misc) -> None:
    """Centre *win* on screen."""
    win.update_idletasks()
    w = win.winfo_width()
    h = win.winfo_height()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")


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
