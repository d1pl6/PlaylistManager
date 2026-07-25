import tkinter as tk


def center_window(win: tk.Misc) -> None:
    win.update_idletasks()
    w = win.winfo_width()
    h = win.winfo_height()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")


def resize_window(win: tk.Misc) -> None:
    """Auto-resize a main window to fit playlist frames laid out on a grid.

    The function inspects gridded children of `win` and calculates the
    required width/height by summing column widths and row heights for
    children placed on grid rows >= 1 (playlists). The header (row 0)
    is accounted for in height. Resulting size respects the current
    `minsize` of the window.
    """
    win.update_idletasks()

    children = win.winfo_children()
    col_widths = {}
    row_heights = {}
    header_height = 0

    for child in children:
        try:
            gi = child.grid_info()
        except Exception:
            # Child is not managed by grid or not yet mapped
            continue

        # grid_info returns strings; default to 0
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
        # Nothing to size for (no playlist frames)
        return

    # Sum the widest width per column and heights per row
    total_w = sum(col_widths.get(c, 0) for c in sorted(col_widths.keys()))
    total_h = header_height + sum(row_heights.get(r, 0) for r in sorted(row_heights.keys()))

    # Account for some padding/margins used by the app layout
    total_w += 20
    total_h += 20

    try:
        min_w, min_h = win.minsize()
        total_w = max(total_w, min_w)
        total_h = max(total_h, min_h)
    except Exception:
        pass

    win.geometry(f"{total_w}x{total_h}")
