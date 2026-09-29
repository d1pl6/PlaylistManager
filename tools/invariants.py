#!/usr/bin/env python3
"""Project-invariant checks: this repo's "typechecker".

A maintainer tool, not an app module -- run it from the repo root with the
interpreter directly::

    python tools/invariants.py                       # all rules, report
    python tools/invariants.py --strict              # exit 1 on any finding
    python tools/invariants.py --list-rules          # what is enforced here
    python tools/invariants.py --disable theme.key   # skip one rule
    python tools/invariants.py --disable theme.key --disable i18n.key
    python tools/invariants.py --quiet               # summary line only

Why a home-grown checker instead of a linter/typechecker: every rule below
encodes an *architectural* invariant of this codebase, not a PEP 8 or typing
convention.  ``C["..."]`` must exist in ``THEME_MAP`` (a missing key is a
``KeyError`` at widget creation), a ``plugin.json`` key must exist because the
loader silently ignores unknown ones, a plugin flow must match the signature
core dispatches against.  No off-the-shelf tool knows any of that, and the
invariants live in ``AGENTS.md`` / ``docs/`` where prose cannot be executed.

Three properties make the tool safe to run anywhere:

* **No app imports.**  Catalog values are extracted with ``ast.literal_eval``
  on the module-level assignment, so nothing here imports ``utils.theme``
  (which would bind real profile paths) and the tool needs neither a display
  nor the app's optional dependencies (``ytmusicapi``, ``keyring``,
  ``pystray``).  It runs on a bare interpreter, in a fresh clone, before
  ``pip install``.
* **Read-only.**  No rule rewrites a source file.  Findings are reported, the
  fix is a human or agent edit.
* **A broken input is one finding, not a crash.**  A file that does not
  parse, or a manifest that is not JSON, is reported against its own path
  and skipped; the other inputs are still checked.  The catalog sources and
  the contract module are the exception: those raise ``ToolError`` (exit 2),
  because a rule built on a catalog it could not read would report nonsense.

Rules come in tiers.  **Tier A** is the "typechecker": catalog keys, the
``plugin.json`` schema, cross-file port sync and the plugin contract.
**Tier B** is architecture and threading: the cross-layer import direction,
the docs/modules.md dependency map, and unguarded ``root.after()`` calls in
worker threads.  A style/doc-drift tier (unused imports, dependency
declarations) is not implemented -- see ``docs/invariants.md``.

Exemptions live in ``EXEMPTIONS`` below rather than in a config file: every
entry is ``{path: reason}``, a reason is mandatory, and an exemption that no
rule ever consumes is itself reported (``exempt.hygiene``).  The allowlist
therefore cannot rot, and every exception is justified where it is read.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

# The repo this file lives in.  Every other path is derived from a
# ``Context.root`` (see the class below), never from a module constant, so a
# test can point the whole tool at a synthetic tree.
REPO_ROOT = Path(__file__).resolve().parent.parent

SEVERITIES = ("error", "warn")
EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


class ToolError(Exception):
    """A precondition the tool cannot check without (missing/renamed source)."""


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    """One rule violation.  ``path`` is repo-relative, ``line`` 1-based."""

    rule: str
    severity: str
    message: str
    path: str = ""
    line: int = 0

    def render(self) -> str:
        where = f"{self.path}:{self.line}: " if self.line else (f"{self.path}: " if self.path else "")
        return f"{where}[{self.severity}] {self.rule}: {self.message}"

    def sort_key(self) -> tuple:
        return (self.path, self.line, self.rule, self.message)


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    summary: str
    source: str  # the documented rule this enforces
    check: Callable[["Context"], Iterable[Finding]]


REGISTRY: dict[str, Rule] = {}


def rule(rule_id: str, severity: str, summary: str, source: str):
    """Register a check.  ``severity`` is the default; --disable drops it."""

    def wrap(fn):
        if severity not in SEVERITIES:
            raise ToolError(f"rule {rule_id}: bad severity {severity!r}")
        if rule_id in REGISTRY:
            raise ToolError(f"duplicate rule id {rule_id!r}")
        REGISTRY[rule_id] = Rule(rule_id, severity, summary, source, fn)
        return fn

    return wrap


# ---------------------------------------------------------------------------
# Exemptions (path -> mandatory reason)
# ---------------------------------------------------------------------------

EXEMPTIONS: dict[str, dict[str, str]] = {
    "theme.key": {
        # utils/theme.py *defines* the C mapping: C[name] = ... inside
        # load_theme() fills it from theme.ini, so the index is a
        # short name read from a file and cannot be checked statically.
        "app/utils/theme.py": "defines and populates the C mapping itself",
    },
    "settings.key": {
        # _migrate_update_check_interval() reads the LEGACY
        # [update_check] is_true key out of an existing user config to pick
        # an interval; that key is deliberately absent from DEFAULT_SETTINGS
        # since the boolean setting became an interval enum ("delete in
        # 0.6.0"), and the migration only fires on a file that still has it.
        "app/utils/config.py": "owns DEFAULT_SETTINGS; reads the legacy [update_check] is_true key in _migrate_update_check_interval",
    },
    "contract.scrobble": {
        # ScrobbleCapable is a UNION of two unrelated capabilities and a
        # service backend only ever implements some of it.  ListenBrainz
        # scrobbles and unscrobbles, but its love/hate feedback is keyed on
        # a MusicBrainz recording MBID that this app has no metadata to
        # resolve, so love/unlove/is_loved are genuinely absent rather
        # than stubbed to a lie.  That is safe because the core no longer
        # duck-types a single "the" ScrobbleCapable: every selection goes
        # through services/scrobble.resolve_scrobble_backend(), which asks
        # for the capabilities the call site needs - so an absent method
        # makes ListenBrainz ineligible for the like button (which keeps
        # resolving to Last.fm) instead of blowing up at click time.
        # NOTE: integrations/ is gitignored, so a checkout without the
        # plugin dir reports this exemption as unused (a warning).
        "integrations/listenbrainz/integration.py": "implements the scrobble half of ScrobbleCapable; love/hate feedback needs a MusicBrainz recording MBID this app cannot resolve, and the core selects per capability (services/scrobble.py)",
    },
    "layer.import": {
        # The three utils -> services edges that exist today, all of them
        # listed in docs/modules.md "Cross-layer exceptions".  An exemption
        # is keyed by path, so it covers every upward edge from the file, not
        # just the one named here: a SECOND edge added to any of these files is
        # invisible to the rule.  The reason names the exact modules, so the
        # next reader can tell an expected edge from a new one.
        "app/utils/config.py": "documented exception 4: imports services.profile_store on startup to call initialize(), so every path constant below it binds an initialised profile; profile_store is stdlib+platformdirs and cannot cycle",
        "app/utils/i18n.py": "same shape as utils/config.py: I18N_DIR = _profile_store.cfg_dir() / 'i18n' is a module-level constant, so the import cannot be made lazy without moving the path into a function",
        "app/utils/thumbnail.py": "two function-level lazy edges, marked '(lazy)' in its modules.md row: services.duplicate_check (shared add-path matcher, so a thumb identity cannot disagree with the duplicate checker) and services.song_manager (warm_song_cache), both needed to break an import cycle at module load",
    },
}


# ---------------------------------------------------------------------------
# Static extraction helpers
# ---------------------------------------------------------------------------

def app_sources(app_dir: Path) -> list[Path]:
    """Every ``*.py`` under *app_dir*, sorted (a rule's scan universe)."""
    return sorted(app_dir.rglob("*.py"))


def module_dict(path: Path, name: str) -> dict:
    """The module-level literal dict *name* from *path*, without importing it.

    Raises :class:`ToolError` when the assignment is missing or is no longer a
    literal -- a silent skip there would turn a renamed constant into a
    checker that checks nothing.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise ToolError(f"{relative(path)}: {exc}") from exc
    for node in tree.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            value = node.value
        if value is None:
            continue
        try:
            result = ast.literal_eval(value)
        except (ValueError, SyntaxError, TypeError) as exc:
            raise ToolError(
                f"{relative(path)}: {name} is no longer a literal "
                f"assignment ({exc.__class__.__name__}) -- update tools/invariants.py"
            ) from exc
        if not isinstance(result, dict):
            raise ToolError(f"{relative(path)}: {name} is not a dict")
        return result
    raise ToolError(
        f"{relative(path)}: no module-level {name} assignment -- update tools/invariants.py"
    )


def relative(path: Path) -> str:
    """Repo-relative posix path, for stable output and exemption keys."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def string_arg(node: ast.Call, index: int) -> str | None:
    """Argument *index* of *node* when it is a plain string literal."""
    if len(node.args) <= index:
        return None
    arg = node.args[index]
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def iter_calls(tree: ast.AST, names: set[str]) -> Iterable[ast.Call]:
    """Every call whose callee is a bare *name* in *names* (or ``i18n.<name>``).

    App modules import ``tr``/``trn``/``get_setting`` unqualified, but
    ``app/app.py`` reaches them through the module object (``i18n.tr(...)``),
    so both spellings are matched -- and only these, to avoid flagging an
    unrelated object's ``.get``/``.tr`` method.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in names:
            yield node
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in names
            and isinstance(func.value, ast.Name)
            and func.value.id == "i18n"
        ):
            yield node


# `except*` (3.11+) is the same shape for this purpose: a block guarded by
# either one is guarded against a raised TclError/RuntimeError.
TRY_NODES: tuple = (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)


def is_type_checking(test: ast.expr) -> bool:
    """True for ``if TYPE_CHECKING:`` in any of the three spellings."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def type_checking_ids(tree: ast.AST) -> set[int]:
    """Ids of every node guarded by an ``if TYPE_CHECKING:`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and is_type_checking(node.test):
            for child in node.body:
                guarded.update(id(n) for n in ast.walk(child))
    return guarded


_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")


def import_binding(alias: ast.alias) -> str | None:
    """The local name an import alias binds, or None for ``import *``."""
    if alias.name == "*":
        return None
    return alias.asname or alias.name.split(".")[0]


def referenced_names(tree: ast.AST) -> set[str]:
    """Every name the module *reads*.

    Three passes, because an import can be used in ways a plain
    ``ast.Name`` walk does not see:

    - the read itself -- ``json.loads`` puts ``json`` here as the ``Name``
      the ``Attribute`` hangs off, and ``import a.b`` binds ``a`` with no
      node at all.  Only ``Load`` context counts; a name that is merely
      assigned is not a use, and :func:`rebound_elsewhere` deals with it;
    - ``__all__``, the explicit statement that a name is exported;
    - identifiers inside a *string annotation* (``-> "MainWindow"``), which
      is a ``Constant`` and would otherwise look unused.  Only annotation
      position counts: scanning every string would let a docstring or an
      i18n key like ``"notif.scan_found"`` mask a genuinely dead import.
    """
    names = {
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    annotations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                names.update(
                    e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
    for root in annotations:
        for node in ast.walk(root):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.update(_IDENTIFIER_RE.findall(node.value))
    return names


def rebound_elsewhere(tree: ast.AST, name: str, import_node: ast.AST) -> bool:
    """True when *name* is bound somewhere other than *import_node*.

    A local that shadows a module-level import -- ``for os in paths`` -- or a
    second import of the same name makes "is *this* import read?" a question
    a name-based scan cannot answer.  The honest answer is "ask a human", so
    the rule says nothing.  ``referenced_names`` has already ruled out a
    genuine read, so anything here is a rebinding.
    """
    for node in ast.walk(tree):
        if node is import_node:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Name) and node.id == name:
            return True  # Store or Del: a binding, never a read
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(import_binding(a) == name for a in node.names):
                return True
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            return True
    return False


def thread_target_names(tree: ast.AST) -> set[str]:
    """Names of the functions handed to ``threading.Thread(target=...)``.

    ``Thread`` is matched as an attribute call rather than by resolving the
    object, so ``threading.Thread`` and a bare ``Thread`` both count.

    A ``target=self._method`` attribute is tracked by its bare name -- the
    dominant style in ``app/ui/`` -- which means a same-named method of a
    different class in the same file is checked too.  That is the same
    bias-towards-reporting the duplicate-name case takes, and a lambda target
    is still not tracked: there is no name to report.
    """
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Thread"):
            continue
        for keyword in node.keywords:
            if keyword.arg != "target":
                continue
            if isinstance(keyword.value, ast.Name):
                targets.add(keyword.value.id)
            elif isinstance(keyword.value, ast.Attribute):
                targets.add(keyword.value.attr)
        # Thread(group=None, target=None, ...) -- the *second* positional is
        # the target.  Thread(work) passes work as `group` and raises at
        # start, so reading args[0] as a target would be wrong, not merely
        # incomplete.
        if len(node.args) > 1 and isinstance(node.args[1], ast.Name):
            targets.add(node.args[1].id)
    return targets


def guarded_call_ids(tree: ast.AST) -> set[int]:
    """Ids of every call inside a ``try:`` block, i.e. behind an except handler."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, TRY_NODES):
            continue
        # body + handlers + else: any of them means a raise there is caught.
        # finalbody is included too -- being generous here costs a missed
        # finding, a false one would send someone chasing a real guard.
        for block in (node.body, node.orelse, node.finalbody):
            for stmt in block:
                guarded.update(id(n) for n in ast.walk(stmt))
    return guarded


def package_marker(tree: ast.Module) -> bool:
    """True for a ``__init__.py`` that only holds a docstring.

    The four layer packages are namespace markers, not modules: nothing to
    document, so they are not expected in modules.md.  ``app/__init__.py``
    re-exports ``__version__`` and is.
    """
    for stmt in tree.body:
        if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)):
            continue  # module docstring
        return False
    return True


# ---------------------------------------------------------------------------
# plugin.json schema (the enforced contract)
# ---------------------------------------------------------------------------

# key -> value kind.  Kinds are validated in _check_manifest_types.
MANIFEST_FIELDS: dict[str, str] = {
    "id": "str",
    "display_name": "str",
    "integration_class": "str",
    "flow_type": "str",
    "flow_module": "str",
    "flow_class": "str",
    "auth_module": "str",
    "auth_attr": "str",
    "auth_file": "str",
    "auth_file_fallbacks": "str_list",
    "login_module": "str",
    "login_class": "str",
    "login_logo": "str",
    "receiver_module": "str",
    "receiver_class": "str",
    "receiver_port": "port",
    "url_hosts": "str_list",
    "playlist_url_template": "template",
    "song_url_template": "template",
    "version": "version",
}

# Unconditionally required.  The flow/receiver/auth/login groups are
# conditional and enforced by MANIFEST_GROUPS + _check_manifest_relations.
MANIFEST_REQUIRED: tuple[str, ...] = ("id", "display_name", "integration_class")

# Keys that are only meaningful together: declaring one declares the group.
MANIFEST_GROUPS: dict[str, tuple[str, ...]] = {
    "flow": ("flow_type", "flow_module", "flow_class"),
    "auth": ("auth_module", "auth_attr"),
    "receiver": ("receiver_module", "receiver_class"),
    "login": ("login_module", "login_class"),
}

FLOW_TYPES = ("api", "extension")
TEMPLATE_PLACEHOLDERS = ("host", "id")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

# The core classes a plugin implements, in app/services/integration.py.
CONTRACT_CLASSES = ("BaseIntegration", "ScrobbleCapable", "BaseFlowController")
# BaseIntegration methods a service-only platform may omit.
PLAYLIST_METHODS = (
    "get_library_playlists", "get_playlist_details", "get_playlist_id",
    "get_playlist_tracks", "remove_track",
)


# ---------------------------------------------------------------------------
# Layer direction (docs/README.md "Layers")
# ---------------------------------------------------------------------------

# Depth index of the layer packages, DEEPEST LAST.  An import from a module
# at a HIGHER index into one at a LOWER index points upward, which is what
# docs/README.md forbids ("utils imports nothing from the app packages").
# "controllers" and "ui" share a rank because the diagram draws them as
# siblings and modules.md records a controllers -> ui edge
# (app_controller delegating the dialogs) as lateral, not upward; a
# services -> ui or services -> controllers import *is* upward and is caught.
APP_LAYERS: dict[str, int] = {
    "controllers": 1,
    "ui": 1,
    "services": 2,
    "utils": 3,
}
LAYER_DIRS: tuple[str, ...] = tuple(APP_LAYERS)

# app/'s top level is not one layer.  The entry points and composition roots
# (__main__.py, app.py, main.py, cli.py) sit above everything -- they are the
# only modules allowed to reach into every layer -- while the rest
# (plugin_loader.py, _version.py) are shared leaves the diagram draws at or
# below utils/.  A module with no explicit rank is treated as a leaf, so a new
# top-level module can be imported from anywhere without tripping the rule.
APP_ROOTS: frozenset[str] = frozenset({"__main__", "app", "main", "cli"})
LEAF_RANK: int = max(APP_LAYERS.values())


# docs/modules.md section heading -> the app/ subdirectory its rows describe
# ("" for the top-level app modules, whose rows spell out "app/..." themselves).
DOC_SECTIONS: dict[str, str] = {
    "Entry points and bootstrap": "",
    "controllers/": "controllers",
    "services/": "services",
    "ui/": "ui",
    "utils/": "utils",
}

# Every modules.md table above has these four columns: File / Role / Used by /
# Uses.  A body row that does not split into four cells has an unescaped "|"
# in a cell, which breaks the table for every renderer AND makes the "Uses"
# column unreadable to this tool -- so it is a finding, not a shrug.
DOC_COLUMNS = 4

# Split on the *unescaped* pipe only: markdown lets a cell contain a literal
# pipe as "\|", including inside a code span, and this tool has to read the row
# the same way a renderer does or it will call a correct table broken.
_TABLE_SPLIT_RE = re.compile(r"(?<!\\)\|")


def split_table_row(line: str) -> list[str]:
    """The cells of a markdown table row, with ``\\|`` unescaped back to ``|``."""
    return [c.strip().replace("\\|", "|")
            for c in _TABLE_SPLIT_RE.split(line.strip().strip("|"))]


def _check_manifest_types(manifest: dict, path: str) -> Iterable[Finding]:
    """Type-check every declared key; report unknown keys (the loader drops
    them silently, so a typo is a no-op nobody notices)."""
    for key in sorted(set(manifest) - set(MANIFEST_FIELDS)):
        yield Finding("manifest.schema", "error",
                      f"unknown manifest key {key!r} -- the loader ignores it; "
                      f"known keys: {', '.join(sorted(MANIFEST_FIELDS))}", path)
    for key in sorted(set(manifest) & set(MANIFEST_FIELDS)):
        value = manifest[key]
        kind = MANIFEST_FIELDS[key]
        if kind == "str" and not isinstance(value, str):
            yield Finding("manifest.schema", "error",
                          f"{key} must be a string, got {type(value).__name__}", path)
        elif kind == "str_list":
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                yield Finding("manifest.schema", "error",
                              f"{key} must be a non-empty list of non-empty strings, "
                              f"got {value!r}", path)
        elif kind == "port":
            # bool is an int subclass; the loader rejects it explicitly, so a
            # true/false here is a typo, not a port.
            if isinstance(value, bool) or not isinstance(value, int):
                yield Finding("manifest.schema", "error",
                              f"{key} must be an integer, got {value!r}", path)
            elif not 1 <= value <= 65535:
                yield Finding("manifest.schema", "error",
                              f"{key} must be in 1..65535, got {value}", path)
        elif kind == "version":
            if isinstance(value, bool) or not isinstance(value, int):
                yield Finding("manifest.schema", "error",
                              f"{key} must be an integer, got {value!r}", path)
            elif value < 0:
                yield Finding("manifest.schema", "error",
                              f"{key} must be non-negative, got {value}", path)
        elif kind == "template":
            if not isinstance(value, str):
                yield Finding("manifest.schema", "error",
                              f"{key} must be a string, got {type(value).__name__}", path)
                continue
            names = _PLACEHOLDER_RE.findall(value)
            unknown = sorted({n for n in names if n not in TEMPLATE_PLACEHOLDERS})
            if unknown:
                yield Finding("manifest.schema", "error",
                              f"{key} has unknown placeholder(s) {', '.join(unknown)}; "
                              f"only {TEMPLATE_PLACEHOLDERS} are substituted", path)
            if "id" not in names:
                yield Finding("manifest.schema", "error",
                              f"{key} has no {{id}} placeholder, so a playlist/song "
                              f"URL can never be built from it", path)


def _check_manifest_relations(
    manifest: dict, plugin_dir: str, path: str, logo: bool
) -> Iterable[Finding]:
    """Cross-field rules: group completeness, flow_type, id/dir, URL shapes."""
    for name, keys in MANIFEST_GROUPS.items():
        present = [k for k in keys if k in manifest]
        if present and len(present) != len(keys):
            missing = [k for k in keys if k not in manifest]
            yield Finding("manifest.schema", "error",
                          f"incomplete {name!r} group: {', '.join(present)} declared but "
                          f"{', '.join(missing)} missing -- every key of a group is read "
                          f"together", path)

    flow_type = manifest.get("flow_type")
    if flow_type is not None and flow_type not in FLOW_TYPES:
        yield Finding("manifest.schema", "error",
                      f"flow_type must be one of {FLOW_TYPES}, got {flow_type!r}", path)
    if flow_type == "extension" and "receiver_class" not in manifest:
        # keybind_controller builds the receiver only when receiver_class is
        # declared, so an extension flow without one raises RuntimeError on
        # the first keybind instead of ever capturing a song.
        yield Finding("manifest.schema", "error",
                      "flow_type 'extension' requires receiver_class (the controller "
                      "builds no receiver without it, then raises at first keybind)", path)
    if "receiver_port" in manifest and "receiver_class" not in manifest:
        yield Finding("manifest.schema", "warn",
                      "receiver_port declared without receiver_class -- the port is "
                      "never bound, so the setting is dead", path)

    plugin_id = manifest.get("id")
    if isinstance(plugin_id, str) and plugin_id and plugin_id != plugin_dir:
        # Imports key on the DIRECTORY name, not on id (docs/plugins.md).
        yield Finding("manifest.schema", "error",
                      f"id {plugin_id!r} != directory {plugin_dir!r} -- the loader "
                      f"imports integrations.{plugin_dir}.*, so the two must agree", path)

    auth_file = manifest.get("auth_file")
    if isinstance(auth_file, str) and auth_file:
        if auth_file in (".", "..") or "/" in auth_file or "\\" in auth_file:
            yield Finding("manifest.schema", "error",
                          f"auth_file {auth_file!r} must be a bare filename inside the "
                          f"profile auth dir (the loader rejects path separators)", path)
    for rel in manifest.get("auth_file_fallbacks", []) or []:
        if not isinstance(rel, str):
            continue
        if rel.startswith("/") or ".." in Path(rel).parts:
            yield Finding("manifest.schema", "error",
                          f"auth_file_fallbacks entry {rel!r} escapes the repo root -- "
                          f"the loader drops it, so the stale credential survives logout", path)

    for host in manifest.get("url_hosts", []) or []:
        if isinstance(host, str) and ("://" in host or "/" in host):
            yield Finding("manifest.schema", "error",
                          f"url_hosts entry {host!r} must be a bare host "
                          f"(no scheme, no path)", path)

    if not logo and "login_logo" not in manifest:
        yield Finding("manifest.schema", "warn",
                      "no logo.png and no login_logo -- the login tile falls back to the "
                      "generic placeholder", path)


# ---------------------------------------------------------------------------
# Plugin contract (core base classes <-> plugin subclasses)
# ---------------------------------------------------------------------------

class Param(NamedTuple):
    name: str
    kind: str  # posonly | pos_or_kw | kwonly | vararg | kwarg
    has_default: bool


class Method(NamedTuple):
    name: str
    params: tuple[Param, ...]


def _method_params(fn: ast.FunctionDef) -> tuple[Param, ...]:
    """A function's parameters, with the kind and whether it has a default.

    ``kw_defaults`` runs parallel to ``kwonlyargs`` with ``None`` for an
    argument that has no default, so the two are zipped rather than searched.
    """
    a = fn.args
    posonly = list(getattr(a, "posonlyargs", []))
    positional = posonly + list(a.args)
    first_defaulted = len(positional) - len(a.defaults)
    params = [
        Param(p.arg, "posonly" if i < len(posonly) else "pos_or_kw", i >= first_defaulted)
        for i, p in enumerate(positional)
    ]
    if a.vararg:
        params.append(Param(a.vararg.arg, "vararg", False))
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        params.append(Param(arg.arg, "kwonly", default is not None))
    if a.kwarg:
        params.append(Param(a.kwarg.arg, "kwarg", False))
    return tuple(params)


def class_methods(node: ast.ClassDef) -> dict[str, Method]:
    return {
        m.name: Method(m.name, _method_params(m))
        for m in node.body
        if isinstance(m, ast.FunctionDef)
    }


def base_name(base: ast.expr) -> str:
    return base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")


