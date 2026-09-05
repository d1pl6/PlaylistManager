"""
Plugin discovery for platform integrations.

Each supported music platform lives in its own top-level package under
``<repo>/integrations/<directory>/`` with a ``plugin.json`` manifest
(the ``id`` inside is the data key):

    {
      "id": "youtube_music",
      "display_name": "YouTube Music",
      "auth_file": "browser.json",
      "auth_file_fallbacks": ["browser.json"],
      "integration_class": "YouTubeMusicIntegration",
      "flow_type": "extension",
      "flow_module": "flow",
      "flow_class": "YouTubeMusicFlow",
      "receiver_module": "youtube_music_receiver",
      "receiver_class": "URLReceiverManager",
      "receiver_port": 5000,
      "url_hosts": ["music.youtube.com", ...],
      "playlist_url_template": "https://{host}/playlist?list={id}",
      "song_url_template": "https://{host}/watch?v={id}"
    }

``auth_file_fallbacks`` (optional) lists additional lookup locations for
the credential file, each relative to the repo root (legacy fallbacks
outside the platformdirs auth dir).  ``PluginInfo.auth_paths`` returns the
auth-dir file plus all fallbacks, so logout / uninstall can delete every
path that could hold the credentials.

Discovery scans the directory tree and reads the manifests. It never
imports plugin Python code at scan time - every class reference below is
resolved lazily on first use (see :class:`PluginInfo`), so an optional
dependency missing from one plugin cannot break startup or the other
plugins.

Adding a platform = create the package, drop in a plugin.json, restart.
The core app contains no platform-specific code besides what each
plugin.json declares.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Ids are stable data keys (registry entries, keybinds, local DB paths),
# so they must be plain lowercase identifiers - but they do NOT become
# Python packages; imports use the plugin's directory name (see
# PluginInfo._import).
_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_VALID_FLOW_TYPES = {"extension", "api", ""}


def _repo_root() -> Path:
    """Directory that holds both ``app/`` and ``integrations/``."""
    return Path(__file__).resolve().parents[1]


def _ensure_repo_root_on_path() -> None:
    """Make ``import integrations.<id>...`` work under every launch mode.

    ``python app/main.py`` (the legacy launcher) puts only ``app/`` on
    sys.path; without this the top-level ``integrations`` package cannot
    resolve there. Keeping the guarantee next to the only consumer means
    entry points don't each have to remember it. No-op for paths already
    present.

    Plugin modules import app code by bare module name (``services.*``,
    ``utils.*``), so ``app/`` must stay importable for them too; both
    roots go onto sys.path.
    """
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    app_dir = str(_repo_root() / "app")
    if app_dir not in sys.path:
        sys.path.insert(1, app_dir)


@dataclass
class PluginInfo:
    """Everything the core knows about one platform - all from plugin.json."""

    id: str
    display_name: str
    directory: Path
    # Credential filename inside {platformdirs}/auth/ ("spotify.json").
    auth_file: str = ""
    # Additional lookup locations for the credential file, each relative to
    # the repo root (legacy fallbacks outside the platformdirs auth dir).
    # Deleted on logout/uninstall alongside auth_file so a stale fallback
    # copy cannot resurrect auth after the platform is removed.
    auth_file_fallbacks: List[str] = field(default_factory=list)
    # Hostnames this plugin handles for playlist URL parsing.
    url_hosts: List[str] = field(default_factory=list)
    # "extension" = add-flow needs a browser-extension URL receiver,
    # "api" = the flow reads the platform API directly, "" = declared
    # but not wired into flows yet.
    flow_type: str = ""
    integration_module: str = "integration"
    integration_class: str = ""
    # Auth-manager singleton inside the plugin package (module + attribute),
    # e.g. youtube_music.youtube_auth. Resolved lazily like every class ref.
    auth_module: str = ""
    auth_attr: str = ""
    flow_module: str = ""
    flow_class: str = ""
    receiver_module: str = ""
    receiver_class: str = ""
    # Optional login-dialog tile: a callable ``(parent, on_success)`` invoked
    # when the user clicks the platform's tile in the login dialog.  Declares
    # a module + attribute like the other lazy class refs.  When omitted the
    # login dialog falls back to its built-in handlers (the platforms
    # that shipped before the plugin system).
    login_module: str = ""
    login_class: str = ""
    # Optional logo for the login tile, relative to the plugin directory.
    login_logo: str = ""
    # TCP port the extension-type receiver binds (plugin.json
    # "receiver_port"). None = receiver module's own default applies.
    receiver_port: Optional[int] = None
    # Optional URL templates for building browseable URLs from IDs.
    # Uses {host} and {id} placeholders, e.g.
    #   "https://{host}/playlist?list={id}"
    # None/empty = fall back to the hardcoded per-platform templates in
    # playlist_url.py (backward-compatible default).
    playlist_url_template: str = ""
    song_url_template: str = ""
    # Plugin version (integer) from plugin.json.  Compared against GitHub
    # releases to decide whether an update is available.  None when the
    # manifest omits the field (no update checking for that plugin).
    version: Optional[int] = None

    def _import(self, module_name: str):
        """Import ``integrations.<dir>.<module_name>`` and cache the module.

        Keyed by the DIRECTORY name (``spotify``), not the plugin id
        (``spotify``): the two only need to agree for readability.
        """
        cache_key = f"_module_{module_name}"
        cached = self.__dict__.get(cache_key)
        if cached is not None:
            return cached
        _ensure_repo_root_on_path()
        full_name = f"integrations.{self.directory.name}.{module_name}"
        module = importlib.import_module(full_name)
        self.__dict__[cache_key] = module
        return module

    def _class(self, module_key: str, class_key: str):
        """Lazily import the class named by *class_key* from its module."""
        class_name = getattr(self, class_key)
        if not class_name:
            raise AttributeError(
                f"plugin '{self.id}': '{class_key}' not set in plugin.json"
            )
        module = self._import(getattr(self, module_key))
        return getattr(module, class_name)

    def import_integration(self):
        """Lazily import and return the integration class."""
        return self._class("integration_module", "integration_class")

    def import_auth_attr(self):
        """Return the plugin's auth-manager singleton (e.g. youtube_auth)."""
        attr_name = self.auth_attr
        if not attr_name:
            raise AttributeError(
                f"plugin '{self.id}': 'auth_attr' not set in plugin.json"
            )
        module = self._import(self.auth_module)
        return getattr(module, attr_name)

    def import_flow(self):
        """Lazily import and return the flow controller class."""
        return self._class("flow_module", "flow_class")

    def import_receiver_class(self):
        """Lazily import and return the URL-receiver class."""
        return self._class("receiver_module", "receiver_class")

    def import_login(self):
        """Lazily import and return the login-dialog handler callable.

        The handler is invoked as ``handler(parent, on_success)`` from the
        login dialog when the user clicks the platform's tile.
        """
        return self._class("login_module", "login_class")

    @property
    def login_logo_path(self) -> Optional[Path]:
        """Login-tile logo resolved against the plugin directory (if set).

        None when no ``login_logo`` is declared or the declared path
        escapes the plugin directory (refused with a warning).
        """
        if not self.login_logo:
            return None
        candidate = (self.directory / self.login_logo).resolve()
        if not candidate.is_relative_to(self.directory.resolve()):
            logger.warning(
                "plugin '%s': login_logo %r escapes the plugin directory",
                self.id, self.login_logo,
            )
            return None
        return candidate

    @property
    def logo_path(self) -> Optional[Path]:
        """Canonical plugin logo.

        Resolves the manifest-declared ``login_logo`` when set, otherwise
        the standard ``integrations/<dir>/logo.png``.  None when neither
        exists - callers fall back to a generic placeholder.
        """
        declared = self.login_logo_path
        if declared is not None and declared.is_file():
            return declared
        standard = self.directory / "logo.png"
        return standard if standard.is_file() else None

    def build_receiver(self, **kwargs):
        """Construct a fresh receiver with the manifest-declared port.

        The port lives in plugin.json ("receiver_port") - the single
        Python-side source of truth. When the manifest omits it, the
        receiver module's own default applies (kwargs pass through
        untouched). Callers may still override any constructor kwarg,
        including port, for tests.
        """
        cls = self.import_receiver_class()
        if self.receiver_port is not None:
            kwargs.setdefault("port", self.receiver_port)
        return cls(**kwargs)

    @property
    def auth_path(self) -> Optional[Path]:
        """Full path of *auth_file* inside the profile-aware auth dir, if declared."""
        if not self.auth_file:
            return None
        from pathlib import PurePosixPath  # local import: only needed here

        name = PurePosixPath(self.auth_file).name  # reject path traversal
        if name != self.auth_file:
            logger.warning(
                "plugin '%s': auth_file %r must be a bare filename",
                self.id, self.auth_file,
            )
            return None
        from services import profile_store
        return profile_store.auth_dir() / name

    @property
    def auth_paths(self) -> List[Path]:
        """Every path that may hold this plugin's credentials.

        ``auth_path`` (the platformdirs auth dir) plus each
        ``auth_file_fallbacks`` entry resolved against the repo root.
        Deliberately defensive: the fallbacks are resolved even when
        ``auth_path`` came back None (badly declared auth_file), so a
        credential cleanup cannot silently miss declared paths.
        """
        paths: List[Path] = []
        if self.auth_path is not None:
            paths.append(self.auth_path)
        root = _repo_root()
        for rel in self.auth_file_fallbacks:
            candidate = (root / rel).resolve()
            if not candidate.is_relative_to(root) or candidate == root:
                logger.warning(
                    "plugin '%s': auth_file_fallbacks entry %r escapes the "
                    "repo root - skipping credential cleanup for it",
                    self.id, rel,
                )
                continue
            paths.append(candidate)
        return paths

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"<Plugin {self.id} ({self.display_name})>"


