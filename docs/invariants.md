# Project invariants

`tools/invariants.py` checks the rules this project states in prose but
cannot execute. It is the repo's typechecker: `C["..."]` must be a real
`THEME_MAP` key, a `plugin.json` key must be one the loader reads, a plugin
class must match the signature core dispatches against, a module must not
import from a layer above it, a worker thread must not call `root.after()`
unguarded, and an import must be read. No off-the-shelf linter knows any of
that, and the rules otherwise live in `AGENTS.md` and `docs/` where nothing
checks them.

The rules come in two tiers. **Tier A** is the typechecker: catalog keys,
the `plugin.json` schema, cross-file port sync, the plugin contract.
**Tier B** is architecture and threading: the cross-layer import direction,
the `docs/modules.md` dependency map, unguarded `root.after()` calls in
worker threads. **Tier C** is style, and so far one rule: a dead import.
What is still not implemented is listed under
[Not implemented yet](#not-implemented-yet).

## Running

```bash
python tools/invariants.py                      # all rules, report
python tools/invariants.py --strict             # exit 1 on any finding
python tools/invariants.py --quiet              # summary line only
python tools/invariants.py --list-rules         # what is enforced here
python tools/invariants.py --disable theme.key  # skip one rule (repeatable)
```

Exit codes: `0` clean, `1` findings, `2` the tool cannot run (a source it
reads is missing, renamed, no longer a literal, or does not parse).
`--strict` promotes warnings to a non-zero exit; without it only errors
do.

Run it before a PR, next to `python -m compileall -q app/` and
`python -m pytest`. There is no CI job for it: it is a local gate.

## Why it imports nothing from `app/`

The four catalogs it checks against are module-level literal dicts, read
with `ast.literal_eval`:

| Catalog | Source | Size |
|---|---|---|
| `THEME_MAP` | `app/utils/theme.py` | 46 keys |
| `DEFAULT_STRINGS` | `app/utils/i18n.py` | 488 rows |
| `DEFAULT_SETTINGS` | `app/utils/config.py` | 31 sections |
| contract classes | `app/services/integration.py` | 3 classes, 14 methods |

Importing them would bind the real profile paths (`db/`, `cfg/`, `auth/`)
and pull in the app's optional dependencies. `ast.literal_eval` keeps the
tool side-effect free, so it runs on a bare interpreter in a fresh clone
before `pip install`, needs no display, and never touches user data. A
catalog that stops being a literal is a `ToolError`, not a silent skip: a
checker that quietly checks nothing is worse than none.

Tier B and C read no catalog at all — they walk `app/**/*.py` with `ast`,
and `modules.doc` reads `docs/modules.md` as text — so they add no import
and no new source of truth. `docs/modules.md` is required: a missing module
map, or one whose layer section headings no longer match `DOC_SECTIONS`, is
the same `ToolError` as a renamed catalog, because `modules.doc` built on a
map it could not read would report every module as undocumented.

The tool never writes a source file. Findings are reported; the fix is a
human or agent edit.

## Rules

### Tier A — catalogs and contracts

| Rule | Enforces | Stated in |
|---|---|---|
| `theme.key` | every `C["..."]` key exists in `THEME_MAP`; a non-literal index warns | theming.md |
| `i18n.key` | every `tr()`/`trn()` literal key exists in `DEFAULT_STRINGS` | i18n.md |
| `settings.key` | `get_setting()`/`get_setting_value()`/`set_setting()` sections and options exist in `DEFAULT_SETTINGS` | config.md |
| `manifest.schema` | `plugin.json` matches the enforced schema, and every class it names exists in the module it names | plugins.md |
| `manifest.port` | `receiver_port` equals the port in the browser extension's `host_permissions` | AGENTS.md |
| `contract.flow` | plugin `BaseIntegration`/`BaseFlowController` subclasses match the base signatures | plugins.md |
| `contract.scrobble` | a plugin implementing `ScrobbleCapable` implements all five methods (ListenBrainz is exempt: it implements the scrobble half) | plugins.md |
| `contract.doc` | the enforced schema and the `plugins.md` key table agree, in both directions | plugins.md |
| `exempt.hygiene` | every exemption has a reason and is still consumed | this file |

### Tier B — architecture and threading

| Rule | Enforces | Stated in |
|---|---|---|
| `layer.import` | no `app/` module imports from a layer above it, static or lazy | README.md "Layers", modules.md "Cross-layer exceptions" |
| `modules.doc` | `docs/modules.md` has a row per app module, names every import in its "Uses" cell, and its rows parse as four-column table rows | AGENTS.md documentation map |
| `thread.after` | a `Thread(target=...)` function's `.after()` is behind a `try`/`except` | AGENTS.md, docs/threading.md |

### Tier C — style

| Rule | Enforces | Stated in |
|---|---|---|
| `py.unused` | no import in `app/` binds a name nothing reads | CONTRIBUTING.md |

`i18n.key` matches `tr()`/`trn()` as a bare name or as `i18n.tr()`, which
is how `app/app.py` calls it. `tr_status()` is not checked against
`DEFAULT_STRINGS`: its argument is a stable card-status code, and opaque
plugin messages pass through unchanged by design. A dynamic key (f-string
or variable) is skipped, matching `tests/test_i18n.py`.

### When an input is broken

A file that does not parse produces exactly one finding against that path
and is skipped; every other app file is still checked. The same holds
for a plugin module and for a manifest that is not valid JSON, with the
dependent contract rule staying quiet rather than cascading "does not
implement ..." for every base method. This is deliberate: an uncaught
`SyntaxError` exits 1, the same code as "found something", so a half-typed
file would read as a verdict on the repo.

Two inputs are fatal instead, and exit 2 with a message naming the file: the
four catalog sources, the contract module, and `docs/modules.md`. A rule
built on a source it could not read would report nonsense, which is worse
than refusing to run.

`settings.key` also reads `cfg.get(...)`/`cfg.getboolean(...)` inside
`app/utils/config.py` only. In that module `cfg` is provably a
`ConfigParser`; elsewhere `.get` is too common to match. A
`get_setting()` call against a section with no `is_true` option is
reported: `getboolean` then raises and the read silently falls back.

**Known limit**: the rule only sees *literal* section names, so it says
nothing about a section computed at runtime. That is deliberate for the one
such setting, `[<platform id>] scrobbles` — the section is a plugin id, so
`DEFAULT_SETTINGS` cannot enumerate it and the tool could not know which
plugins are installed (see [config.md](config.md)). The misspelling that
would silently fall back is instead caught at the reader
(`utils.config.get_setting_bool` defaults to enabled) and by
`tests/test_config.py`.

### Capability exemptions inside the contract

`BaseIntegration` has 8 methods. A service-only platform sets
`supports_playlists = False` in its class body and may then omit the five
playlist methods; the other three (`is_authenticated`, `authenticate`,
`refresh_auth`) are always required. Omitting them is an error, not a
warning: core calls them unconditionally.

The inverse is a warning: a class that sets `supports_playlists = False`
and implements a playlist method ships code the core never calls.

`ScrobbleCapable` is duck-typed, so a plugin is not required to inherit
it. It must be all five methods or none. One to four means a partially
implemented capability that fails at call time rather than at load time -
unless the file carries a `contract.scrobble` exemption, which is what
`integrations/listenbrainz/integration.py` does.

The exemption is safe *because* of how the core consumes the capability, so
it is not a hole: `ScrobbleCapable` is a union of two unrelated halves (the
scrobble half `scrobble` + `delete_scrobble`, the love half `love` /
`unlove` / `is_loved` plus the optional `get_loved_set`), and every core
selection goes through `services/scrobble.scrobble_backends()` (with
`scrobble_backend_for()` for the remove path and `resolve_scrobble_backend()`
for the love paths), which ask for the capabilities the call site needs. ListenBrainz
implements the scrobble half and cannot implement the love half (its love/
hate feedback is keyed on a MusicBrainz recording MBID this app has no
metadata to resolve), so absence makes it *ineligible for the heart* - the
button keeps resolving to Last.fm - instead of blowing up at click time.
A capability that is present but does not work is the real hazard; do not
exempt a stub.

### The signature check is dispatch-aware

`contract.flow` and `contract.scrobble` compare a plugin method against
its base method and report three problems: a dropped parameter, a new
required parameter, and a reorder.

The reorder check only covers the parameters the app actually passes
positionally. `ctx.positional_dispatch` records, per contract method, the
largest number of positional arguments any call site in `app/` uses
(`execute_flow` is 4, `get_playlist_details` is 1); everything past that
is passed by keyword and may be ordered freely. All four shipped flow
controllers order `playlist_id`/`url`/`song_data` differently from
`BaseFlowController`, which is safe under that convention and would stop
being safe the moment a caller passed one of them positionally. A new
positional argument is required unless the implementation has a default
or `**kwargs`.

### `layer.import` reads the layer diagram as a partial order

`APP_LAYERS` assigns a depth index per layer, deepest last, and an import
from a module at a higher index into one at a lower index is *upward*.
The diagram in [README.md](README.md) is the rule, and its two loose ends
are resolved explicitly:

- **`controllers` and `ui` share a rank**, so an import between them is
  lateral, not upward — `app_controller` delegating the dialogs to the UI
  layer is legal, and `card_grid`/`main_window` importing
  `controllers.keybind_registry` for `KeybindCallbacks` is legal. A
  `services` → `ui` or `services` → `controllers` import *is* upward and
  is caught.
- **`app/`'s top level is not one layer.** The four roots
  (`__main__.py`, `main.py`, `app.py`, `cli.py`) sit above everything and
  may reach any layer; `plugin_loader.py` and `_version.py` are shared
  leaves the diagram draws at or below `utils/`, so importing them from a
  layer is downward. Anything else at the top level is treated as a leaf,
  which is the permissive default: a new top-level module is importable
  from anywhere rather than flagged until someone decides it is a root.

Only the *first* component of an import is resolved against this table, so
`import platform` cannot be mistaken for `app/utils/platform.py` — which
matters, because `utils/platform.py` does exist. A relative import resolves
against the importing file, and `from . import card` is treated as an edge to
`ui.card` when a sibling module of that name exists and as a plain
re-export when it does not, which is what it is in each case.

An exemption is keyed by path, which on its own would hide a *second*
upward edge added to an already-exempted file. So the rule requires the
exemption's **reason to name the module it covers**: an upward edge in an
exempted file that the reason does not name is a warning. That makes the
reason a specification rather than a justification, and needs no second
table.

**Known limits.** `importlib.import_module("services.x")` and a name built
at runtime are invisible — the rule reads import statements only. The
cross-repo edges into `integrations/` are not checked at all, deliberately:
a plugin importing core is the design, and the plugin repos are not in this
tree. A `services` module importing a `ui` module *inside a function* is
still caught, because the rule does not care whether the edge fires at load
time.

### `modules.doc` reads only the backticked names

The rule parses the `docs/modules.md` tables and checks five things: an app
module with no row (error), an `__init__.py` that is not a bare package
marker with no row (warning — the four layer packages are namespace
markers with nothing in them to document), a row for a file that no longer
exists (error), a project import the row's "Uses" cell does not name
(error), and a row that does not split into four cells (error).

That last one is a real finding rather than a shrug: an unescaped `|` in a
cell breaks the table for every renderer *and* makes the "Uses" column
unreadable to this tool. The splitter honours `\|`, including inside a code
span, so an escaped pipe is read as one cell. A broken row is reported once
and then skipped, rather than also producing "has no row" for the same
file.

Only names inside backticks count. The prose around them ("stdlib only",
"most modules") is commentary, and reading prose words as module names is
how a checker starts crying wolf. The brace-group shorthand the file uses
to keep rows on one line is expanded before resolution:

```
`utils.{config,theme}`   ->   utils.config, utils.theme
```

Names are then matched by *resolved file*, not by string, so a row may
spell a module however it likes — `utils.config`, `app.utils.config`, or
the bare `config` the same table prefers elsewhere. A name that resolves to
nothing (`integrations.*`, which lives in another repo) is ignored.

An import inside `if TYPE_CHECKING:` is **not** a documented edge. It
annotates rather than runs. Nothing in `app/` relies on that any more —
`KeybindCallbacks` used to sit in a `controllers/callbacks.py` of its own,
which meant `keybind_registry` had to name it under `TYPE_CHECKING`; both
ends already import the registry, so folding the class back in removed
both the module and the special case.

**Known limits.** The reverse direction — a name in a "Uses" cell that is no
longer imported — is not checked, because the sibling "Used by" column is
free prose ("most modules", module names without their directory) and has
no parseable shape. A stale *row* is caught; a stale *entry inside* a row
is not.

### `thread.after` reads the Thread target, not the file

A function handed to `threading.Thread(target=...)` runs off the main
thread, where `root.after()` raises `TclError` after `root.destroy()` and
`RuntimeError` when there is no mainloop — either way the daemon thread
dies with a traceback at quit, silently dropping the callback. So an
`.after(...)` call inside such a function must be behind a `try` with an
`except` handler. That is one of the two guards `AGENTS.md` names; the
other, routing through `_schedule_ui`/`_async_ui`/`_tray_after`, leaves no
direct `.after()` in the target function at all, so it never fires.

`Thread` is matched as an attribute call, so `threading.Thread` and a bare
`Thread` both count. A `target=self._method` attribute is tracked by its bare
name — the dominant style in `app/ui/`, and the one the rule would otherwise
be blind to — which means a same-named method of a *different* class in the
same file is checked too. So is every definition of a name that appears
several times in a file (seven nested `run` functions in `login_ui.py`).
Both biases err towards reporting, which costs a glance; the opposite bias
would hide a real bug. A `target=lambda: ...` is the one form not tracked,
because there is no name to put in the message.

A target passed *positionally* is the second argument, not the first:
`Thread(group=None, target=None, ...)` is the stdlib signature, so
`Thread(None, work)` names a target and `Thread(work)` passes `work` as the
group — which raises at `start()`, so it is a different bug and not this
rule's business.

**Known limits.** Only a *direct* `.after()` in the target is checked. A
worker that calls a module-level helper which then calls `root.after()`
unchecked is invisible — which is exactly what `manage_integrations_ui`'s
`_after_main` is, and it is the shape a new bug would take. `ast` cannot
see that the helper is only ever called from a worker without a call-graph
analysis this tool does not attempt.

### `py.unused` reads a name, not a line

An import whose bound name is never referenced is dead. The interesting
part is everything that counts as a reference, because a false positive
here deletes an import that was load-bearing:

- **The read itself.** `json.loads` puts `json` in the read set as the
  `Name` the `Attribute` hangs off, and `import os.path` binds `os` with
  no node at all — the alias table handles the split at the first dot.
- **`__all__`**, which is the explicit statement that a name is exported.
- **A string annotation** (`-> "MainWindow"`), which is a `Constant` and
  would otherwise look unread. Only *annotation position* is scanned —
  a parameter, a return, an `AnnAssign`. Letting any string count would
  hand the rule a free pass: an i18n key like `notif.scan_found` or a
  docstring that says "json" would mask a genuinely dead import.
- **A `# noqa` on the line**, the marker this repo already uses for the
  one deliberate re-export (`app/__init__.py`'s `__version__`).

And two things that make the rule say nothing at all, because "is this
import read?" stops being a question `ast` can answer:

- **The name is bound somewhere else too** — the `import logging` /
  `logging = configure()` idiom, a local shadowing a module-level import
  (`for os in paths`), a second import of the same name, an
  `except X as os`. Only a *read* counts as a use, so these are the
  remaining bindings, and whether that first import still does anything is
  not a question a name-based scan can answer. The rule says nothing.
- **A `from . import *`**, where the bound names are unknowable from here.

`from __future__ import annotations` is skipped as what it is: a compiler
directive, not an import.

**Known limits.** `app/` only — plugin modules are separate repositories
with their own CI, and the tool reads them for the manifest and contract
rules rather than scanning them. A name read only through `globals()`,
`getattr(module, "x")` or an `exec`'d string is invisible. A name used in a
`# type: ignore`-style comment is a comment.

## Exemptions

Exemptions live in the `EXEMPTIONS` table in the tool, not in a separate
config file, and each is `{path: reason}` with a mandatory reason:

```python
EXEMPTIONS = {
    "theme.key": {
        "app/utils/theme.py": "defines and populates the C mapping itself",
    },
    "settings.key": {
        "app/utils/config.py": "owns DEFAULT_SETTINGS; reads the legacy [update_check] is_true key",
    },
}
```

`exempt.hygiene` reports two failure modes. An entry whose reason is
blank is an error: an unjustified allowlist is not reviewable. An entry no
rule ever consumes is a warning: the module was renamed or deleted and the
entry is now dead weight that hides nothing. `tests/test_invariants.py`
additionally asserts every exempt path still exists on disk.

One narrow carve-out applies to the staleness warning: an unused
exemption whose path is under `integrations/` **and** absent from the tree
is skipped, because those plugin dirs are gitignored - a fresh clone has no
plugin to exempt, and reporting it would train people to ignore the
warning. Under `app/` nothing is carved out: a missing module there is a
real deletion and the stale entry is still reported.

Six entries ship today, all narrow: `utils/theme.py` builds the `C`
mapping from `theme.ini` so its indices are file-read names;
`utils/config.py` reads the legacy `[update_check] is_true` key in
`_migrate_update_check_interval` on files that still carry it (that key is
deliberately absent from `DEFAULT_SETTINGS` since the setting became an
interval enum); `integrations/listenbrainz/integration.py` implements
only the scrobble half of `ScrobbleCapable` (see above); and three
`utils` → `services` upward edges that [modules.md](modules.md) lists as
cross-layer exceptions — `utils/config.py` and `utils/i18n.py` both need
`profile_store` statically because a module-level path constant derives
from it, and `utils/thumbnail.py` needs `duplicate_check` and
`song_manager` lazily so the add path and the thumbnail cache agree on
what a song identity is.

The three `layer.import` reasons each name the modules they cover, because
the rule checks that (see above). That is also the closest thing to a
"narrow" exemption the design gets for free: the reason is load-bearing
twice, once as a justification and once as the list of edges allowed.

## Adding a rule

1. Add the `@rule(id, severity, summary, source)` function to the tool.
   `summary` is one line; `source` names the doc the rule comes from.
2. Yield `Finding(rule, severity, message, path, line)`. `path` is
   repo-relative.
3. Add an exemption only for a file that cannot satisfy the rule, and
   write the reason.
4. Add a violating-case test and a satisfying-case test to
   `tests/test_invariants.py`. The suite points `Context(root=...)` at a
   synthetic tree in `tmp_path`, so a rule test needs no repo file.
5. Run the tool. It must be clean on this repo, or the finding is a real
   one to fix first.

`TestRealRepository` asserts the tool passes the tree it ships with, so a
rule that cannot pass is caught at once. `TestCleanTree` asserts a
synthetic repo is clean, so a rule that fires on everything is caught too.

## Not implemented yet

Tier A (catalogs and contracts), Tier B (architecture and threading) and
Tier C (`py.unused`) are above. Two checks are absent, and nothing in the
tool pretends to check them:

- **A line-length cap**, which is not adoptable here rather than merely
  unwritten: the tree mixes 79- and 100-column code and
  `app/utils/i18n.py:165` runs to 329. Any cap would be either ignored or
  adopted by reformatting code nobody asked to reformat.
- **Dependency declarations**: `pyproject.toml` checked against the
  imports that actually happen. This needs an import-name-to-distribution
  table, because the names and the distributions do not match — `PIL`
  ships in `pillow`, `minisign` in `py-minisign`, and `keyring` is an
  optional extra that must not be confused with the keyring *protocol*.

## Known limits

- Only literal call arguments are checked. A key or section computed at
  runtime is skipped, except a non-literal theme index, which warns so
  someone verifies it by hand.
- `manifest.port` warns rather than fails when a plugin declares a
  `receiver_port` with no extension manifest next to it. That is
  legitimate: an extension can live in its own repository.
- `contract.doc` compares only the unambiguous `yes`/`no` cells of the
  `plugins.md` Required column. The free-text cells (`extension/hybrid`,
  `add-flow`) are documentation, not a contract the tool can
  second-guess.
- Rules run over `app/**/*.py`, not `tools/`, `tests/`, or
  `integrations/`. A plugin rule against an un-cloned `integrations/`
  skips with a note rather than passing silently; the manifest and
  contract rules read plugin modules by name, but no rule *scans* them
  for style or dead code.

The per-rule limits are in the Tier B and C sections above
([`layer.import`](#layerimport-reads-the-layer-diagram-as-a-partial-order),
[`modules.doc`](#modulesdoc-reads-only-the-backticked-names),
[`thread.after`](#threadafter-reads-the-thread-target-not-the-file),
[`py.unused`](#pyunused-reads-a-name-not-a-line)); the rest are here.
