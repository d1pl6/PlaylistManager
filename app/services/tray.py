"""System tray support (pystray wrapper).

TrayService is backend plumbing - no tkinter imports, knows nothing
about widgets.  It wraps :class:`pystray.Icon` and exposes
:attr:`available` - whether a tray icon could be constructed at all
(missing dependency, headless display, backend failure, or an
X11-system-tray-only backend on a Wayland session → False).

Callbacks passed to :meth:`start` fire on the tray backend thread; the
caller must marshal them to the tkinter main thread via
``root.after(0, ...)``.
"""

import logging
import threading
from pathlib import Path

from utils.platform import is_wayland_session

logger = logging.getLogger(__name__)

try:
    import pystray
    from pystray import Menu as _Menu
    from pystray import MenuItem as _MenuItem
    _PYSTRAY_OK = True
except ImportError:
    _PYSTRAY_OK = False
    pystray = None
    _Menu = None
    _MenuItem = None

APP_IMAGE_PATH = Path(__file__).resolve().parents[2] / "assets" / "app_image.png"


def _has_default_action() -> bool:
    """Whether the active backend fires a menu item on primary click.

    pystray renamed ``HAS_DEFAULT`` to ``HAS_DEFAULT_ACTION`` in 0.19.x;
    accept both so old and new versions keep working.
    """
    if not _PYSTRAY_OK:
        return False
    for name in ("HAS_DEFAULT_ACTION", "HAS_DEFAULT"):
        try:
            return bool(getattr(pystray.Icon, name))
        except AttributeError:
            continue
    return False


def _is_gtk_backend() -> bool:
    """Whether the active backend is the Gtk family (appindicator/gtk).

    These backends register their icon/menu updates as GLib idle
    callbacks but ``run_detached()`` never starts a GLib mainloop, so
    under tkinter's mainloop the indicator is never shown (no
    set_icon/set_menu/set_status calls at all).  The other backends
    (win32, xorg, darwin) start their own event loop in
    ``run_detached()`` and must keep using it.
    """
    return "pystray._appindicator" in pystray.Icon.__module__ or "pystray._gtk" in pystray.Icon.__module__


def _is_x11_tray_backend() -> bool:
    """Whether the active backend docks into an X11 system tray (xembed).

    The gtk StatusIcon and xorg backends need an X11 system-tray host to
    display the icon.  The appindicator backend instead speaks
    StatusNotifierItem over DBus -- the only mechanism that works in a
    Wayland session, where no xembed tray host exists.  Only consulted
    on Wayland sessions; see :meth:`TrayService.__init__`.
    """
    if not _PYSTRAY_OK:
        return False
    module = pystray.Icon.__module__
    return "pystray._gtk" in module or "pystray._xorg" in module


class TrayService:
    """System tray icon (pystray).  Not available -> .available is False.

    Callbacks fire on the tray backend thread - the caller must marshal
    them to the tkinter main thread via root.after(0, ...).
    """

    def __init__(self, title="PlaylistManager", image=None):
        self._icon = None
        if not _PYSTRAY_OK:
            logger.debug("pystray not installed - tray disabled")
            return
        try:
            from PIL import Image
            if image is None:
                # copy() decodes the image so the file can be closed
                # without leaving pystray holding an open handle.
                with Image.open(APP_IMAGE_PATH) as img_file:
                    image = img_file.copy()
            self._icon = pystray.Icon("playlistmanager", image, title, menu=None)
        except Exception as e:
            logger.warning("Tray unavailable: %s", e)
            self._icon = None
            return
        if _is_x11_tray_backend() and is_wayland_session():
            # The gtk StatusIcon and xorg backends dock into the X11
            # system tray (xembed).  A Wayland session has no such host,
            # so the icon would silently never appear -- yet .available
            # would stay True and the Settings checkbox would falsely
            # enable hide-to-tray.  Degrade to unavailable instead.
            # The appindicator backend (StatusNotifierItem over DBus) is
            # the only one that works under Wayland.
            logger.warning(
                "Tray unavailable: pystray resolved to %s backend, which "
                "requires an X11 system-tray host (none exists under "
                "Wayland)",
                pystray.Icon.__module__,
            )
            self._icon = None

    @property
    def available(self) -> bool:
        return self._icon is not None

    def start(self, on_open, on_quit):
        """Build the menu and start the icon on a background thread.

        The menu adapts to the backend's capabilities:

        * ``HAS_DEFAULT_ACTION`` (win32/gtk/xorg): the "Open" item is the
          default, so a primary click opens the app.
        * ``HAS_MENU`` (everything except the xorg fallback): a Quit item
          is added; on appindicator/macOS the menu opens on any click.

        :param on_open: callable, invoked when the user opens the app.
        :param on_quit: callable, invoked when the user picks Quit.
        """
        if self._icon is None:
            return
        items = []
        items.append(
            _MenuItem(
                "Open PlaylistManager",
                lambda icon, item: on_open(),
                default=_has_default_action(),
            )
        )
        if pystray.Icon.HAS_MENU:
            items.append(_MenuItem("Quit", lambda icon, item: on_quit()))
        # The Open item is always present, so the menu is never empty.
        self._icon.menu = _Menu(*items)
        if _is_gtk_backend():
            # See _is_gtk_backend - the Gtk family needs its own running
            # GLib mainloop; run it in a daemon thread.
            threading.Thread(target=self._icon.run, daemon=True).start()
        else:
            self._icon.run_detached()

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception as e:
                logger.warning("Error stopping tray: %s", e)