def class_supports_playlists(node: ast.ClassDef) -> bool:
    """The class's own ``supports_playlists`` flag; the base default is True."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if getattr(target, "id", None) == "supports_playlists":
                    return bool(ast.literal_eval(stmt.value))
        elif isinstance(stmt, ast.AnnAssign) and getattr(stmt.target, "id", None) == "supports_playlists":
            return bool(ast.literal_eval(stmt.value))
    return True


def signature_problems(base: Method, impl: Method, positional: int | None = None) -> list[str]:
    """Why *impl* cannot stand in for *base* at a core dispatch site.

    *positional* is how many leading parameters a core call site actually
    passes positionally; only those are order-sensitive.  Everything past
    that is passed by keyword in the app, so an implementation may reorder it
    freely (all four shipped flow controllers do).
    """
    problems: list[str] = []
    base_named = [p for p in base.params if p.kind in ("posonly", "pos_or_kw", "kwonly")]
    impl_named = [p for p in impl.params if p.kind in ("posonly", "pos_or_kw", "kwonly")]
    impl_names = {p.name for p in impl_named}
    has_var_kw = any(p.kind == "kwarg" for p in impl.params)

    for p in base_named:
        if p.name not in impl_names and not has_var_kw:
            problems.append(f"drops parameter {p.name!r}")

    # A new parameter the core will never pass breaks every call site -- unless
    # it is optional.
    for p in impl_named:
        if p.name not in {b.name for b in base_named} and not p.has_default and not has_var_kw:
            problems.append(f"adds required parameter {p.name!r}")

    # Reordering an order-sensitive parameter silently changes what a
    # positional call binds to.
    if positional is None:
        positional = len(base_named)
    sensitive = {b.name for b in base_named[:positional]}
    base_order = [p.name for p in base_named if p.name in sensitive]
    impl_order = [p.name for p in impl_named if p.name in sensitive]
    if impl_order != base_order:
        problems.append(f"reorders positionally-dispatched parameters: "
                        f"base {base_order} vs impl {impl_order}")
    return problems


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

@dataclass
class Context:
    """Everything a rule may read, built once from a repo root.

    Every path is derived from ``root`` (defaulting to this file's repo at
    *load* time, not at class-definition time), so a test can point the whole
    tool at a synthetic tree in ``tmp_path`` and assert on real findings.
    """

    root: Path | None = None
    sources: list[Path] = field(default_factory=list)
    theme_map: dict = field(default_factory=dict)
    default_strings: dict = field(default_factory=dict)
    default_settings: dict = field(default_factory=dict)
    manifests: list[tuple[str, dict, Path]] = field(default_factory=list)
    contract: dict[str, dict[str, Method]] = field(default_factory=dict)
    contract_capability: dict[str, tuple[str, ...]] = field(default_factory=dict)
    positional_dispatch: dict[str, int] = field(default_factory=dict)
    module_map: dict[str, str] = field(default_factory=dict)
    module_map_broken: list[tuple[int, str]] = field(default_factory=list)
    used_exemptions: set[tuple[str, str]] = field(default_factory=set)
    unparseable: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def parse(self, path: Path) -> ast.Module | None:
        """Parse *path* for a scanning rule, or record why it could not be.

        A file that does not compile must not take the whole run down: the
        finding is reported against that one path, and every other file is
        still checked.  (An uncaught SyntaxError would exit 1, which is the
        same code as "findings".)
        """
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self.unparseable[self.rel(path)] = f"unreadable ({exc.__class__.__name__})"
            return None
        try:
            return ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            self.unparseable[self.rel(path)] = f"does not parse ({exc})"
            return None

    # -- derived paths --------------------------------------------------
    @property
    def app_dir(self) -> Path:
        return self.root / "app"

    @property
    def integrations_dir(self) -> Path:
        return self.root / "integrations"

    @property
    def contract_module(self) -> Path:
        return self.app_dir / "services" / "integration.py"

    @property
    def theme_source(self) -> Path:
        return self.app_dir / "utils" / "theme.py"

    @property
    def i18n_source(self) -> Path:
        return self.app_dir / "utils" / "i18n.py"

    @property
    def config_source(self) -> Path:
        return self.app_dir / "utils" / "config.py"

    @property
    def schema_doc(self) -> Path:
        return self.root / "docs" / "plugins.md"

    @property
    def module_map_doc(self) -> Path:
        return self.root / "docs" / "modules.md"

    def load(self) -> None:
        if self.root is None:
            self.root = REPO_ROOT
        for path, name in ((self.theme_source, "THEME_MAP"),
                           (self.i18n_source, "DEFAULT_STRINGS"),
                           (self.config_source, "DEFAULT_SETTINGS")):
            if not path.exists():
                raise ToolError(f"{self.rel(path)} is missing -- cannot check {name}")
        self.sources = app_sources(self.app_dir)
        self.theme_map = module_dict(self.theme_source, "THEME_MAP")
        self.default_strings = module_dict(self.i18n_source, "DEFAULT_STRINGS")
        self.default_settings = module_dict(self.config_source, "DEFAULT_SETTINGS")
        self._load_contract()
        self._scan_dispatch()
        self._load_manifests()
        self._load_module_map()

    # -- how the core dispatches a contract method ----------------------
    def _scan_dispatch(self) -> None:
        """Record how many arguments each ``<contract method>(...)`` call passes
        positionally, so the signature check only treats those as
        order-sensitive.

        ``self`` is excluded: a call's first positional argument binds to the
        base's second parameter.
        """
        for path in self.sources:
            tree = self.parse(path)
            if tree is None:
                continue  # reported per-file by the scanning rules
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                name = node.func.attr
                if name not in {m for methods in self.contract.values() for m in methods}:
                    continue
                count = len(node.args)
                if count > self.positional_dispatch.get(name, 0):
                    self.positional_dispatch[name] = count

    def positional_count(self, method: Method) -> int:
        """Order-sensitive prefix length of *method* as the core calls it."""
        named = [p for p in method.params if p.kind in ("posonly", "pos_or_kw", "kwonly")]
        if named and named[0].name in ("self", "cls"):
            named = named[1:]
        return min(self.positional_dispatch.get(method.name, 0), len(named))

    # -- contract base classes ------------------------------------------
    def _load_contract(self) -> None:
        if not self.contract_module.exists():
            raise ToolError(f"{self.rel(self.contract_module)} is missing")
        try:
            tree = ast.parse(self.contract_module.read_text(encoding="utf-8"),
                             filename=str(self.contract_module))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            # Fatal rather than a finding: with no contract in hand, every
            # contract rule would report nonsense.
            raise ToolError(f"{self.rel(self.contract_module)} {exc}") from exc
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name in CONTRACT_CLASSES:
                self.contract[node.name] = class_methods(node)
        missing = set(CONTRACT_CLASSES) - set(self.contract)
        if missing:
            raise ToolError(
                f"{self.rel(self.contract_module)}: contract class(es) {sorted(missing)} "
                f"renamed or removed -- update tools/invariants.py"
            )
        # Methods a service-only platform (supports_playlists = False) may omit.
        self.contract_capability["BaseIntegration"] = PLAYLIST_METHODS

    # -- manifests ------------------------------------------------------
    def _load_manifests(self) -> None:
        if not self.integrations_dir.is_dir():
            self.notes.append("integrations/ not present -- plugin rules skipped")
            return
        dirs = sorted(
            p for p in self.integrations_dir.iterdir()
            if p.is_dir() and not p.name.startswith("_")
        )
        if not dirs:
            self.notes.append("integrations/ holds no plugin dirs -- plugin rules skipped")
            return
        for plugin_dir in dirs:
            manifest_path = plugin_dir / "plugin.json"
            if not manifest_path.exists():
                self.notes.append(f"{self.rel(manifest_path.parent)}: no plugin.json, skipped")
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                self.manifests.append((plugin_dir.name, {}, manifest_path))
                self.notes.append(f"{self.rel(manifest_path)}: unreadable ({exc.__class__.__name__})")
                continue
            except json.JSONDecodeError as exc:
                self.manifests.append((plugin_dir.name, {}, manifest_path))
                self.notes.append(f"{self.rel(manifest_path)}: invalid JSON ({exc})")
                continue
            if not isinstance(manifest, dict):
                self.manifests.append((plugin_dir.name, {}, manifest_path))
                continue
            self.manifests.append((plugin_dir.name, manifest, manifest_path))

    def plugin_dir_path(self, name: str) -> Path:
        return self.integrations_dir / name

    def extension_manifest(self, plugin_dir: str) -> Path | None:
        """The browser extension's manifest.json, if the plugin ships one."""
        direct = self.integrations_dir / plugin_dir / f"{plugin_dir}-extension" / "manifest.json"
        if direct.exists():
            return direct
        candidates = sorted(
            self.integrations_dir.glob(f"{plugin_dir}/*-extension/manifest.json")
        )
        return candidates[0] if candidates else None

    # -- the docs/modules.md dependency map -----------------------------
    def _load_module_map(self) -> None:
        """Parse the "Uses" cell of every app module row in docs/modules.md.

        Fatal (ToolError, exit 2) rather than a finding: a rule built on a map
        it could not read would report every module as undocumented, which is
        the exact failure mode the tool exists to prevent.  A *row* that does
        not split into DOC_COLUMNS cells is not fatal -- that is a real defect
        in the doc, recorded in ``module_map_broken`` and reported by
        ``modules.doc``.
        """
        path = self.module_map_doc
        if not path.exists():
            raise ToolError(f"{self.rel(path)} is missing -- cannot check the module map")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolError(f"{self.rel(path)}: unreadable ({exc})") from exc
        section = None
        seen_sections: set[str] = set()
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.startswith("## "):
                section = line[3:].strip()
                if section in DOC_SECTIONS:
                    seen_sections.add(section)
                continue
            if section not in DOC_SECTIONS or not line.startswith("| `"):
                continue
            cells = split_table_row(line)
            if len(cells) != DOC_COLUMNS:
                self.module_map_broken.append((lineno, cells[0] if cells else line))
                continue
            directory = DOC_SECTIONS[section]
            name = cells[0].strip("`")
            key = f"app/{directory}/{name}" if directory else name
            self.module_map[key] = cells[3]
        missing = set(DOC_SECTIONS) - seen_sections
        if missing:
            raise ToolError(
                f"{self.rel(path)}: section(s) {sorted(missing)} renamed or removed -- "
                f"update DOC_SECTIONS in tools/invariants.py"
            )

    # -- module resolution ----------------------------------------------
    def top_level_names(self) -> set[str]:
        """First dotted components that can start a project import.

        ``app/`` goes on ``sys.path`` (AGENTS.md), so an import is a project
        import when its head is a layer package or a top-level app module.
        Anything else -- ``os``, ``tkinter``, ``requests`` -- is not, even when
        a bare name happens to collide with a project file (``app/utils/
        platform.py`` vs the stdlib ``platform``).
        """
        names = set(APP_LAYERS) | {"app"}
        names.update(p.stem for p in self.app_dir.glob("*.py"))
        return names

    def resolve_module(self, name: str) -> Path | None:
        """The ``app/`` file a dotted module name refers to, or None.

        Accepts every spelling docs/modules.md uses: fully qualified
        (``app.utils.config``), layer-relative (``utils.config``), and the bare
        stem the same file's row prefers (``config``).
        """
        parts = [p for p in name.split(".") if p]
        if not parts:
            return None
        if parts[0] == "app":
            candidates = [self.app_dir.joinpath(*parts[1:]).with_suffix(".py")]
        elif len(parts) > 1 and parts[0] in APP_LAYERS and parts[0]:
            candidates = [self.app_dir / parts[0] / (".".join(parts[1:]) + ".py")]
        elif len(parts) == 1:
            candidates = [self.app_dir / layer / (parts[0] + ".py")
                          for layer in ("", *LAYER_DIRS)]
        else:
            return None
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def imported_modules(self, tree: ast.Module, path: Path) -> list[tuple[int, str, Path]]:
        """Project modules *path* imports, as ``(lineno, dotted name, file)``.

        A name resolves to its longest prefix that is a real file under
        ``app/``, so ``from services.database import DatabaseManager`` records
        the module, not the class.  Imports inside an ``if TYPE_CHECKING:``
        block are dropped: they annotate, they are not runtime edges, and
        modules.md records the ones that exist in the row's "Used by" column
        by hand.
        """
        top_level = self.top_level_names()
        skipped = type_checking_ids(tree)
        found: list[tuple[int, str, Path]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if not node.level:
                    names = [node.module or ""]
                else:
                    # ``from . import x`` is a re-export of a *name* from the
                    # package, but it is also how a sibling module gets
                    # imported.  Offer both spellings; resolution keeps only
                    # the one that names a real file.
                    base = self._relative_name(node, path)
                    names = [base]
                    if base and not node.module:
                        names += [f"{base}.{a.name}" for a in node.names
                                  if not a.name.startswith("*")]
            else:
                continue
            if id(node) in skipped:
                continue
            for name in names:
                parts = name.split(".")
                if not parts or parts[0] not in top_level:
                    continue
                for count in range(len(parts), 0, -1):
                    prefix = ".".join(parts[:count])
                    resolved = self.resolve_module(prefix)
                    if resolved is not None:
                        found.append((node.lineno, prefix, resolved))
                        break
        return sorted(found)

    def _relative_name(self, node: ast.ImportFrom, path: Path) -> str:
        """A relative ``from . import x`` as a dotted name rooted at ``app``."""
        base = path.parent
        for _ in range(max(0, node.level - 1)):
            base = base.parent
        try:
            root = base.relative_to(self.app_dir).as_posix()
        except ValueError:
            return ""
        parts = [p for p in root.split("/") if p not in ("", ".")]
        if node.module:
            parts.extend(p for p in node.module.split(".") if p)
        return ".".join(parts)

    def layer_of(self, path: Path) -> str:
        """The layer package of an ``app/`` module ("" for the top level)."""
        try:
            head = path.relative_to(self.app_dir).parts[0]
        except (ValueError, IndexError):
            return ""
        return head if head in APP_LAYERS else ""

    def layer_rank(self, path: Path) -> int:
        """Depth index of an ``app/`` module; higher means closer to utils."""
        layer = self.layer_of(path)
        if layer:
            return APP_LAYERS[layer]
        return 0 if path.stem in APP_ROOTS else LEAF_RANK

    # -- helpers --------------------------------------------------------
    def exempt(self, rule_id: str, path: str) -> bool:
        """True when *path* is exempt for *rule_id*; records the use."""
        if path in EXEMPTIONS.get(rule_id, {}):
            self.used_exemptions.add((rule_id, path))
            return True
        return False

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()  # type: ignore[arg-type]
        except ValueError:
            return path.as_posix()

    def plugin_classes(self, plugin_dir_name: str, module: str) -> dict[str, ast.ClassDef]:
        """Class defs of ``integrations/<plugin_dir_name>/<module>.py``."""
        return {
            name: node
            for name, node, node_kind in self.plugin_bindings(plugin_dir_name, module, None)
            if node_kind == "class" and isinstance(node, ast.ClassDef)
        }

    def plugin_module_path(self, plugin_dir_name: str, module: str) -> Path:
        return self.integrations_dir / plugin_dir_name / f"{module}.py"

    def plugin_bindings(
        self, plugin_dir_name: str, module: str, kind: str | None
    ) -> list[tuple[str, ast.AST, str]]:
        """Top-level names of a plugin module as ``(name, node, kind)``.

        ``kind`` is ``"class"`` / ``"def"`` / ``"assign"``; pass ``None`` for
        all of them (what the contract rules walk).  An unreadable module
        returns nothing and records itself in :attr:`unparseable`, so callers
        skip it instead of cascading "does not implement ..." findings.
        """
        path = self.plugin_module_path(plugin_dir_name, module)
        if not path.exists():
            return []
        tree = self.parse(path)
        if tree is None:
            return []
        found: list[tuple[str, ast.AST, str]] = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                name, node_kind = node.name, "class"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name, node_kind = node.name, "def"
            elif isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if not targets:
                    continue
                found.append((targets[0], node, "assign"))
                continue
            else:
                continue
            if kind in (None, node_kind) or (kind == "callable" and node_kind in ("def", "class")):
                found.append((name, node, node_kind))
        return found


# ---------------------------------------------------------------------------
# Tier A -- catalog and contract rules
# ---------------------------------------------------------------------------

@rule("theme.key", "error",
      "Every C[\"...\"] theme key exists in THEME_MAP (utils/theme.py)",
      "AGENTS.md: Theme system -- a missing C[...] key raises KeyError at widget creation")
def check_theme_keys(ctx: Context) -> Iterable[Finding]:
    for path in ctx.sources:
        rel = ctx.rel(path)
        if ctx.exempt("theme.key", rel):
            continue
        tree = ctx.parse(path)
        if tree is None:
            yield Finding("theme.key", "error",
                          f"{ctx.unparseable[rel]} -- file skipped", rel)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            if not (isinstance(node.value, ast.Name) and node.value.id == "C"):
                continue
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                if sl.value not in ctx.theme_map:
                    yield Finding("theme.key", "error",
                                  f"C[{sl.value!r}] is not a THEME_MAP key "
                                  f"(a missing colour raises KeyError at widget creation)",
                                  rel, node.lineno)
            else:
                yield Finding("theme.key", "warn",
                              "non-literal C[...] index -- cannot verify statically, "
                              "verify the key by hand", rel, node.lineno)


@rule("i18n.key", "error",
      "Every tr()/trn() literal key exists in DEFAULT_STRINGS (utils/i18n.py)",
      "AGENTS.md: Translation system -- add a catalog row in the same commit as any tr() call")
def check_i18n_keys(ctx: Context) -> Iterable[Finding]:
    for path in ctx.sources:
        rel = ctx.rel(path)
        if ctx.exempt("i18n.key", rel):
            continue
        tree = ctx.parse(path)
        if tree is None:
            yield Finding("i18n.key", "error",
                          f"{ctx.unparseable[rel]} -- file skipped", rel)
            continue
        for call in iter_calls(tree, {"tr", "trn"}):
            key = string_arg(call, 0)
            if key is None:
                continue  # dynamic key (f-string/variable) -- exempt by design
            plural = call.func.attr == "trn" if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "") == "trn"
            if key in ctx.default_strings:
                continue
            if plural and f"{key}.one" in ctx.default_strings:
                continue
            if plural and f"{key}.other" in ctx.default_strings:
                continue
            kind = "trn()" if plural else "tr()"
            yield Finding("i18n.key", "error",
                          f"{kind} key {key!r} is not in DEFAULT_STRINGS", rel, call.lineno)


