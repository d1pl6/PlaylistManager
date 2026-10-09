"""Cross-platform desktop notifications (best-effort delivering sink).

A thin sink that posts one OS-native notification.  It knows nothing
about widgets (no tkinter import) and adds no third-party dependency of
its own - it degrades to a logged no-op when no backend is usable, the
same posture as :mod:`services.tray`.

Backend per platform:

* **Linux** - ``notify-send`` (libnotify -> D-Bus
  ``org.freedesktop.Notifications``), falling back to ``gdbus call`` when
  libnotify is not installed.  A D-Bus call works on X11 and Wayland
  alike.
* **macOS** - ``osascript -e 'display notification ...'``: built in and
  needs no code signing (the modern ``UNUserNotificationCenter`` route
  needs a signed executable, which a bare Python interpreter is not).
* **Windows** - the running tray icon's ``Shell_NotifyIcon`` balloon
  (:meth:`services.tray.TrayService.notify`), falling back to a
  PowerShell/WinRT toast.

``ui/main_window.py`` decides *which* event to send: it calls
:func:`notify_event` from the same choke point that feeds the
notification bar.  This module then maps the event kind to a
``[desktop_notify]`` category, builds the body and delivers it; the text
it is handed is already translated and already redacted by the caller.

Delivery is fire-and-forget on a subprocess, so a slow notifier never
blocks the caller (the tkinter thread for bar events, the poll for
queued near-duplicates).
"""

import base64
import logging
import os
import platform
import shutil
import subprocess

from utils.config import desktop_notify_enabled
from utils.i18n import tr, tr_status

logger = logging.getLogger(__name__)

#: Notification-bar event kind -> ``[desktop_notify]`` category.  The
#: kinds are exactly the ones ``ui/notification_bar.py`` renders; a kind
#: absent here (``scan``, ``status``) is never turned into a toast, so
#: progress and scan summaries stay in-window only.
KIND_TO_CATEGORY = {
    "added": "success",
    "exists": "duplicate",
    "duplicate": "extra",
    "error": "errors",
}

_available_cache: bool | None = None
_warned_unavailable = False

# PowerShell toast template.  __TITLE__/__BODY__ are replaced with
# .replace(), not str.format(), so the script's own braces would need no
# escaping (there are none today, but a future edit must not have to
# care).
_WINDOWS_TOAST_SCRIPT = (
    "$ErrorActionPreference = 'Stop'\n"
    "[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]\n"
    "[void][Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]\n"
    "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
    "$texts = $template.GetElementsByTagName('text')\n"
    "[void]$texts.Item(0).AppendChild($template.CreateTextNode(__TITLE__))\n"
    "[void]$texts.Item(1).AppendChild($template.CreateTextNode(__BODY__))\n"
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($template)\n"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PlaylistManager')"
    ".Show($toast)\n"
)


#: Live notification subprocesses.  Reaped on the next spawn so a finished
#: notify-send/osascript never lingers as a zombie until process exit.
_LIVE: list = []


def available() -> bool:
    """Whether a usable notification backend exists (probed once).

    Windows always reports True (PowerShell ships with a stock install);
    macOS needs ``osascript``; Linux needs ``notify-send`` or ``gdbus``.
    Cached for the process - the answer cannot change at runtime.
    """
    global _available_cache
    if _available_cache is None:
        _available_cache = _probe_available()
    return _available_cache


def _probe_available() -> bool:
    system = platform.system()
    if system == "Windows":
        return True
    if system == "Darwin":
        return shutil.which("osascript") is not None
    return shutil.which("notify-send") is not None or shutil.which("gdbus") is not None


def build_body(
    category: str,
    *,
    artist: str = "",
    song: str = "",
    playlist: str = "",
    status: str = "",
) -> str:
    """Render the body text for a notification *category*.

    ``song`` is the ``"Artist - Song"`` identity the templates show; it is
    empty when the event carried neither field, in which case a
    non-error toast is skipped rather than shown as ``" was added to
    X"``.  A missing playlist falls back to the identity alone (no
    dangling ``"to "``).  Errors always render (the message is the
    point), falling back to a generic label when the status code is
    unknown.
    """
    artist = (artist or "").strip()
    song = (song or "").strip()
    playlist = (playlist or "").strip()
    label = f"{artist} - {song}" if artist and song else (artist or song)
    if category == "errors":
        message = tr_status(status) if status else ""
        if label:
            return tr(
                "desktop_notify.error",
                song=label,
                error=message or tr("desktop_notify.generic_error"),
            ).strip()
        return message.strip()
    if not label:
        return ""
    if category == "success":
        if not playlist:
            # No playlist context (the card vanished mid-flow) - the
            # identity alone still says what happened.
            return label
        return tr("desktop_notify.added", song=label, playlist=playlist).strip()
    if category == "duplicate":
        if not playlist:
            return label
        return tr("desktop_notify.exists", song=label, playlist=playlist).strip()
    if category == "extra":
        if not playlist:
            return label
        return tr("desktop_notify.extra", song=label, playlist=playlist).strip()
    return ""


