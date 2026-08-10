"""
Entry point (legacy).  ``python app/main.py`` still works, but
``python main.py`` or ``python -m app`` is preferred.

Kept for backward compatibility - will be removed in a future release.
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cli import (
    run_add,
    run_add_url,
    run_del,
    run_list,
    run_login,
    run_logout,
    run_refresh,
)
from utils.logging_config import configure_logging


def parse_args():
    p = argparse.ArgumentParser(
        prog="playlistmanager",
        description="PlaylistManager - add the currently-playing song to your playlists.",
    )
    p.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="more logging: -v = INFO, -vv = DEBUG (same as --debug)",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="verbose logging (DEBUG level, same as -vv)",
    )
    p.add_argument(
        "--trace", action="store_true",
        help="ultra-verbose logging (TRACE level + third-party debug)",
    )
    p.add_argument(
        "-a", "--add-song", dest="add_song_targets", metavar="PLAYLISTS",
        help='add the currently-playing song to playlist(s), e.g. "1,2,3", "1-3"',
    )
    p.add_argument(
        "-p", "--playlist", dest="playlist_args", nargs="+",
        metavar="ACTION [TARGETS]...",
        help='playlist management: "add <URL>", "del <TARGETS>", "ref <TARGETS>" '
             '(del/ref accept numbers, names and playlist URLs; "all" targets every '
             "playlist; delete/refresh are aliases)",
    )
    p.add_argument(
        "-l", "--list", dest="list_only", action="store_true",
        help="print numbered playlists and exit",
    )
    p.add_argument(
        "--login", dest="login_platform", metavar="PLATFORM",
        help='log in to a platform: "youtube_music" or "spotify" '
             "(spotify also needs --client-id/--client-secret/--refresh-token)",
    )
    p.add_argument(
        "--logout", dest="logout_platform", metavar="PLATFORM",
        help="log out of a platform: delete its credentials, registry "
             "entries and local databases",
    )
    p.add_argument(
        "--client-id", dest="client_id", metavar="ID",
        help="Spotify client ID (with --login spotify)",
    )
    p.add_argument(
        "--client-secret", dest="client_secret", metavar="SECRET",
        help="Spotify client secret (with --login spotify)",
    )
    p.add_argument(
        "--refresh-token", dest="refresh_token", metavar="TOKEN",
        help="Spotify refresh token (with --login spotify)",
    )
    return p.parse_args()


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
    verbosity = args.verbose
    if args.debug:
        verbosity = max(verbosity, 2)
    if args.trace:
        verbosity = 3
    configure_logging(verbosity)

    if args.list_only:
        sys.exit(run_list())
    if args.login_platform is not None:
        sys.exit(
            run_login(
                args.login_platform,
                args.client_id,
                args.client_secret,
                args.refresh_token,
            )
        )
    if args.logout_platform is not None:
        sys.exit(run_logout(args.logout_platform))
    if args.add_song_targets is not None:
        sys.exit(run_add(args.add_song_targets))
    if args.playlist_args is not None:
        verb = args.playlist_args[0].lower()
        rest = ",".join(args.playlist_args[1:])
        if verb == "add":
            sys.exit(run_add_url(rest))
        if verb in ("del", "delete"):
            sys.exit(run_del(rest))
        if verb in ("ref", "refresh"):
            sys.exit(run_refresh(rest))
        print(
            f"error: unknown playlist action '{verb}' "
            "(use add, del/delete, ref/refresh)",
            file=sys.stderr,
        )
        sys.exit(2)

    App = _import_app()
    app = App(args)
    app.run()
    sys.exit(0)


if __name__ == "__main__":
    main()
