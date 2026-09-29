"""
Local URL-receiver port diagnostics.

The extension-backed platforms take the current tab's URL from a loopback
HTTP server whose port is pinned in ``plugin.json`` (``"receiver_port"``)
and mirrored in the browser extension's ``host_permissions`` - four sites
that must agree, per docs/plugins.md "URL receiver protocol".

When that port is already taken the receiver's ``start()`` raises
``OSError(EADDRINUSE)``.  Every flow currently swallows it in
``_start_receiver`` and then waits out the whole capture timeout, so the
user is told "the extension never sent anything" about a problem that is
really a port collision - with no mention of the port, the process holding
it, or what to do.

This module answers "can the receiver bind?" *before* the flow starts and
returns a diagnostic naming the port and its holder.  It starts no server
and calls no platform API, and every step is best-effort: nothing here may
turn a working add into a failure, so a probe that cannot answer reads as
"no conflict" and the flow's own error path remains the backstop.  The
probe is also a race by nature - it reports what was true a moment before
the bind, so a port grabbed in between is still the flow's problem.

Scope is deliberately narrow: only ``flow_type`` ``"extension"`` plugins
are pre-flighted, because only those are guaranteed to bind the port (see
:func:`conflict_for`).  A hybrid/api flow may never start its receiver at
all, and blocking it on a busy port would abort an add that was about to
work.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
from pathlib import Path

from utils.i18n import tr

logger = logging.getLogger(__name__)

#: The host every receiver binds.  Mirrors ``PluginInfo.build_receiver``'s
#: ``kwargs.setdefault("host", "127.0.0.1")`` - a probe on any other host
#: would answer a question the receiver never asks.
RECEIVER_HOST = "127.0.0.1"

#: ``/proc/net/tcp`` state column for LISTEN.
_TCP_LISTEN = "0A"


def port_in_use(port: int, host: str = RECEIVER_HOST) -> bool:
    """True when *host*:*port* cannot be bound right now.

    Binds and immediately closes, the same way the receiver's server would.
    ``SO_REUSEADDR`` is set because that is what werkzeug sets: without it a
    socket lingering in ``TIME_WAIT`` from the app's own last run would read
    as a collision and abort an add that would have worked.

    Only ``EADDRINUSE`` counts as a collision.  ``EACCES`` on a privileged
    port is a different problem (and a real one for the receiver too), but
    calling it a port conflict would send the user hunting for the wrong
    process; anything unrecognized - including a socket that cannot even be
    created (``EMFILE``) - is logged at debug and treated as free, so a
    diagnostic probe can never be the reason an add fails.
    """
    if not isinstance(port, int) or isinstance(port, bool):
        return False
    if not 1 <= port <= 65535:
        return False
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return False
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return True
        logger.debug("Port %s probe inconclusive: %s", port, exc)
        return False
    finally:
        if sock is not None:
            sock.close()


def _listening_inodes(port: int) -> set[str]:
    """Socket inodes LISTENing on *port*, from /proc/net/tcp{,6} (Linux).

    Returns an empty set on any other platform or on a failed read: the
    holder is a diagnostic nicety, never a precondition.
    """
    inodes: set[str] = set()
    for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proc_file, "r", encoding="ascii", errors="replace") as handle:
                next(handle, None)  # header row
                for line in handle:
                    fields = line.split()
                    # local_address rem state ... inode
                    if len(fields) < 10 or fields[3] != _TCP_LISTEN:
                        continue
                    try:
                        local_port = int(fields[1].rsplit(":", 1)[1], 16)
                    except (IndexError, ValueError):
                        continue
                    if local_port == port:
                        inodes.add(fields[9])
        except OSError:
            continue
    return inodes


def _pid_holding(inodes: set[str]) -> int | None:
    """PID owning a socket in *inodes*, or None.

    Scans ``/proc/<pid>/fd`` for a ``socket:[inode]`` symlink, so another
    user's process is simply not found (its fds are not readable) rather
    than reported wrongly.  This process is included on purpose: the holder
    is sometimes us (a leftover receiver from a closed frame, or a second
    flow in the same process), and "your own app still holds the port" is
    exactly what the user needs to know.  Only runs on the diagnostic
    path, after a collision was already detected.
    """
    if not inodes:
        return None
    targets = {f"socket:[{inode}]" for inode in inodes}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"{fd_dir}/{fd}") in targets:
                    return pid
            except OSError:
                continue
    return None


def describe_port_holder(port: int) -> str | None:
    """Name the process LISTENing on *port* ("pid 4242 (firefox)"), or None.

    None means "something holds it, but not visibly to us" - another user's
    process, a kernel-visible-but-unattributed socket, or a non-Linux host.
    The caller must still say the port is taken in that case; this only
    sharpens the diagnosis when it can.
    """
    pid = _pid_holding(_listening_inodes(port))
    if pid is None:
        return None
    name = None
    try:
        name = Path(f"/proc/{pid}/comm").read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        name = None
    return f"pid {pid} ({name})" if name else f"pid {pid}"


#: ``plugin.json`` ``flow_type`` for platforms whose flow is *required* to
#: start its receiver.  ``_build_flow`` already enforces the rest of the
#: rule: an ``"extension"`` plugin without a receiver raises instead of
#: building a flow, so this value means "a bind is guaranteed".
REQUIRED_FLOW_TYPE = "extension"


def conflict_for(plugin, receiver=None) -> str | None:
    """Diagnostic when *plugin*'s receiver cannot bind its port, else None.

    *plugin* is a ``PluginInfo`` (``receiver_port`` / ``flow_type`` /
    ``display_name`` are read); *receiver* is that platform's
    already-built receiver instance, used for the one exemption that
    matters: a receiver that is already running **holds the port on our
    behalf**, so probing would report a conflict with ourselves and abort
    an add that the flow handles fine (it skips ``start()`` when
    ``is_running()``).

    Returns the translated, user-facing message - it goes to the log, the
    activity window's Errors tab and the status row.  None means "go
    ahead", and the three exemptions are deliberately biased that way: a
    missed diagnostic (today's behaviour, a timeout) beats aborting an add
    that was about to work.

    - no ``receiver_port`` declared, or no receiver to start;
    - ``flow_type`` is not ``"extension"``.  A hybrid/api flow *decides for
      itself* whether to consult the receiver (SoundCloud declares a
      receiver but reads the API directly under ``[soundcloud]
      capture_mode = api``), so a busy port does not imply a failed
      capture.  Only ``"extension"`` guarantees the bind, so only it is
      pre-flighted;
    - the receiver is already running (see above).

    Never raises.  Callers run on the main thread before the flow starts,
    where an exception would escape past the dispatch and leave the flow
    lock held; a diagnostic that cannot be produced must degrade to "no
    conflict" so the flow still gets its chance.
    """
    try:
        return _conflict_for(plugin, receiver)
    except Exception:
        logger.debug("Receiver port pre-flight failed", exc_info=True)
        return None


def _conflict_for(plugin, receiver) -> str | None:
    port = getattr(plugin, "receiver_port", None)
    if not port or receiver is None:
        return None
    if getattr(plugin, "flow_type", None) != REQUIRED_FLOW_TYPE:
        return None
    if _receiver_is_running(receiver):
        return None
    if not port_in_use(port):
        return None

    holder = describe_port_holder(port)
    hint = tr("receiver.port_busy_holder", holder=holder) if holder else ""
    platform = getattr(plugin, "display_name", None) or getattr(plugin, "id", "")
    return (
        tr("receiver.port_busy", port=port, platform=platform, hint=hint)
        + " "
        + tr("receiver.port_busy_fix")
    )


def _receiver_is_running(receiver) -> bool:
    """``receiver.is_running()``, defaulting to "no" if it cannot answer.

    A receiver without the method is treated as not running, which is the
    safe direction: the caller then probes and, on a genuine collision,
    reports it.  A false "not running" on a receiver that *is* listening
    costs one pointless probe, never a wrong verdict on a free port.
    """
    try:
        return bool(receiver.is_running())
    except Exception:
        return False