@rule("settings.key", "error",
      "get_setting()/get_setting_value()/set_setting() literals exist in DEFAULT_SETTINGS",
      "AGENTS.md: Config -- DEFAULT_SETTINGS defines the entire settings surface")
def check_settings_keys(ctx: Context) -> Iterable[Finding]:
    paths = [p for p in ctx.sources if p != ctx.config_source]
    for path in paths:
        rel = ctx.rel(path)
        if ctx.exempt("settings.key", rel):
            continue
        tree = ctx.parse(path)
        if tree is None:
            yield Finding("settings.key", "error",
                          f"{ctx.unparseable[rel]} -- file skipped", rel)
            continue
        for call in iter_calls(tree, {"get_setting", "set_setting", "get_setting_value"}):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
            section = string_arg(call, 0)
            if section is None:
                continue
            if section not in ctx.default_settings:
                yield Finding("settings.key", "error",
                              f"{name}({section!r}) -- no [{section}] section in "
                              f"DEFAULT_SETTINGS (the read always falls back)",
                              rel, call.lineno)
                continue
            options = ctx.default_settings[section]
            if name in ("get_setting", "set_setting") and "is_true" not in options:
                yield Finding("settings.key", "error",
                              f"{name}({section!r}) reads/writes the boolean option "
                              f"'is_true', which [{section}] does not define",
                              rel, call.lineno)
            option = string_arg(call, 1) if name == "get_setting_value" else None
            if option is not None and option not in options:
                yield Finding("settings.key", "error",
                              f"get_setting_value({section!r}, {option!r}) -- no "
                              f"'{option}' option in DEFAULT_SETTINGS[{section!r}]",
                              rel, call.lineno)

    # Direct ConfigParser reads inside utils/config.py itself (the module that
    # owns the schema); elsewhere ``.get``/``.getboolean`` is far too generic
    # to match, so this stays scoped to the one module where `cfg` is provably
    # a ConfigParser.
    if not ctx.exempt("settings.key", ctx.rel(ctx.config_source)):
        rel = ctx.rel(ctx.config_source)
        tree = ctx.parse(ctx.config_source)
        if tree is not None:
            yield from _check_configparser_reads(ctx, tree, rel)


