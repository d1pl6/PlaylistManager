"""
Shared atomic JSON file writer.

Canonical temp-file + rename used by every service that persists small
JSON state files (playlist registry, duplicate queue, scrobble ledger,
profile metadata).  Before this module existed the pattern was copy-pasted
three times with slightly different contracts (one returned ``bool``, two
swallowed errors) - the mechanic lives here exactly once, and each caller
decides how to surface a failure.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_json_write(path: Path, data: object) -> None:
    """Atomically write *data* to *path* (unique temp file + rename).

    Removable-filesystems-safe: the temp file lives in the same directory
    as *path* so the final ``os.replace`` never crosses filesystems, and
    ``mkstemp`` guarantees a unique name so concurrent processes (CLI +
    GUI) never clobber each other's temp file.  A crash mid-write leaves
    the old file intact, never a torn one.

    Raises the underlying error (``OSError`` from the filesystem, or
    ``TypeError`` from ``json.dump``) on failure - the temp file is
    cleaned up first.  Callers that cannot surface an error (best-effort
    bookkeeping) wrap this themselves.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise