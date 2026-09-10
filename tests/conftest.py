"""
Pytest bootstrap for the PlaylistManager test suite.

Two jobs:

1. Put ``app/`` on ``sys.path`` BEFORE any test module imports an app
   module, so the bare imports the app itself uses (``from services.X
   import ...``) resolve identically in tests (AGENTS.md constraint:
   mixing ``app.services.X`` and ``services.X`` creates two module
   objects and silently breaks monkeypatching).

2. Redirect profile_store's file constants to a throwaway temp tree
   before importing any app module whose import-time code binds paths
   (``services.playlist_store.playlists_json``,
   ``utils.config.SETTINGS_PATH``/``THEME_PATH``, ...).  Without this,
   merely importing those modules would touch - and on a fresh clone,
   CREATE - files in the real ``db/`` / ``cfg/``.  Per-test isolation is
   handled by per-module monkeypatches on top of this global redirect.
"""

import atexit
import os
import shutil
import sys
import tempfile

# ---------------------------------------------------------------------------
# 1. app/ on sys.path (same import resolution as the running app)
# ---------------------------------------------------------------------------
_APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

# Ensure the repo root is reachable too (plugin_loader._repo_root, and
# `import integrations.<dir>` in plugin tests that import plugin code).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(1, _REPO_ROOT)

# ---------------------------------------------------------------------------
# 2. Global sandbox for profile-rooted paths (the single, early redirect).
# ---------------------------------------------------------------------------
# A module-level temp tree survives for the whole pytest process; the
# per-test fixtures in test_profile_store.py re-point the same constants
# to per-test directerories for finer isolation.
_TEMP_ROOT = tempfile.mkdtemp(prefix="pm_test_root_")
_TEMP_DB = os.path.join(_TEMP_ROOT, "db")
_TEMP_CFG = os.path.join(_TEMP_ROOT, "cfg")
_TEMP_AUTH = os.path.join(_TEMP_ROOT, "auth")


def _redirect_profile_store() -> None:
    import services.profile_store as ps  # noqa: PLC0415  (must import AFTER sys.path setup)

    ps.ACTIVE_JSON = ps.Path(_TEMP_CFG) / "profile.json"
    ps.PROFILES_JSON = ps.Path(_TEMP_DB) / "profiles.json"
    ps._DEFAULT_DB_DIR = ps.Path(_TEMP_DB)
    ps._DEFAULT_CFG_DIR = ps.Path(_TEMP_CFG)
    ps._AUTH_ROOT = ps.Path(_TEMP_AUTH)
    ps._MIGRATE_LEGACY = False
    ps._active = ""
    ps._profiles_data = None


_redirect_profile_store()


def _redirect_thumbnail_cache() -> None:
    """Sandbox the on-disk thumbnail cache (module globals in
    utils/thumbnail.py are referenced at call time, so re-pinning the
    derived paths after import is enough).  Without this, a test that
    exercises a persistent cache mode would write to the REAL
    ``~/.cache/playlistmanager/`` tree.
    """
    import utils.thumbnail as th  # noqa: PLC0415

    th.CACHE_ROOT = th.Path(_TEMP_ROOT) / "cache"
    th._PLAYLIST_DIR = th.CACHE_ROOT / "playlists"
    th._SONG_DIR = th.CACHE_ROOT / "songs"
    th._FULL_DIR = th.CACHE_ROOT / "full"
    th._DEDUPE_INDEX = th._SONG_DIR / "index.json"
    th.DATA_SAVER = False


_redirect_thumbnail_cache()


def _cleanup_temp_root() -> None:
    shutil.rmtree(_TEMP_ROOT, ignore_errors=True)


atexit.register(_cleanup_temp_root)