def _check_configparser_reads(ctx: Context, tree: ast.Module, rel: str) -> Iterable[Finding]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("get", "getboolean", "getint", "getfloat"):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "cfg"):
            continue
        section, option = string_arg(node, 0), string_arg(node, 1)
        if section is None:
            continue
        if section not in ctx.default_settings:
            yield Finding("settings.key", "error",
                          f"cfg.{node.func.attr}({section!r}, ...) -- no [{section}] "
                          f"section in DEFAULT_SETTINGS", rel, node.lineno)
        elif option is not None and option not in ctx.default_settings[section]:
            yield Finding("settings.key", "error",
                          f"cfg.{node.func.attr}({section!r}, {option!r}) -- no "
                          f"'{option}' option in DEFAULT_SETTINGS[{section!r}]",
                          rel, node.lineno)


@rule("manifest.schema", "error",
      "Every integrations/*/plugin.json matches the enforced schema",
      "docs/plugins.md: plugin.json schema -- unknown keys are silently ignored by the loader")
def check_manifest_schema(ctx: Context) -> Iterable[Finding]:
    for plugin_dir, manifest, manifest_path in ctx.manifests:
        rel = ctx.rel(manifest_path)
        if ctx.exempt("manifest.schema", rel):
            continue
        if not manifest:
            yield Finding("manifest.schema", "error", "manifest is empty or invalid JSON", rel)
            continue
        for key in MANIFEST_REQUIRED:
            if key not in manifest:
                yield Finding("manifest.schema", "error", f"missing required key {key!r}", rel)
        yield from _check_manifest_types(manifest, rel)
        yield from _check_manifest_relations(
            manifest, plugin_dir, rel, (ctx.plugin_dir_path(plugin_dir) / "logo.png").exists()
        )

        # Cross-file: the manifest names a class that must exist in that module.
        # integration_class/flow_class are instantiated (so they must be classes),
        # while login_class is only *called* -- it may be a plain function.
        for manifest_key, module_key, kind in (
            ("integration_class", None, "class"),
            ("flow_class", "flow_module", "class"),
            ("login_class", "login_module", "callable"),
        ):
            class_name = manifest.get(manifest_key)
            if not isinstance(class_name, str) or not class_name:
                continue
            module = manifest.get(module_key) if module_key else "integration"
            if not isinstance(module, str) or not module:
                continue
            module_path = ctx.plugin_module_path(plugin_dir, module)
            if not module_path.exists():
                yield Finding("manifest.schema", "error",
                              f"{manifest_key}={class_name!r} but {module}.py does not "
                              f"exist (the class is resolved lazily, so this only fails "
                              f"at first use)", rel)
                continue
            bindings = {name for name, _node, _kind in
                        ctx.plugin_bindings(plugin_dir, module, kind)}
            module_rel = ctx.rel(module_path)
            if module_rel in ctx.unparseable:
                # One clear finding, not "the class is missing" for every class.
                yield Finding("manifest.schema", "error",
                              f"{module}.py {ctx.unparseable[module_rel]} -- "
                              f"cannot check {manifest_key}={class_name!r}", rel)
                continue
            if class_name not in bindings:
                yield Finding("manifest.schema", "error",
                              f"{manifest_key}={class_name!r} is not defined in "
                              f"{module}.py", rel)


@rule("manifest.port", "error",
      "plugin.json receiver_port matches the browser extension's host_permissions",
      "AGENTS.md: Integration quirks -- the receiver port is pinned in two places that must stay in sync")