class PluginRegistry:
    """Discovers plugins and hands out :class:`PluginInfo` by id."""

    def __init__(self):
        self._plugins: Dict[str, PluginInfo] = {}

    def discover(self, base_dir: Optional[Path] = None) -> "PluginRegistry":
        """Scan ``<base>/*/plugin.json``; never raises.

        A broken manifest skips exactly its own plugin (logged warning);
        the remaining platforms stay usable. Import errors are deferred
        to first use - see PluginInfo. A missing integrations folder is
        zero plugins, not an error.
        """
        self._plugins.clear()
        base = base_dir if base_dir is not None else self.base_dir
        if not base.is_dir():
            logger.warning("No integrations directory at %s", base)
            return self

        for manifest_path in sorted(base.glob("*/plugin.json")):
            info = self._load_manifest(manifest_path)
            if info is None:
                continue
            if info.id in self._plugins:
                logger.warning(
                    "Duplicate plugin id '%s' (%s) - keeping the first "
                    "declaration found",
                    info.id, manifest_path.parent.name,
                )
                continue
            self._plugins[info.id] = info

        logger.info(
            "Discovered plugins: %s",
            ", ".join(sorted(self._plugins)) or "(none)",
        )
        return self

    def _load_manifest(self, manifest_path: Path) -> Optional[PluginInfo]:
        """Parse one plugin.json into PluginInfo (None = skip with warning)."""
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("Skipping unreadable %s: %s", manifest_path, e)
            return None

        plugin_id = raw.get("id", "")
        if not _PLUGIN_ID_RE.match(plugin_id):
            logger.warning(
                "Skipping %s: invalid plugin id %r (must match %s)",
                manifest_path, plugin_id, _PLUGIN_ID_RE.pattern,
            )
            return None
        if not raw.get("display_name"):
            logger.warning("Skipping %s: missing display_name", manifest_path)
            return None
        if not raw.get("integration_class"):
            logger.warning(
                "Skipping %s: missing integration_class", manifest_path
            )
            return None

        # Optional class references need their module and class name
        # together; half-declared pairs would only fail later at
        # lazy-import time. (integration.py is implicit - its module key
        # defaults - so only integration_class is checked above.)
        pairs = (
            ("auth_module", "auth_attr"),
            ("flow_module", "flow_class"),
            ("receiver_module", "receiver_class"),
            ("login_module", "login_class"),
        )
        for module_key, class_key in pairs:
            if bool(raw.get(module_key)) != bool(raw.get(class_key)):
                logger.warning(
                    "Plugin '%s': %s and %s must be set together",
                    plugin_id, module_key, class_key,
                )
                return None

        flow_type = raw.get("flow_type", "")
        if flow_type not in _VALID_FLOW_TYPES:
            logger.warning(
                "Skipping plugin '%s': unknown flow_type %r "
                "(expected extension or api)", plugin_id, flow_type,
            )
            return None
        if raw.get("flow_class") and not flow_type:
            logger.warning(
                "Skipping plugin '%s': flow_class set but flow_type missing "
                "(extension or api)", plugin_id,
            )
            return None
        if flow_type == "extension" and not raw.get("receiver_class"):
            logger.warning(
                "Skipping plugin '%s': flow_type 'extension' needs a "
                "receiver_class (the flow waits on a local URL receiver)",
                plugin_id,
            )
            return None

        receiver_port = raw.get("receiver_port", None)
        if receiver_port is not None and (
            isinstance(receiver_port, bool)
            or not isinstance(receiver_port, int)
            or not (1 <= receiver_port <= 65535)
        ):
            logger.warning(
                "Skipping plugin '%s': receiver_port must be an integer "
                "in 1..65535 (got %r)", plugin_id, receiver_port,
            )
            return None

        # Version: optional integer in plugin.json, compared against
        # GitHub releases to detect available updates.
        version = raw.get("version", None)
        if version is not None and (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 0
        ):
            logger.warning(
                "Plugin '%s': version must be a non-negative integer "
                "(got %r) - ignoring", plugin_id, version,
            )
            version = None

        return PluginInfo(
            id=plugin_id,
            display_name=raw["display_name"],
            directory=manifest_path.parent,
            auth_file=raw.get("auth_file", ""),
            auth_file_fallbacks=self._validate_fallback_paths(plugin_id, raw.get("auth_file_fallbacks", [])),
            url_hosts=list(raw.get("url_hosts", [])),
            flow_type=flow_type,
            integration_module=raw.get("integration_module") or "integration",
            integration_class=raw["integration_class"],
            auth_module=raw.get("auth_module", ""),
            auth_attr=raw.get("auth_attr", ""),
            flow_module=raw.get("flow_module", ""),
            flow_class=raw.get("flow_class", ""),
            receiver_module=raw.get("receiver_module", ""),
            receiver_class=raw.get("receiver_class", ""),
            login_module=raw.get("login_module", ""),
            login_class=raw.get("login_class", ""),
            login_logo=raw.get("login_logo", ""),
            receiver_port=receiver_port,
            playlist_url_template=raw.get("playlist_url_template", ""),
            song_url_template=raw.get("song_url_template", ""),
            version=version,
        )

    def _validate_fallback_paths(self, plugin_id: str, raw: object) -> List[str]:
        """Sanitize the auth_file_fallbacks manifest value.

        Entries are repo-root-relative paths: must be plain strings, not
        absolute and without any ``..`` components.  A malformed entry is
        dropped with a warning instead of failing the whole plugin (the
        plugin-dir fallback copy is deleted anyway with the directory).
        """
        if not isinstance(raw, list):
            if raw:
                logger.warning(
                    "Plugin '%s': auth_file_fallbacks must be a list of "
                    "repo-root-relative paths - ignoring", plugin_id,
                )
            return []
        from pathlib import PurePosixPath  # local import: only needed here

        out: List[str] = []
        for entry in raw:
            if not isinstance(entry, str) or not entry:
                logger.warning(
                    "Plugin '%s': ignoring non-string auth_file_fallbacks "
                    "entry %r", plugin_id, entry,
                )
                continue
            p = PurePosixPath(entry)
            if p.is_absolute() or ".." in p.parts:
                logger.warning(
                    "Plugin '%s': auth_file_fallbacks entry %r must be a "
                    "relative path without '..' components - ignoring",
                    plugin_id, entry,
                )
                continue
            out.append(entry)
        return out

    @property
    def base_dir(self) -> Path:
        """Top-level ``integrations/`` directory scanned for plugins."""
        return _repo_root() / "integrations"

    def get(self, plugin_id: str) -> Optional[PluginInfo]:
        return self._plugins.get(plugin_id)

    def get_all(self) -> Dict[str, PluginInfo]:
        return dict(self._plugins)

    def get_platform_ids(self) -> List[str]:
        return list(self._plugins.keys())

    def unregister(self, plugin_id: str) -> None:
        """Drop one plugin from the registry (Manage dialog uninstall path).

        Also evicts the plugin's modules from ``sys.modules`` so a later
        re-download re-imports the fresh code from disk instead of
        resurrecting the old module objects through the import cache.
        Only the plugin's own submodules are purged (``integrations.<dir>.``
        prefix); the package itself stays importable so sibling plugins or
        lazy importers that hold a reference keep working.
        """
        info = self._plugins.pop(plugin_id, None)
        if info is None:
            return
        prefix = f"integrations.{info.directory.name}."
        for module_name in [m for m in sys.modules if m.startswith(prefix)]:
            try:
                del sys.modules[module_name]
            except KeyError:
                pass
        # Drop the lazy-import caches on the PluginInfo too, so a caller
        # that kept a reference cannot re-lift the old classes.
        for key in [k for k in info.__dict__ if k.startswith("_module_")]:
            info.__dict__.pop(key, None)
        logger.info(
            "Unregistered plugin '%s' (%s)", plugin_id, info.display_name
        )

    def __iter__(self):
        return iter(sorted(self._plugins.values(), key=lambda p: p.id))

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, plugin_id: object) -> bool:
        return plugin_id in self._plugins


_default_registry: Optional[PluginRegistry] = None


def get_default_registry() -> PluginRegistry:
    """Process-wide registry, discovered once.

    Callers that need a fresh scan (tests, hot-reload tooling) build their
    own ``PluginRegistry().discover()``.
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = PluginRegistry().discover()
    return _default_registry
