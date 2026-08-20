"""PlaylistCard dataclass - bundles all per-playlist-card state.

Replaces the five parallel lists (``frames``, ``frame_positions``,
``playlist_name_labels``, ``frame_platforms``, ``active_log_labels``)
that MainWindow previously kept in lockstep.  One ``PlaylistCard``
instance travels with its card through renumbering, reordering, and
deletion - no index bookkeeping required.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field


@dataclass
class PlaylistCard:
    """Encapsulates every widget and metadata slot for one playlist card."""

    # -- Main widget tree ------------------------------------------------
    frame: tk.Frame = field(repr=False)
    header_frame: tk.Frame = field(repr=False)
    name_label: tk.Label = field(repr=False)
    cover_label: tk.Label = field(repr=False)
    keybind_entry: tk.Entry = field(repr=False)
    reload_btn: tk.Button = field(repr=False)

    # -- Stats row -------------------------------------------------------
    stats_frame: tk.Frame = field(repr=False)
    stats_songs: tk.Label = field(repr=False)
    stats_duration: tk.Label = field(repr=False)
    stats_followers: tk.Label = field(repr=False)

    # -- Log row ---------------------------------------------------------
    log_frame: tk.Frame = field(repr=False)
    log_artist: tk.Label = field(repr=False)
    log_name: tk.Label = field(repr=False)
    log_status: tk.Label = field(repr=False)

    # -- Identity --------------------------------------------------------
    platform: str = ""
    playlist_id: str = ""

    # -- Grid position ---------------------------------------------------
    position: tuple[int, int] = (0, 0)

    # -- Showcase (last-N-songs section) ---------------------------------
    showcase_frame: tk.Frame | None = None
    showcase_rows: int = 0

    # -- In-flight operation guards --------------------------------------
    removing: bool = False
    syncing: bool = False
