# Architecture notes

This folder documents how the code is wired together: which module uses
which, what a platform plugin must provide, and how a user action travels
through the layers. It answers "where does X live" and "who calls Y". It
does not explain how to run the app or individual function bodies; those
live in docstrings.

## Navigation

| Question | File |
|---|---|
| Who imports module X? What does X import? | [modules.md](modules.md) |
| What keys does plugin.json need? Who reads each key? | [plugins.md](plugins.md) |
| How does feature Y work end to end? | [flows.md](flows.md) |

Other documentation lives elsewhere on purpose:

- `AGENTS.md` (repo root): run commands, environment quirks, threading /
  Wayland / tray gotchas. Operational rules. This folder does not repeat
  them, it links instead.
- `README.MD`: user guide. `CLI.MD`: cli and global install guides.
  `INTEGRATIONS.MD`: per-platform setup for users.
- Each integration documents its own internals in its own repository.
  `integrations/*` are separate repos; only their manifests follow the
  contract in [plugins.md](plugins.md).

## Layers

```
main.py  app/__main__.py           entry points
  |
app/main.py                        argparse: GUI vs headless dispatch
  |          \
  |           app/cli.py           headless commands (-a -p --list ...)
app/app.py (App)                   GUI bootstrap
  |
controllers/   ui/                 orchestration, widgets
  |             |
services/                          stores, SQLite, sync, base classes
  |
utils/                             config, theme, scaling, helpers

plugin_loader.py ---- integrations/<platform>/    plugins (separate repos)
```

Import direction goes downward: `ui` and `controllers` import `services`
and `utils`; `services` imports `utils`; `utils` imports nothing from the
app packages. The known exceptions are function-level lazy imports, listed
at the bottom of [modules.md](modules.md).

## Maintenance

Update the relevant file in the same commit as the code change: a moved
or renamed module edits `modules.md`, a new manifest key edits
`plugins.md`, a changed call chain edits `flows.md`. Entries stay at one
line per fact. If a fact needs a paragraph, it belongs in the module's
docstring, not here.
