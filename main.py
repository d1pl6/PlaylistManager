#!/usr/bin/env python3
"""
PlaylistManager - entry point.

Usage:
  python main.py              # launch GUI
  python main.py --debug      # verbose logging
  python main.py add 1,2,3    # CLI: add current song to playlists #1, #2, #3
  python main.py -a 1         # CLI: same, option style
  python main.py --list       # CLI: print numbered playlists
  python -m app               # equivalent alternative
"""

import sys
from pathlib import Path

# Insert the repo root so the `app` package resolves here exactly as it does
# for `python -m app` from the repo root. resolve() keeps this working when
# the script is invoked through a symlink (e.g. ~/.local/bin/playlistmanager).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main

if __name__ == "__main__":
    main()
