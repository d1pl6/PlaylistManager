"""
OS / platform utility functions.

Provides helpers for opening directories in the file manager and
launching terminal emulators for the YouTube Music auth flow.
"""

import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

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


def x11_root_desktop_state() -> tuple[int | None, bool | None]:
    """Read two EWMH root-window properties from the X server.

    Returns ``(current_desktop, showing_desktop)``:

    - ``current_desktop``: the value of ``_NET_CURRENT_DESKTOP``, i.e.
      which virtual desktop the WM is currently on.
    - ``showing_desktop``: ``True`` when ``_NET_SHOWING_DESKTOP`` is 1
      (the WM is in "show the desktop" mode - all windows minimized),
      ``False`` when it is 0, ``None`` when the property is unset.

    Either element is ``None`` on any failure (``xprop`` missing, no X
    display, unreadable property, timeout) - callers must treat ``None``
    as "unknown, keep the previous behavior".

    Used by the hide-to-tray logic to tell a genuine per-window minimize
    apart from WM-wide actions that also unmap/minimize windows
    (virtual desktop switches, "show the desktop").
    """
    try:
        out = subprocess.run(
            ["xprop", "-root", "_NET_CURRENT_DESKTOP", "_NET_SHOWING_DESKTOP"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    current_desktop: int | None = None
    showing_desktop: bool | None = None
    for line in out.splitlines():
        m = re.match(r"_NET_CURRENT_DESKTOP[^=]*=\s*(\d+)", line)
        if m:
            current_desktop = int(m.group(1))
            continue
        m = re.match(r"_NET_SHOWING_DESKTOP[^=]*=\s*(\d+)", line)
        if m:
            showing_desktop = m.group(1) == "1"
    return current_desktop, showing_desktop


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


# Known terminal emulators, tried in order when no explicit default is
# configured.  The list order is a last-resort heuristic only: konsole is
# common on KDE systems while foot/kitty are the typical Wayland defaults,
# so a user on e.g. Hyprland gets konsole here unless they export
# ``$TERMINAL`` (or ``xdg-terminal-exec`` resolves the right default).
KNOWN_TERMINALS = [
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
]


def find_linux_terminal() -> str | None:
    """Resolve the terminal emulator to launch, honoring the user's default.

    Priority:
    1. ``$TERMINAL`` - explicit user intent (compositor configs commonly
       export this, e.g. ``env = TERMINAL,foot`` in hyprland.conf).
    2. ``xdg-terminal-exec`` - freedesktop default-terminal resolution
       that consults the desktop's configured default application.  Only
       returned when installed; ``get_terminal_command()`` launches it
       *without* ``-e`` because the wrapper forwards the command itself.
    3. ``x-terminal-emulator`` - the update-alternatives default (how a
       distro-declared default is supposed to win over this module).
    4. The well-known emulators in :data:`KNOWN_TERMINALS` order.
    """
    env_terminal = os.environ.get("TERMINAL", "").strip()
    if env_terminal:
        # $TERMINAL may be a bare name or a full path; ignore bogus values.
        resolved = shutil.which(env_terminal)
        if resolved:
            return resolved
        if (
            env_terminal.startswith("/")
            and os.path.isfile(env_terminal)
            and os.access(env_terminal, os.X_OK)
        ):
            return env_terminal
    if shutil.which("xdg-terminal-exec"):
        return "xdg-terminal-exec"
    if shutil.which("x-terminal-emulator"):
        return "x-terminal-emulator"
    for term in KNOWN_TERMINALS:
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


def get_terminal_command(work_dir: Path) -> list[str]:
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
        if term == "xdg-terminal-exec":
            # The freedesktop wrapper launches the configured default
            # terminal itself and forwards the command as positional
            # args - no "-e" flag.
            return [term, shell, "-c", cmd]
        return [term, "-e", shell, "-c", cmd]
