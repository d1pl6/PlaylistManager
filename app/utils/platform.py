"""
OS / platform utility functions.

Extracted from ``app/ui/login_ui.py`` (Issue #2).  Provides helpers for
opening directories in the file manager and launching terminal emulators
for the YouTube Music auth flow.
"""

import logging
import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def is_wayland_session() -> bool:
    """Whether the session is running a Wayland compositor.

    ``WAYLAND_DISPLAY`` being set is the reliable "a Wayland compositor
    is running" signal; ``XDG_SESSION_TYPE`` can be absent or stale.
    Either one indicating Wayland is enough -- notably an X11 app
    launched from a Wayland session still has ``XDG_SESSION_TYPE=wayland``,
    which is exactly what callers want: the compositor protocol is
    Wayland, so X11-only integration points (X11 system tray, global
    keyboard grabs) do not exist regardless of the app's own backend.
    """
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get(
        "XDG_SESSION_TYPE", ""
    ).lower() == "wayland"


def open_directory(path: Path) -> None:
    """Open *path* in the system file manager."""
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(
                ["open", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as e:
        logger.error("Failed to open directory %s: %s", path, e)


def find_linux_terminal() -> Optional[str]:
    """Return the path of the first supported terminal emulator found."""
    for term in [
        "kgx",
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "xterm",
        "alacritty",
        "kitty",
        "ghostty",
        "wezterm",
        "tilix",
        "foot",
        "x-terminal-emulator",
    ]:
        if shutil.which(term):
            return term
    return None


def find_shell() -> str:
    """Return the path of the first available shell."""
    for sh in ["bash", "sh", "dash", "mksh", "busybox"]:
        path = shutil.which(sh)
        if path:
            return path
    for p in ["/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh"]:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return "sh"


def get_terminal_command(work_dir: Path) -> List[str]:
    """Build a command list that opens a terminal running ``ytmusicapi browser``.

    Raises ``FileNotFoundError`` on Linux when no supported terminal
    emulator is installed.
    """
    system = platform.system()
    if system == "Windows":
        return ["cmd", "/k", f'cd /d "{work_dir}" && ytmusicapi browser']
    elif system == "Darwin":
        # "open -a Terminal <dir>" opens Finder, not a terminal - drive
        # Terminal.app via AppleScript instead so the command actually runs.
        escaped_dir = str(work_dir).replace('"', '\\"')
        return [
            "osascript",
            "-e",
            f'tell app "Terminal" to do script "cd \\"{escaped_dir}\\" && ytmusicapi browser"',
        ]
    else:
        term = find_linux_terminal()
        if not term:
            raise FileNotFoundError("No supported terminal emulator found")
        shell = find_shell()
        # work_dir comes from platformdirs (typically ~/.config/playlistmanager/
        # auth) - quote it so an unusual home-dir name ($, backtick, quote)
        # cannot break or hijack the shell command.  shell is quoted too even
        # though it is normally an absolute path, because find_shell()'s last
        # resort is the bare name "sh".
        cmd = (
            f"cd {shlex.quote(str(work_dir))} && ytmusicapi browser; "
            f"exec {shlex.quote(shell)}"
        )
        return [term, "-e", shell, "-c", cmd]
