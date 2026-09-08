# Contributing to PlaylistManager

This file covers the contribution process:
how to get the code, branch, commit, and open a pull request, plus the
project conventions your change must respect. It deliberately does not
document the architecture - that lives in `docs/` - and the operational
rules for working inside the repo live in `AGENTS.md`. See
[Documentation](#documentation) for how the files divide.

## Table of Contents
- [Development workflow](#development-workflow)
- [Getting the latest code (dev branch)](#getting-the-latest-code-dev-branch)
- [Project conventions](#project-conventions)
- [Documentation](#documentation)
- [Adding an integration](#adding-an-integration)
- [plugin.json reference](#pluginjson-reference)
- [Testing](#testing)

## Development workflow

1. Fork [d1pl6/PlaylistManager](https://github.com/d1pl6/PlaylistManager)
   on GitHub.
2. Clone your fork and add the upstream remote:

   ```bash
   git clone https://github.com/<your-username>/PlaylistManager.git
   cd PlaylistManager
   git remote add upstream https://github.com/d1pl6/PlaylistManager.git
   ```

3. Create a branch off `master` (the default branch):

   ```bash
   git checkout -b <your-change> origin/master
   ```

   Branch off `upstream/dev` instead when you need in-progress
   unreleased work as a starting point (see below).

4. Commit your changes. The repo has no commit-message convention -
   messages are terse and lowercase (`bug fixes`, `soundcloud
   integration`), with no `feat:`/`fix:` prefixes.
5. Push the branch to your fork:

   ```bash
   git push -u origin <your-change>
   ```

6. Open a pull request on GitHub from `<your-change>` to
   `d1pl6/PlaylistManager:master` - or to `:dev` when your change
   builds on unreleased work. In the description state what the change
   does and how you tested it (see [Testing](#testing)).
7. While the PR is open, keep the branch current with upstream:

   ```bash
   git fetch upstream
   git merge upstream/master
   git push
   ```

## Getting the latest code (dev branch)

In-progress work lands on the `dev` branch of the main repo
(`d1pl6/PlaylistManager`) before it is released to `master`. To follow
it:

```bash
git fetch upstream dev
git checkout -b dev upstream/dev   # first time only
git pull upstream dev              # afterwards, each time you want the latest
```

If you already track `dev` from your own fork, pull from upstream
directly:

```bash
git fetch upstream
git pull upstream dev
```

Use `dev` as the pull-request base instead of `master` (step 6) when
your change builds on work that has not been released yet - pick
`d1pl6/PlaylistManager:dev` in the "base" dropdown - and branch off
`upstream/dev` in step 3 for the same reason.

## Project conventions

Rules your change must respect. The full operational details and
reasoning live in `AGENTS.md`; these are the ones most likely to bite
a contribution.

- **Python 3.10+.** The code uses `X | Y` union type syntax; don't
  introduce constructs older interpreters can't parse.
- **No linter or typechecker is configured** - match the surrounding
  style and verify with the [Testing](#testing) checks instead. There
  is a pytest suite for the service layer; the GUI layer has no tests.
- **Dependency changes go in `pyproject.toml`**.
- **A theme color touches four places**: `THEME_MAP`
  (`app/utils/theme.py`), `DEFAULT_THEME` (`app/utils/config.py`),
  `theme.txt`, and `cfg/theme.ini` (the ini is the source of truth
  when files disagree). Every `C[...]` access must exist in
  `THEME_MAP` or the app raises `KeyError` at widget creation.
- **Add-flow invariant**: a keybind flow adds to the platform API
  first and aborts on failure - a `False` return or missing playlist
  id raises and nothing is written only to the local DB. Keep this
  ordering in any new flow; a platform failure must never leave a
  "successful" local entry.

## Documentation

- **`docs/README.md`** is the index of the structural reference:
  `modules.md` (who imports whom), `plugins.md` (plugin contract and
  `plugin.json` schema), `flows.md` (end-to-end call chains). Use it
  as the source of truth for how the code is wired together - this
  file and AGENTS.md link to it instead of repeating it.
- **`AGENTS.md`** (repo root) holds the operational rules: run
  commands, environment quirks, threading, Wayland, tray. Don't
  republish those here or in `docs/`.
- **`README.MD`** is the user guide, **`CLI.MD`** the CLI reference,
  **`INTEGRATIONS.MD`** the per-platform user setup. A change with
  user-visible effects should update the matching one.

## Adding an integration

See [`docs/plugins.md`](docs/plugins.md) for the full integration
guide - plugin system contract, `plugin.json` schema, and lifecycle.

If you wish to maintain your integration add it to `INTEGRATION_REPOS`
in `services/integration_manager.py`.

### plugin.json reference

See [`docs/plugins.md`](docs/plugins.md#pluginjson-schema) for the
`plugin.json` schema, field types, loader validation rules, and
field-by-field examples.

## Testing

The service layer (URL parsing, registry, song DB, profiles, duplicate
checking, plugin manifests, config files) has a **pytest suite** in
`tests/`. It runs headless - no display, no network, no real `db/`,
`cfg/`, or `auth/` (the conftest redirects every file-backed module to
a per-run temp tree). Run it from the repo root:

```bash
python -m pytest            # everything
python -m pytest -q         # quiet
python -m pytest tests/test_database.py   # one file
```

`pytest` is an optional dependency (`pip install .[test]` or
`pip install pytest`); it is not needed to run the app.

The automatic sanity checks are:

```bash
python -m compileall -q app/   # byte-compile everything
python -m app --list           # headless smoke test (needs no display)
```

Run these plus the suite before opening a PR. `compileall` walks every
module under `app/`, so a new module is covered automatically.

To verify a plugin loads without launching the GUI:

```bash
python -c "import sys; sys.path.insert(0, 'app'); \
from plugin_loader import PluginRegistry; \
print(PluginRegistry().discover().get_platform_ids())"
```

### Adding a test

- Tests live in `tests/test_<module>.py` with pytest-style classes
  (`TestAddPlaylist`) and the files are plain functions/classes - no
  test framework imports app modules with `app.` prefixes (see the
  import note below).
- **Never let a test touch the real `db/`, `cfg/`, or `auth/`.** File-
  backed modules (playlist store, song DB, duplicate queue, scrobble
  log, config, profiles) bind paths at import or read them at call
  time - point them at a `tmp_path` first (see the existing tests for
  the monkeypatch pattern per module). Destructive functions that wipe
  registry entries or databases are fine to call once sandboxed, but
  never against live data.
- Modules with pure logic (playlist URL parsing, duplicate matching,
  key normalization, DB sanitization) can be tested with no sandboxing.
- `conftest.py` inserts `app/` on `sys.path` and redirects the
  profile-store paths to a temp tree before any app module is
  imported - new test files get that for free.
- Tests must pass headlessly (no `tkinter.Tk`, no display) so they run
  in CI and on machines without a WM.

Notes:

- The GUI requires a display. Service-layer code can be exercised
  headlessly by importing with `sys.path.insert(0, "app")` - app
  modules use bare `from services.X import ...` imports and must be
  imported the same way in any harness or test (a `from
  app.services.X import ...` in a test creates a second module object
  and silently patches the wrong one - see AGENTS.md).
- `root.after(...)` from a worker thread raises `RuntimeError` unless
  the tkinter mainloop is actually running, and `TclError` after
  `root.destroy()` - a headless script that instantiates `tkinter.Tk`
  must never schedule from a non-main thread.