def check_manifest_port(ctx: Context) -> Iterable[Finding]:
    for plugin_dir, manifest, manifest_path in ctx.manifests:
        rel = ctx.rel(manifest_path)
        if ctx.exempt("manifest.port", rel):
            continue
        port = manifest.get("receiver_port")
        if not isinstance(port, int) or isinstance(port, bool):
            continue
        ext_manifest = ctx.extension_manifest(plugin_dir)
        if ext_manifest is None:
            yield Finding("manifest.port", "warn",
                          f"receiver_port {port} declared but no *-extension/manifest.json "
                          f"to verify it against -- the port must match the extension's "
                          f"host_permissions", rel)
            continue
        try:
            ext = json.loads(ext_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            yield Finding("manifest.port", "error",
                          f"extension manifest is unreadable ({exc.__class__.__name__})",
                          ctx.rel(ext_manifest))
            continue
        except json.JSONDecodeError as exc:
            yield Finding("manifest.port", "error",
                          f"extension manifest is invalid JSON ({exc})", ctx.rel(ext_manifest))
            continue
        permissions = ext.get("host_permissions", []) if isinstance(ext, dict) else []
        found = sorted({
            int(m) for value in permissions
            if isinstance(value, str)
            for m in re.findall(r"(?:127\.0\.0\.1|localhost):(\d+)", value)
        })
        if not found:
            yield Finding("manifest.port", "warn",
                          f"extension host_permissions names no localhost port, so "
                          f"receiver_port {port} cannot be confirmed", ctx.rel(ext_manifest))
        elif found != [port]:
            yield Finding("manifest.port", "error",
                          f"receiver_port {port} != port(s) {found} in the extension's "
                          f"host_permissions -- the extension posts to a port the flow "
                          f"never binds", ctx.rel(ext_manifest))


@rule("contract.flow", "error",
      "Plugin integration/flow classes match the core base-class signatures",
      "docs/plugins.md: adding a platform -- BaseIntegration / BaseFlowController subclass")
def check_contract_flow(ctx: Context) -> Iterable[Finding]:
    for plugin_dir, manifest, manifest_path in ctx.manifests:
        rel = ctx.rel(manifest_path)
        if ctx.exempt("contract.flow", rel):
            continue
        if not manifest:
            # A manifest that failed to load declares nothing; manifest.schema
            # already said so, and "declares no flow" would be a false claim.
            continue
        has_flow_keys = any(k in manifest for k in MANIFEST_GROUPS["flow"])

        # integration.py always hosts the BaseIntegration subclass; flow.py
        # hosts the BaseFlowController one.  Both are contract surfaces, so
        # both are walked (the loader resolves either lazily).
        modules = ["integration"]
        flow_module = manifest.get("flow_module")
        if isinstance(flow_module, str) and flow_module:
            modules.append(flow_module)

        for module in modules:
            classes = ctx.plugin_classes(plugin_dir, module)
            module_rel = ctx.rel(ctx.plugin_module_path(plugin_dir, module))
            if module_rel in ctx.unparseable:
                # The manifest rule already reports the parse failure; adding
                # "does not implement ..." for every base method would bury it.
                continue
            for class_name, node in classes.items():
                for base in node.bases:
                    base_id = base_name(base)
                    if base_id not in ("BaseIntegration", "BaseFlowController"):
                        continue
                    rel_class = f"{module}.{class_name}"
                    base_methods = ctx.contract[base_id]
                    impl_methods = class_methods(node)
                    optional = set(ctx.contract_capability.get(base_id, ()))
                    supports = class_supports_playlists(node)
                    if base_id == "BaseIntegration" and not supports:
                        optional = set(base_methods)
                    for name, method in base_methods.items():
                        if name not in impl_methods:
                            if name not in optional:
                                yield Finding("contract.flow", "error",
                                              f"{rel_class} does not implement "
                                              f"{base_id}.{name}()", rel)
                            continue
                        for problem in signature_problems(
                            method, impl_methods[name], ctx.positional_count(method)
                        ):
                            yield Finding("contract.flow", "error",
                                          f"{rel_class}.{name}() {problem} -- core dispatches "
                                          f"against {base_id}.{name}{_render(method.params)}",
                                          rel)
                    if base_id == "BaseIntegration" and not supports:
                        for name in ctx.contract_capability.get("BaseIntegration", ()):
                            if name in impl_methods:
                                yield Finding("contract.flow", "warn",
                                              f"{rel_class} sets supports_playlists = False but "
                                              f"implements {name}() -- the core never calls it",
                                              rel)
                    if base_id == "BaseIntegration" and supports and not has_flow_keys:
                        yield Finding("contract.flow", "error",
                                      f"{rel_class} claims playlist support but the manifest "
                                      f"declares no flow (flow_type/flow_module/flow_class), so "
                                      f"no add path can be built", rel)


def _render(params: Iterable[Param]) -> str:
    return "(" + ", ".join(p.name for p in params if p.kind in ("posonly", "pos_or_kw", "kwonly")) + ")"


@rule("contract.scrobble", "error",
      "A plugin implementing ScrobbleCapable implements all of it",
      "docs/plugins.md / app/services/integration.py: duck-typed scrobble capability")
def check_contract_scrobble(ctx: Context) -> Iterable[Finding]:
    required = tuple(m for m in ctx.contract["ScrobbleCapable"])
    for plugin_dir, _manifest, _path in ctx.manifests:
        rel_module = f"integrations/{plugin_dir}/integration.py"
        if ctx.exempt("contract.scrobble", rel_module):
            continue
        classes = ctx.plugin_classes(plugin_dir, "integration")
        if rel_module in ctx.unparseable:
            continue  # reported once by manifest.schema
        for class_name, node in classes.items():
            impl = class_methods(node)
            present = [m for m in required if m in impl]
            if present and len(present) != len(required):
                missing = [m for m in required if m not in present]
                yield Finding("contract.scrobble", "error",
                              f"{class_name} implements "
                              f"{len(present)}/{len(required)} ScrobbleCapable methods "
                              f"({', '.join(present)}) -- missing {', '.join(missing)}; core "
                              f"duck-types the capability, so a partial one fails at call time",
                              rel_module)
            for name in present:
                base = ctx.contract["ScrobbleCapable"][name]
                for problem in signature_problems(
                    base, impl[name], ctx.positional_count(base)
                ):
                    yield Finding("contract.scrobble", "error",
                                  f"{class_name}.{name}() {problem} -- core calls "
                                  f"ScrobbleCapable.{name}{_render(base.params)}",
                                  rel_module)


_BRACE_GROUP_RE = re.compile(r"([\w.]+)\.\{([^{}]*)\}")


def documented_modules(cell: str) -> set[str]:
    """The module names a modules.md "Uses" cell documents.

    Only backticked spans count: those are the names the cell asserts as
    imports.  The prose around them ("stdlib only", "most modules",
    "`services.{database,song_manager}` (lazy)") is commentary, and reading
    prose words as module names is how a checker starts crying wolf.  The
    brace-group shorthand the file uses to keep rows on one line --
    ``utils.{config,theme}`` -- is expanded to ``utils.config``,
    ``utils.theme``; :func:`Context.resolve_module` then maps each name to a
    file, so a row may spell a module however it likes.
    """
    names: set[str] = set()
    for span in re.findall(r"`([^`]+)`", cell):
        expanded = _BRACE_GROUP_RE.sub(
            lambda m: " " + " ".join(
                f"{m.group(1)}.{part.strip()}" for part in m.group(2).split(",") if part.strip()
            ) + " ",
            span,
        )
        for token in re.split(r"[\s,/]+", expanded):
            token = token.strip().strip("()")
            if token and re.fullmatch(r"[\w.]+", token):
                names.add(token)
    return names


def documented_manifest_keys(text: str) -> dict[str, str]:
    """Parse the manifest-key table of a plugins.md-style doc.

    Only the ``## plugin.json schema`` section is read (the same file also
    documents the scrobble capability and the receiver protocol, whose tables
    look identical but are not manifest keys), and only its first two columns
    matter: key and requiredness.
    """
    rows: dict[str, str] = {}
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip().lower().startswith("## plugin.json schema")
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        # Skip the table's own header row and its ---|--- separator.
        if not cells[0].strip("` ") or set(cells[0]) <= set("-: "):
            continue
        if cells[0].replace("`", "").strip() == "Key":
            continue
        # A cell holds one key or a ``a`` / ``b`` pair.
        for key in cells[0].replace("`", "").split("/"):
            key = key.strip()
            if key:
                rows[key] = cells[1].strip().strip("`").lower()
    return rows


@rule("contract.doc", "error",
      "The enforced plugin.json schema and docs/plugins.md agree",
      "AGENTS.md: Documentation map -- plugins.md is the manifest contract reference")
def check_contract_doc(ctx: Context) -> Iterable[Finding]:
    if not ctx.schema_doc.exists():
        raise ToolError(f"{ctx.rel(ctx.schema_doc)} is missing")
    try:
        text = ctx.schema_doc.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolError(f"{ctx.rel(ctx.schema_doc)}: unreadable ({exc})") from exc
    documented = documented_manifest_keys(text)
    if not documented:
        raise ToolError(
            f"{ctx.rel(ctx.schema_doc)}: no '## plugin.json schema' key table found -- "
            f"update tools/invariants.py"
        )
    doc = ctx.rel(ctx.schema_doc)

    enforced = set(MANIFEST_FIELDS) | set(MANIFEST_REQUIRED)
    for key in sorted(enforced - set(documented)):
        yield Finding("contract.doc", "error",
                      f"docs/plugins.md does not document the enforced manifest key "
                      f"{key!r}", doc)
    for key in sorted(set(documented) - enforced):
        yield Finding("contract.doc", "error",
                      f"docs/plugins.md documents manifest key {key!r}, which the enforced "
                      f"schema in tools/invariants.py does not know -- add it to "
                      f"MANIFEST_FIELDS or drop it from the table", doc)

    # Only an unambiguous "yes"/"no" cell is comparable with the enforced
    # requiredness; the table's free-text cells ("extension/hybrid", ...) are
    # documentation, not a contract the tool can second-guess.
    for key in MANIFEST_REQUIRED:
        if documented.get(key) not in ("yes", None):
            yield Finding("contract.doc", "error",
                          f"docs/plugins.md marks {key!r} required={documented.get(key)!r}, "
                          f"but the enforced schema requires it unconditionally", doc)
    for key in sorted(enforced - set(MANIFEST_REQUIRED)):
        if documented.get(key) == "yes":
            group = next((g for g, keys in MANIFEST_GROUPS.items() if key in keys), None)
            yield Finding("contract.doc", "error",
                          f"docs/plugins.md marks {key!r} unconditionally required, but the "
                          f"enforced schema only requires it as part of the {group!r} group "
                          f"(service-only platforms such as lastfm omit it)", doc)


@rule("exempt.hygiene", "error",
      "Every exemption is justified and still needed",
      "tools/invariants.py: an exemption without a reason is not reviewable")
def check_exemption_hygiene(ctx: Context) -> Iterable[Finding]:
    for rule_id, table in EXEMPTIONS.items():
        if rule_id not in REGISTRY:
            yield Finding("exempt.hygiene", "error",
                          f"exemptions listed for unknown rule {rule_id!r} -- the rule was "
                          f"renamed or removed, so the table below it is dead",
                          "tools/invariants.py")
            continue
        for path, reason in sorted(table.items()):
            if not isinstance(reason, str) or not reason.strip():
                yield Finding("exempt.hygiene", "error",
                              f"exemption {rule_id} {path} has no reason", "tools/invariants.py")
            if (rule_id, path) in ctx.used_exemptions:
                continue
            # An exemption for a file this checkout does not have cannot be
            # evaluated at all: the rule never walked it, so "unused" would
            # be a false alarm rather than rot.  For app/ that is not the
            # case -- a missing module there is a real deletion, and the
            # stale entry should be reported (see
            # TestExemptions::test_stale_exemption_is_reported).  Under
            # integrations/ it IS the normal state: those plugin dirs are
            # gitignored, so a fresh clone has no plugin to exempt, and a
            # machine that never installed it must not be told its
            # exemption is stale.
            if path.startswith("integrations/") and not (ctx.root / path).exists():
                continue
            yield Finding("exempt.hygiene", "warn",
                          f"exemption {rule_id} {path} is unused -- the rule no longer "
                          f"flags that file, so the entry can go", "tools/invariants.py")


# ---------------------------------------------------------------------------
# Tier B -- architecture and threading
# ---------------------------------------------------------------------------

@rule("layer.import", "error",
      "No app module imports from a layer above it (docs/README.md: import direction)",
      "docs/README.md: Layers -- import direction goes downward; the exceptions are listed in modules.md")
def check_layer_imports(ctx: Context) -> Iterable[Finding]:
    for path in ctx.sources:
        rel = ctx.rel(path)
        # Asked before the scan, not after a finding: an exemption covers a
        # whole file, so it is "used" for as long as the file exists, whether
        # or not the upward edge it was written for is still there.
        exempted = ctx.exempt("layer.import", rel)
        tree = ctx.parse(path)
        if tree is None:
            continue  # reported per-file by the theme/i18n/settings rules
        source_layer = ctx.layer_of(path)
        source_rank = ctx.layer_rank(path)
        # An exemption is keyed by path, so on its own it would hide a *second*
        # upward edge added to an already-exempted file.  Requiring the reason
        # to name the module it justifies closes that hole without a second
        # table: the reason is mandatory anyway, and naming the edge is what
        # makes it reviewable.
        reason = EXEMPTIONS.get("layer.import", {}).get(rel, "")
        seen: set[str] = set()
        for lineno, name, resolved in ctx.imported_modules(tree, path):
            if ctx.layer_rank(resolved) >= source_rank or name in seen:
                continue
            seen.add(name)
            if exempted:
                if name not in reason:
                    yield Finding(
                        "layer.import", "warn",
                        f"imports {name}, which is above the {source_layer} layer, and "
                        f"this file's layer.import exemption does not name it -- the "
                        f"exemption covers one reviewed edge, not the file; add {name} "
                        f"to the reason or drop the edge",
                        rel, lineno,
                    )
                continue
            yield Finding(
                "layer.import", "error",
                f"imports {name}, which is above the {source_layer} layer -- imports go "
                f"downward; a new upward edge means a cycle is being papered over. Move "
                f"the work into a function and import it lazily, or push it into the "
                f"layer that owns the data, and list the edge in the 'Cross-layer "
                f"exceptions' section of docs/modules.md if it is unavoidable",
                rel, lineno,
            )



@rule("modules.doc", "error",
      "docs/modules.md documents every app module and every module it imports",
      "AGENTS.md: Documentation map -- modules.md is the per-file dependency table")
def check_modules_doc(ctx: Context) -> Iterable[Finding]:
    doc = ctx.rel(ctx.module_map_doc)

    # A row whose cell count is wrong makes its "Uses" column unreadable, so
    # the import check below cannot run for that file.  Report the row and skip
    # it rather than piling "undocumented" findings on top.
    broken_files: set[str] = set()
    for lineno, first_cell in ctx.module_map_broken:
        broken_files.add(first_cell.strip("`").rsplit("/", 1)[-1])
        yield Finding("modules.doc", "error",
                      f"row for {first_cell} does not split into {DOC_COLUMNS} cells -- "
                      f"a '|' inside a cell needs escaping as '\\|', or the whole table "
                      f"stops being a table", doc, lineno)

    for path in ctx.sources:
        rel = ctx.rel(path)
        if ctx.exempt("modules.doc", rel):
            continue
        if rel in ctx.module_map:
            continue
        marker = path.name == "__init__.py"
        tree = ctx.parse(path)
        if marker and tree is not None and package_marker(tree):
            continue  # a namespace marker: nothing in it to document
        if rel in ctx.unparseable:
            continue  # the parse failure is already one finding elsewhere
        if path.name in broken_files:
            continue  # its row exists, it is just unreadable -- reported above
        yield Finding("modules.doc", "warn" if marker else "error",
                      "has no row in docs/modules.md -- the module map answers 'where is "
                      "X and what does it import', and this module is in neither",
                      rel, 0)

    for key in sorted(ctx.module_map):
        path = ctx.root / key
        if not path.is_file():
            yield Finding("modules.doc", "error",
                          f"row documents {key}, which does not exist -- the module was "
                          f"moved or deleted and the row was not updated", doc)
            continue
        rel = ctx.rel(path)
        if ctx.exempt("modules.doc", rel):
            continue
        tree = ctx.parse(path)
        if tree is None:
            continue
        documented = {ctx.resolve_module(n) for n in documented_modules(ctx.module_map[key])}
        for lineno, name, resolved in ctx.imported_modules(tree, path):
            if resolved in documented:
                continue
            yield Finding("modules.doc", "error",
                          f"imports {name} but its 'Uses' cell does not list it -- the "
                          f"map is the only place that edge is written down",
                          rel, lineno)


@rule("thread.after", "error",
      "A worker thread's root.after() is behind a try/except",
      "AGENTS.md / docs/threading.md: after root.destroy() a root.after() raises TclError, "
      "and from a worker with no mainloop it raises RuntimeError -- both kill the daemon "
      "thread with a traceback at quit")
def check_thread_after(ctx: Context) -> Iterable[Finding]:
    for path in ctx.sources:
        rel = ctx.rel(path)
        if ctx.exempt("thread.after", rel):
            continue
        tree = ctx.parse(path)
        if tree is None:
            continue
        targets = thread_target_names(tree)
        if not targets:
            continue
        guarded = guarded_call_ids(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name not in targets:
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "after"):
                    continue
                if id(node) in guarded:
                    continue
                yield Finding(
                    "thread.after", "error",
                    f"{fn.name}() is a Thread target and calls "
                    f"{ast.unparse(node.func)} unguarded -- marshal through one of the "
                    f"repo's helpers (_schedule_ui / _async_ui / _tray_after) or wrap the "
                    f"call in try/except, or the callback is lost to a TclError at quit",
                    rel, node.lineno,
                )


# ---------------------------------------------------------------------------
# Tier C -- style
# ---------------------------------------------------------------------------

@rule("py.unused", "error",
      "No import in app/ binds a name nothing reads",
      "CONTRIBUTING.md: a dead import hides what a module actually depends on, and "
      "docs/modules.md's 'Uses' column is the map that dependency claim is made in")
def check_unused_imports(ctx: Context) -> Iterable[Finding]:
    for path in ctx.sources:
        rel = ctx.rel(path)
        if ctx.exempt("py.unused", rel):
            continue
        tree = ctx.parse(path)
        if tree is None:
            continue  # the parse failure is already one finding elsewhere
        lines = path.read_text(encoding="utf-8").splitlines()
        used = referenced_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue  # a compiler directive, not an import
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if "noqa" in line:
                continue  # the deliberate re-export marker
            for alias in node.names:
                name = import_binding(alias)
                if name is None or name in used:
                    continue
                if rebound_elsewhere(tree, name, node):
                    continue
                yield Finding(
                    "py.unused", "error",
                    f"{name!r} is imported and never read -- dead weight that hides "
                    f"what the module actually depends on; delete the import, or mark "
                    f"the line '# noqa' if the re-export is deliberate",
                    rel, node.lineno,
                )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_checks(ctx: Context, disabled: Iterable[str] = ()) -> list[Finding]:
    """Every finding from every enabled rule (exempt.hygiene always runs)."""
    disabled = set(disabled)
    unknown = disabled - set(REGISTRY)
    if unknown:
        raise ToolError(f"unknown rule id(s): {', '.join(sorted(unknown))}")
    # exempt.hygiene reads the *consumed* set every other rule fills in, so it
    # has to run last -- not because of where it sits in the file, which a new
    # rule added above it would silently break.
    ordered = sorted(REGISTRY.items(), key=lambda item: item[0] == "exempt.hygiene")
    findings: list[Finding] = []
    for rule_id, definition in ordered:
        if rule_id in disabled and rule_id != "exempt.hygiene":
            continue
        findings.extend(definition.check(ctx))
    return sorted(findings, key=Finding.sort_key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the repo's architectural invariants (custom typechecker).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 on any finding (default: exit 1 only on errors)")
    parser.add_argument("--disable", action="append", default=[], metavar="RULE",
                        help="skip a rule (repeatable); see --list-rules")
    parser.add_argument("--quiet", action="store_true", help="summary line only")
    parser.add_argument("--list-rules", action="store_true", dest="list_rules",
                        help="list the enforced rules and exit")
    args = parser.parse_args(argv)

    if args.list_rules:
        for definition in REGISTRY.values():
            print(f"{definition.id:18s} {definition.severity:5s} {definition.summary}")
            print(f"{'':18s}       rule: {definition.source}")
        return EXIT_OK

    try:
        ctx = Context()
        ctx.load()
        findings = run_checks(ctx, args.disable)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    for note in ctx.notes:
        print(f"note: {note}", file=sys.stderr)
    for finding in findings:
        if not args.quiet:
            print(finding.render())
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = len(findings) - errors
    print(f"{errors} error(s), {warnings} warning(s)"
          + (f", {len(args.disable)} rule(s) disabled" if args.disable else ""))
    if errors:
        return EXIT_FINDINGS
    return EXIT_FINDINGS if (warnings and args.strict) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