def notification_for(
    category: str,
    *,
    artist: str = "",
    song: str = "",
    playlist: str = "",
    status: str = "",
) -> tuple[str, str] | None:
    """``(title, body)`` for a *category*, or ``None`` when there is none.

    Pure: no settings read, no delivery - the caller (or
    :func:`notify_event`) decides whether it may be shown.
    """
    body = build_body(
        category, artist=artist, song=song, playlist=playlist, status=status
    )
    if not body:
        return None
    return tr("app.title"), body


def notify_event(
    kind: str,
    *,
    artist: str = "",
    song: str = "",
    playlist: str = "",
    status: str = "",
    tray=None,
    icon_path: str | None = None,
) -> bool:
    """Gate, build and deliver a notification-bar event.

    *kind* is a ``show_event`` kind; it is mapped to a ``[desktop_notify]``
    category via :data:`KIND_TO_CATEGORY`, and that category's switch is
    consulted (all four default to off).  An unmapped kind or a
    switched-off category is a no-op - so this is safe to call from every
    ``show_event`` site.  Returns whether a backend was handed the toast.
    """
    try:
        category = KIND_TO_CATEGORY.get(kind)
        if category is None or not desktop_notify_enabled(category):
            return False
        payload = notification_for(
            category, artist=artist, song=song, playlist=playlist, status=status
        )
        if payload is None:
            return False
        title, body = payload
        return notify(title, body, tray=tray, icon_path=icon_path)
    except Exception:
        logger.debug("Desktop notification path failed", exc_info=True)
        return False


def notify(
    title: str, body: str, *, tray=None, icon_path: str | None = None
) -> bool:
    """Deliver one desktop notification.  Fire-and-forget, never raises.

    *tray* is the live :class:`services.tray.TrayService` (or None); it is
    only consulted on Windows, where its balloon is preferred over the
    PowerShell toast.  Returns whether a backend was actually invoked.
    """
    title = (title or "").strip()
    body = (body or "").strip()
    if not title and not body:
        return False
    global _warned_unavailable
    if not available():
        # A category is switched on but this machine has no notifier
        # (minimal Linux without notify-send/gdbus).  Say so once, then
        # stay quiet.
        if not _warned_unavailable:
            _warned_unavailable = True
            logger.warning(
                "Desktop notifications are enabled but no backend is "
                "available on this system; toasts will be skipped"
            )
        return False
    try:
        system = platform.system()
        if system == "Windows":
            if tray is not None and getattr(tray, "can_notify", False):
                tray.notify(body, title or None)
                return True
            return _notify_windows(title, body)
        if system == "Darwin":
            return _notify_macos(title, body)
        return _notify_linux(title, body, icon_path)
    except Exception as e:
        logger.debug("Desktop notification failed: %s", e)
        return False


def _spawn(argv: list[str]) -> None:
    """Start *argv* detached with every stream discarded."""
    kwargs = {}
    if os.name == "nt":
        # Never flash a console window for the PowerShell fallback.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=os.name != "nt",
        **kwargs,
    )
    # Keep a handle and reap finished children on the next spawn, so a
    # one-shot notify-send/osascript cannot zombie until the process exits.
    _LIVE.append(proc)
    _LIVE[:] = [p for p in _LIVE if p.poll() is None]


def _notify_linux(title: str, body: str, icon_path: str | None) -> bool:
    notify_send = shutil.which("notify-send")
    if notify_send:
        argv = [notify_send, "--app-name", "PlaylistManager"]
        if icon_path:
            argv += ["--icon", str(icon_path)]
        argv += [title, body]
        _spawn(argv)
        return True
    gdbus = shutil.which("gdbus")
    if gdbus:
        _spawn(
            [
                gdbus,
                "call",
                "--session",
                "--dest",
                "org.freedesktop.Notifications",
                "--object-path",
                "/org/freedesktop/Notifications",
                "--method",
                "org.freedesktop.Notifications.Notify",
                "PlaylistManager",
                "0",
                "",
                title,
                body,
                "[]",
                "{}",
                "5000",
            ]
        )
        return True
    return False


def _apple_literal(value: str) -> str:
    """A double-quoted AppleScript string literal for *value*."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def _notify_macos(title: str, body: str) -> bool:
    osascript = shutil.which("osascript")
    if not osascript:
        return False
    script = (
        f"display notification {_apple_literal(body)} "
        f"with title {_apple_literal(title)}"
    )
    _spawn([osascript, "-e", script])
    return True


def _ps_literal(value: str) -> str:
    """A single-quoted PowerShell string literal for *value*."""
    return "'" + value.replace("'", "''") + "'"


def _notify_windows(title: str, body: str) -> bool:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return False
    script = _WINDOWS_TOAST_SCRIPT.replace("__TITLE__", _ps_literal(title)).replace(
        "__BODY__", _ps_literal(body)
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    _spawn([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])
    return True
