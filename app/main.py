"""
Entry point (legacy).  ``python app/main.py`` still works, but
``python main.py`` or ``python -m app`` is preferred.

Kept for backward compatibility - will be removed in a future release.
"""

import sys
import argparse
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cli import run_add, run_list


def parse_args():
    p = argparse.ArgumentParser(
        prog="playlistmanager",
        description="PlaylistManager - add the currently-playing song to your playlists.",
    )
    p.add_argument("--debug", action="store_true", help="verbose logging")

    sub = p.add_subparsers(dest="command")
    add_p = sub.add_parser("add", help="add the currently-playing song to playlist(s)")
    add_p.add_argument(
        "targets",
        nargs="?",
        help='playlist order numbers and/or names, e.g. "1,2,3", "1-3", "1,\\"Chill Mix\\""',
    )
    add_p.add_argument(
        "-l", "--list", dest="list_only", action="store_true",
        help="print numbered playlists and exit",
    )

    p.add_argument(
        "-a", "--add", dest="add_targets", metavar="PLAYLISTS",
        help="add the currently-playing song to playlist(s) (option style, same as 'add')",
    )
    p.add_argument(
        "-l", "--list", dest="list_only", action="store_true",
        help="print numbered playlists and exit",
    )
    return p.parse_args()


def configure_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s: %(message)s")


def _import_app():
    """
    Import the App class.

    `app` is both a package (repo root / the `app` directory) and a module
    (app/app.py), so the resolution depends on how this file was loaded:
    - as a plain module (python main.py / python app/main.py) `from app import
      App` finds app/app.py;
    - as part of the package (python -m app) the package `app` is already in
      sys.modules without an `App` attribute, so use the relative import.
    """
    if __package__:
        from .app import App
    else:
        from app import App
    return App


def main():
    args = parse_args()
    configure_logging(args.debug)

    if args.list_only:
        sys.exit(run_list())
    if args.command == "add":
        sys.exit(run_add(args.targets or ""))
    if args.add_targets is not None:
        sys.exit(run_add(args.add_targets))

    App = _import_app()
    app = App(args)
    app.run()
    sys.exit(0)


if __name__ == "__main__":
    main()
