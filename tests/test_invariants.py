"""Tests for the invariant checker (tools/invariants.py).

Covers:
* a minimal synthetic repo that is clean by construction
* one seeded violation per rule, so no rule can silently stop working
* the exemption mechanism (consumed / unjustified / stale)
* ToolError on a renamed or non-literal catalog source
* CLI surface (exit codes, --list-rules, --disable, --strict)
* dogfood: the real repository passes every rule

Headless-safe: the tool imports no app module, so nothing here needs a
display, a profile sandbox or the app's optional dependencies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import invariants as tool  # noqa: E402  (bare-import rule, see AGENTS.md)

# ---------------------------------------------------------------------------
# Synthetic repo contents
# ---------------------------------------------------------------------------

THEME_SRC = '''\
THEME_MAP = {
    "bg": ("background", "background"),
    "accent_bg": ("highlight", "background"),
}


def load_theme():
    C = {}
    C[name] = "#000"          # dynamic: filled from theme.ini
    return C
'''

I18N_SRC = '''\
DEFAULT_STRINGS = {
    "app.ok": "OK",
    "app.gone": "Gone",
    "items.one": "{count} item",
    "items.other": "{count} items",
}
'''

CONFIG_SRC = '''\
DEFAULT_SETTINGS = {
    "fullscreen": {"is_true": "no"},
    "ui_scale": {"value": "auto"},
}


def _read():
    cfg = None
    return cfg.getboolean("fullscreen", "is_true")
'''

CONTRACT_SRC = '''\
class BaseIntegration:
    supports_playlists: bool = True

    def is_authenticated(self) -> bool:
        raise NotImplementedError

    def authenticate(self) -> bool:
        raise NotImplementedError

    def refresh_auth(self) -> bool:
        raise NotImplementedError

    def get_library_playlists(self) -> list:
        raise NotImplementedError

    def get_playlist_details(self, playlist_id: str, limit: int = 1) -> dict:
        raise NotImplementedError

    def get_playlist_id(self, name: str):
        return None

    def get_playlist_tracks(self, playlist_id: str) -> list:
        return []

    def remove_track(self, playlist_id: str, track_id: str) -> bool:
        return False


class ScrobbleCapable:
    def scrobble(self, song_data: dict) -> int:
        raise NotImplementedError

    def unlove(self, artist: str, track: str) -> bool:
        raise NotImplementedError

    def love(self, artist: str, track: str) -> bool:
        raise NotImplementedError

    def is_loved(self, artist: str, track: str):
        raise NotImplementedError

    def delete_scrobble(self, artist: str, track: str, timestamp: int = None) -> bool:
        raise NotImplementedError


class BaseFlowController:
    def execute_flow(self, playlist_name, on_status, on_error, on_success,
                     playlist_id=None, url=None, song_data=None,
                     skip_duplicate_check=False) -> None:
        raise NotImplementedError
'''

CARD_SRC = '''\
from utils.i18n import tr, trn
from utils.config import get_setting, get_setting_value
from utils.theme import C


def render(n):
    return (C["bg"], tr("app.ok"), trn("items", n),
            get_setting("fullscreen"), get_setting_value("ui_scale", "value"))
'''

# utils -> services, lazily: one of the three upward edges app/ actually has.
# The two modules it imports are the two the *shipped* layer.import exemption
# for app/utils/thumbnail.py names in its reason, so the fixture exercises the
# exemption on a real edge instead of a vacuous one.
THUMBNAIL_SRC = '''\
import threading


def fetch_song_image(song):
    from services.duplicate_check import resolve_near_duplicate
    from services.song_manager import warm_song_cache
    return resolve_near_duplicate, warm_song_cache


def worker():
    fetch_song_image({})
    threading.Thread(target=worker).start()
'''

# An entry point: the root of the layer graph, so anything it imports is
# downward.  Kept minimal -- main.py's job in the test is to be a legal row.
MAIN_SRC = '''\
from utils.config import get_setting_value


def main():
    return get_setting_value("ui_scale", "value")
'''

# Mirrors how the app dispatches a flow: the first four arguments positionally,
# everything after that by keyword (app/cli.py, app/ui/main_window.py).
DISPATCH_SRC = '''\
def dispatch(flow, playlist_name, on_status, on_error, on_success, url, song_data,
             playlist_id):
    flow.execute_flow(
        playlist_name,
        on_status,
        on_error,
        on_success,
        url=url,
        song_data=song_data,
        playlist_id=playlist_id,
    )
'''

INTEGRATION_SRC = '''\
from services.integration import BaseIntegration


class DemoIntegration(BaseIntegration):
    def is_authenticated(self):
        return True

    def authenticate(self):
        return True

    def refresh_auth(self):
        return True

    def get_library_playlists(self):
        return []

    def get_playlist_details(self, playlist_id, limit=1):
        return {}

    def get_playlist_id(self, name):
        return None

    def get_playlist_tracks(self, playlist_id):
        return []

    def remove_track(self, playlist_id, track_id):
        return False
'''

FLOW_SRC = '''\
from services.integration import BaseFlowController


class DemoFlow(BaseFlowController):
    def execute_flow(self, playlist_name, on_status, on_error, on_success,
                     playlist_id=None, url=None, song_data=None,
                     skip_duplicate_check=False):
        on_success()
'''

# Same flow plus one extra keyword-only parameter the core never passes.
FLOW_EXTRA_REQUIRED_SRC = '''\
from services.integration import BaseFlowController


class DemoFlow(BaseFlowController):
    def execute_flow(self, playlist_name, on_status, on_error, on_success,
                     playlist_id=None, url=None, song_data=None,
                     skip_duplicate_check=False, *, extra):
        on_success()
'''

# ... and the same, absorbed by **kwargs.
FLOW_KWARGS_SRC = '''\
from services.integration import BaseFlowController


class DemoFlow(BaseFlowController):
    def execute_flow(self, playlist_name, on_status, on_error, on_success,
                     playlist_id=None, url=None, song_data=None,
                     skip_duplicate_check=False, *, extra, **kwargs):
        on_success()
'''

SERVICE_ONLY_INTEGRATION_SRC = '''\
from services.integration import ScrobbleCapable


class DemoIntegration(ScrobbleCapable):
    def scrobble(self, song_data):
        return 0

    def unlove(self, artist, track):
        return True

    def love(self, artist, track):
        return True

    def is_loved(self, artist, track):
        return False

    def delete_scrobble(self, artist, track, timestamp=None):
        return True
'''

MANIFEST = {
    "id": "demo",
    "display_name": "Demo",
    "integration_class": "DemoIntegration",
    "flow_type": "api",
    "flow_module": "flow",
    "flow_class": "DemoFlow",
    "url_hosts": ["demo.com"],
    "playlist_url_template": "https://{host}/playlist/{id}",
    "song_url_template": "https://{host}/track/{id}",
    "version": 1,
}

EXTENSION_MANIFEST = {
    "manifest_version": 3,
    "host_permissions": ["https://demo.com/*", "http://127.0.0.1:5000/*"],
}


def _schema_table() -> str:
    """A docs/plugins.md manifest table that agrees with the enforced schema.

    Generated from the tool's own constants, so the fixture stays valid when a
    key is added -- the point of the fixture is to be clean, not to hard-code
    a second copy of the schema.
    """
    lines = ["## plugin.json schema", "", "| Key | Required | Read by | Purpose |",
             "|---|---|---|---|"]
    for key in sorted(tool.MANIFEST_FIELDS):
        if key in tool.MANIFEST_REQUIRED:
            required = "yes"
        else:
            group = next((g for g, keys in tool.MANIFEST_GROUPS.items() if key in keys), None)
            required = group or "no"
        lines.append(f"| `{key}` | {required} | somewhere | something |")
    return "\n".join(lines) + "\n\n## Lifecycle\n\nnothing else\n"


# The app modules in the synthetic tree, grouped the way docs/modules.md groups
# them: section heading -> rows.  The "Uses" cell of each row names exactly what
# the module imports, so the fixture is clean by construction.  A row's file is
# resolved as "app/<section dir>/<first cell>", except in the entry-point
# section, where the cell already spells the full path.
FIXTURE_ROWS = {
    "Entry points and bootstrap": [
        ("app/main.py", "`utils.config`"),
    ],
    "controllers/": [
        ("dispatch.py", "stdlib only"),
    ],
    "services/": [
        ("duplicate_check.py", "stdlib only"),
        ("integration.py", "stdlib only"),
        ("song_manager.py", "stdlib only"),
    ],
    "ui/": [
        ("card.py", "`utils.{i18n,config,theme}`"),
    ],
    "utils/": [
        ("config.py", "stdlib only"),
        ("i18n.py", "stdlib only"),
        ("spare.py", "stdlib only"),
        ("theme.py", "stdlib only"),
        ("thumbnail.py", "`services.{duplicate_check,song_manager}` `(lazy)`"),
    ],
}


def _modules_doc() -> str:
    """A docs/modules.md with one correct row per app module in the fixture."""
    lines: list[str] = []
    for section, rows in FIXTURE_ROWS.items():
        lines += [f"## {section}", "", "| File | Role | Used by | Uses |",
                  "|---|---|---|---|"]
        lines += [f"| `{name}` | role | someone | {uses} |" for name, uses in rows]
        lines.append("")
    return "\n".join(lines) + "\n"




def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """A minimal synthetic repo that every rule accepts."""
    (tmp_path / "app" / "utils").mkdir(parents=True)
    (tmp_path / "app" / "services").mkdir(parents=True)
    (tmp_path / "app" / "ui").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    _write(tmp_path / "app" / "main.py", MAIN_SRC)
    _write(tmp_path / "app" / "utils" / "theme.py", THEME_SRC)
    _write(tmp_path / "app" / "utils" / "i18n.py", I18N_SRC)
    _write(tmp_path / "app" / "utils" / "config.py", CONFIG_SRC)
    _write(tmp_path / "app" / "utils" / "spare.py", "SPARE = 1\n")
    _write(tmp_path / "app" / "utils" / "thumbnail.py", THUMBNAIL_SRC)
    _write(tmp_path / "app" / "services" / "integration.py", CONTRACT_SRC)
    _write(tmp_path / "app" / "services" / "duplicate_check.py", "def resolve_near_duplicate():\n    return None\n")
    _write(tmp_path / "app" / "services" / "song_manager.py", "def warm_song_cache():\n    return None\n")
    _write(tmp_path / "app" / "ui" / "card.py", CARD_SRC)
    (tmp_path / "app" / "controllers").mkdir()
    _write(tmp_path / "app" / "controllers" / "dispatch.py", DISPATCH_SRC)
    _write(tmp_path / "docs" / "plugins.md", _schema_table())
    # The module map is required: modules.doc is fatal without it (a rule
    # built on a map it could not read would report every module as
    # undocumented).  Every section DOC_SECTIONS names has to appear, and
    # every module above needs a row whose "Uses" cell lists its imports -
    # otherwise this fixture would not be the clean repo it claims to be.
    _write(tmp_path / "docs" / "modules.md", _modules_doc())

    plugin = tmp_path / "integrations" / "demo"
    _write(plugin / "plugin.json", json.dumps(MANIFEST, indent=2))
    _write(plugin / "integration.py", INTEGRATION_SRC)
    _write(plugin / "flow.py", FLOW_SRC)
    (plugin / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _write(plugin / "demo-extension" / "manifest.json", json.dumps(EXTENSION_MANIFEST))
    return tmp_path


def run(repo_root: Path, disabled=()) -> list:
    ctx = tool.Context(root=repo_root)
    ctx.load()
    return tool.run_checks(ctx, disabled)


def rules_of(findings) -> set:
    return {f.rule for f in findings}


def of_rule(findings, rule_id) -> list:
    return [f for f in findings if f.rule == rule_id]


def rewrite_manifest(repo_root: Path, **changes) -> None:
    manifest = dict(MANIFEST)
    manifest.update(changes)
    _write(repo_root / "integrations" / "demo" / "plugin.json",
           json.dumps(manifest, indent=2))


# ---------------------------------------------------------------------------

class TestCleanTree:
    def test_no_findings(self, repo):
        assert run(repo) == []

    def test_every_rule_is_registered(self):
        assert set(tool.REGISTRY) == {
            "theme.key", "i18n.key", "settings.key", "manifest.schema",
            "manifest.port", "contract.flow", "contract.scrobble",
            "contract.doc", "exempt.hygiene",
            "layer.import", "modules.doc", "thread.after",
            "py.unused",
        }

    def test_severities_are_known(self):
        for definition in tool.REGISTRY.values():
            assert definition.severity in tool.SEVERITIES
            assert definition.summary and definition.source


class TestThemeKey:
    def test_flags_unknown_key(self, repo):
        _write(repo / "app" / "ui" / "card.py", CARD_SRC.replace('C["bg"]', 'C["nope"]'))
        found = of_rule(run(repo), "theme.key")
        assert [f.message for f in found if f.severity == "error"]
        assert "nope" in found[0].message
        assert found[0].line > 0 and found[0].path == "app/ui/card.py"

    def test_dynamic_index_warns_but_theme_py_is_exempt(self, repo):
        # app/utils/theme.py populates C[name] from theme.ini; the exemption
        # suppresses it, and the exemption counts as used.
        found = of_rule(run(repo), "theme.key")
        assert not [f for f in found if f.path == "app/utils/theme.py"]

    def test_dynamic_index_in_another_file_warns(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               CARD_SRC.replace('C["bg"]', 'C[key_name]'))
        found = [f for f in of_rule(run(repo), "theme.key") if f.severity == "warn"]
        assert found and "non-literal" in found[0].message


class TestI18nKey:
    def test_flags_unknown_key(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               CARD_SRC.replace('tr("app.ok")', 'tr("app.nope")'))
        found = [f for f in of_rule(run(repo), "i18n.key") if f.severity == "error"]
        assert found and "app.nope" in found[0].message

    def test_plural_base_is_satisfied_by_its_rows(self, repo):
        assert not of_rule(run(repo), "i18n.key")

    def test_dynamic_key_is_exempt_by_design(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               CARD_SRC.replace('tr("app.ok")', 'tr(f"app.{name}")'))
        assert not of_rule(run(repo), "i18n.key")


class TestSettingsKey:
    def test_flags_unknown_section(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               CARD_SRC.replace('get_setting("fullscreen")', 'get_setting("nope")'))
        found = [f for f in of_rule(run(repo), "settings.key") if f.severity == "error"]
        assert found and "nope" in found[0].message

    def test_flags_section_without_is_true(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               CARD_SRC.replace('get_setting("fullscreen")', 'get_setting("ui_scale")'))
        found = [f for f in of_rule(run(repo), "settings.key") if "'is_true'" in f.message]
        assert found

    def test_flags_unknown_option(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               CARD_SRC.replace('"ui_scale", "value"', '"ui_scale", "nope"'))
        found = [f for f in of_rule(run(repo), "settings.key") if "'nope'" in f.message]
        assert found

    def test_checks_direct_configparser_reads_in_config_py(self, repo, monkeypatch):
        # app/utils/config.py carries a shipped settings.key exemption (the
        # legacy [update_check] is_true migration), so drop it to observe the
        # rule -- the synthetic tree shares the real repo-relative paths.
        monkeypatch.delitem(tool.EXEMPTIONS["settings.key"], "app/utils/config.py")
        _write(repo / "app" / "utils" / "config.py",
               CONFIG_SRC.replace('"fullscreen", "is_true"', '"nope", "is_true"'))
        found = [f for f in of_rule(run(repo), "settings.key") if f.path.endswith("config.py")]
        assert found


class TestManifestSchema:
    def test_flags_missing_required_key(self, repo):
        manifest = {k: v for k, v in MANIFEST.items() if k != "display_name"}
        _write(repo / "integrations" / "demo" / "plugin.json", json.dumps(manifest))
        found = [f for f in of_rule(run(repo), "manifest.schema") if "display_name" in f.message]
        assert found and found[0].severity == "error"

    def test_flags_unknown_key(self, repo):
        rewrite_manifest(repo, typo="x")
        found = [f for f in of_rule(run(repo), "manifest.schema") if "typo" in f.message]
        assert found

    def test_flags_incomplete_group(self, repo):
        manifest = {k: v for k, v in MANIFEST.items() if k != "flow_class"}
        _write(repo / "integrations" / "demo" / "plugin.json", json.dumps(manifest))
        found = [f for f in of_rule(run(repo), "manifest.schema") if "flow" in f.message]
        assert any("incomplete" in f.message for f in found)

    def test_flags_extension_without_receiver(self, repo):
        rewrite_manifest(repo, flow_type="extension")
        found = [f for f in of_rule(run(repo), "manifest.schema")
                 if "receiver_class" in f.message]
        assert found and found[0].severity == "error"

    def test_flags_bool_port(self, repo):
        rewrite_manifest(repo, receiver_class="R", receiver_module="r", receiver_port=True)
        found = [f for f in of_rule(run(repo), "manifest.schema") if "receiver_port" in f.message]
        assert found

    def test_flags_out_of_range_port(self, repo):
        rewrite_manifest(repo, receiver_class="R", receiver_module="r", receiver_port=70000)
        found = [f for f in of_rule(run(repo), "manifest.schema") if "65535" in f.message]
        assert found

    def test_flags_id_directory_mismatch(self, repo):
        rewrite_manifest(repo, id="other")
        found = [f for f in of_rule(run(repo), "manifest.schema") if "directory" in f.message]
        assert found

    def test_flags_unknown_template_placeholder(self, repo):
        rewrite_manifest(repo, playlist_url_template="https://{host}/p/{id}/{nope}")
        found = [f for f in of_rule(run(repo), "manifest.schema") if "placeholder" in f.message]
        assert found

    def test_flags_auth_file_with_path_separator(self, repo):
        rewrite_manifest(repo, auth_file="sub/creds.json")
        found = [f for f in of_rule(run(repo), "manifest.schema") if "auth_file" in f.message]
        assert found

    def test_flags_fallback_escaping_repo_root(self, repo):
        rewrite_manifest(repo, auth_file_fallbacks=["../secrets.json"])
        found = [f for f in of_rule(run(repo), "manifest.schema") if "escapes" in f.message]
        assert found

    def test_flags_url_host_with_scheme(self, repo):
        rewrite_manifest(repo, url_hosts=["https://demo.com"])
        found = [f for f in of_rule(run(repo), "manifest.schema") if "bare host" in f.message]
        assert found

    def test_flags_class_absent_from_module(self, repo):
        rewrite_manifest(repo, flow_class="NoSuchFlow")
        found = [f for f in of_rule(run(repo), "manifest.schema") if "flow.py" in f.message]
        assert found

    def test_login_class_may_be_a_function(self, repo):
        _write(repo / "integrations" / "demo" / "login.py", "def show_demo_login(p, ok):\n    return ok\n")
        rewrite_manifest(repo, login_module="login", login_class="show_demo_login")
        assert not [f for f in of_rule(run(repo), "manifest.schema") if "login" in f.message]

    def test_missing_logo_warns(self, repo):
        (repo / "integrations" / "demo" / "logo.png").unlink()
        found = [f for f in of_rule(run(repo), "manifest.schema") if "logo" in f.message]
        assert found and found[0].severity == "warn"

    def test_login_logo_satisfies_the_logo_check(self, repo):
        (repo / "integrations" / "demo" / "logo.png").unlink()
        rewrite_manifest(repo, login_logo="logo.png")
        assert not [f for f in of_rule(run(repo), "manifest.schema") if "logo" in f.message]

    def test_invalid_json_is_reported_not_crashed(self, repo):
        _write(repo / "integrations" / "demo" / "plugin.json", "{nope")
        found = of_rule(run(repo), "manifest.schema")
        assert any(f.severity == "error" for f in found)


class TestManifestPort:
    def test_matching_port_is_clean(self, repo):
        rewrite_manifest(repo, receiver_class="R", receiver_module="r", receiver_port=5000)
        assert not of_rule(run(repo), "manifest.port")

    def test_flags_mismatch(self, repo):
        rewrite_manifest(repo, receiver_class="R", receiver_module="r", receiver_port=5001)
        found = of_rule(run(repo), "manifest.port")
        assert found and found[0].severity == "error" and "5000" in found[0].message

    def test_no_extension_to_verify_against_warns(self, repo):
        rewrite_manifest(repo, receiver_class="R", receiver_module="r", receiver_port=5000)
        (repo / "integrations" / "demo" / "demo-extension" / "manifest.json").unlink()
        found = of_rule(run(repo), "manifest.port")
        assert found and found[0].severity == "warn"

    def test_extension_without_localhost_permission_warns(self, repo):
        rewrite_manifest(repo, receiver_class="R", receiver_module="r", receiver_port=5000)
        _write(repo / "integrations" / "demo" / "demo-extension" / "manifest.json",
               json.dumps({"host_permissions": ["https://demo.com/*"]}))
        found = of_rule(run(repo), "manifest.port")
        assert found and found[0].severity == "warn"

    def test_no_receiver_port_is_clean(self, repo):
        assert not of_rule(run(repo), "manifest.port")


class TestContractFlow:
    def test_flags_missing_override(self, repo):
        _write(repo / "integrations" / "demo" / "integration.py",
               INTEGRATION_SRC.replace(
                   "    def authenticate(self):\n        return True\n", ""))
        found = [f for f in of_rule(run(repo), "contract.flow")
                 if "authenticate" in f.message]
        assert found and found[0].severity == "error"

    def test_flags_dropped_parameter(self, repo):
        _write(repo / "integrations" / "demo" / "integration.py",
               INTEGRATION_SRC.replace(
                   "def remove_track(self, playlist_id, track_id):",
                   "def remove_track(self, track_id):"))
        found = [f for f in of_rule(run(repo), "contract.flow")
                 if "playlist_id" in f.message]
        assert found

    def test_flags_new_required_parameter(self, repo):
        _write(repo / "integrations" / "demo" / "flow.py", FLOW_EXTRA_REQUIRED_SRC)
        found = [f for f in of_rule(run(repo), "contract.flow") if "extra" in f.message]
        assert found and found[0].severity == "error"

    def test_accepts_optional_adder_and_kwargs(self, repo):
        _write(repo / "integrations" / "demo" / "flow.py", FLOW_KWARGS_SRC)
        assert not [f for f in of_rule(run(repo), "contract.flow")
                    if "extra" in f.message]

    def test_accepts_reordering_of_keyword_dispatched_params(self, repo):
        # url/song_data/playlist_id are only ever passed by keyword, so the
        # shipped plugins are free to order them differently.
        _write(repo / "integrations" / "demo" / "flow.py",
               FLOW_SRC.replace("playlist_id=None, url=None, song_data=None,",
                                "url=None, song_data=None, playlist_id=None,"))
        assert not of_rule(run(repo), "contract.flow")

    def test_flags_reordering_of_positional_params(self, repo):
        _write(repo / "integrations" / "demo" / "flow.py",
               FLOW_SRC.replace("on_status, on_error, on_success",
                                "on_error, on_status, on_success"))
        found = [f for f in of_rule(run(repo), "contract.flow") if "reorder" in f.message]
        assert found and found[0].severity == "error"
        assert "on_status" in found[0].message

    def test_service_only_platform_may_omit_playlist_methods(self, repo):
        _write(repo / "integrations" / "demo" / "integration.py",
               "from services.integration import BaseIntegration\n\n\n"
               "class DemoIntegration(BaseIntegration):\n"
               "    supports_playlists = False\n\n"
               "    def is_authenticated(self):\n        return True\n\n"
               "    def authenticate(self):\n        return True\n\n"
               "    def refresh_auth(self):\n        return True\n")
        manifest = {k: v for k, v in MANIFEST.items()
                    if k not in ("flow_type", "flow_module", "flow_class")}
        _write(repo / "integrations" / "demo" / "plugin.json", json.dumps(manifest))
        found = of_rule(run(repo), "contract.flow")
        assert found == []

    def test_service_only_platform_implementing_playlists_warns(self, repo):
        # A class that claims no playlist support but implements the playlist
        # methods anyway: the core never calls them (dead code + a signal the
        # manifest may be wrong about).
        _write(repo / "integrations" / "demo" / "integration.py",
               "from services.integration import BaseIntegration\n\n\n"
               "class DemoIntegration(BaseIntegration):\n"
               "    supports_playlists = False\n\n"
               "    def is_authenticated(self):\n        return True\n\n"
               "    def authenticate(self):\n        return True\n\n"
               "    def refresh_auth(self):\n        return True\n\n"
               "    def get_library_playlists(self):\n        return []\n")
        found = [f for f in of_rule(run(repo), "contract.flow") if f.severity == "warn"]
        assert found and "supports_playlists" in found[0].message
        assert "get_library_playlists" in found[0].message

    def test_playlist_support_without_a_flow_errors(self, repo):
        manifest = {k: v for k, v in MANIFEST.items() if not k.startswith("flow_")}
        _write(repo / "integrations" / "demo" / "plugin.json", json.dumps(manifest))
        found = [f for f in of_rule(run(repo), "contract.flow") if "no flow" in f.message]
        assert found and found[0].severity == "error"


class TestContractScrobble:
    def test_full_capability_is_clean(self, repo):
        _write(repo / "integrations" / "demo" / "integration.py", SERVICE_ONLY_INTEGRATION_SRC)
        assert not of_rule(run(repo), "contract.scrobble")

    def test_partial_capability_is_flagged(self, repo):
        _write(repo / "integrations" / "demo" / "integration.py",
               "from services.integration import ScrobbleCapable\n\n\n"
               "class DemoIntegration(ScrobbleCapable):\n"
               "    def scrobble(self, song_data):\n        return 0\n\n"
               "    def love(self, artist, track):\n        return True\n")
        found = of_rule(run(repo), "contract.scrobble")
        assert found and "missing" in found[0].message
        assert "unlove" in found[0].message

    def test_no_capability_methods_is_clean(self, repo):
        assert not of_rule(run(repo), "contract.scrobble")

    def test_flags_capability_signature_drift(self, repo):
        _write(repo / "integrations" / "demo" / "integration.py",
               SERVICE_ONLY_INTEGRATION_SRC.replace(
                   "def unlove(self, artist, track):", "def unlove(self, track):"))
        found = [f for f in of_rule(run(repo), "contract.scrobble") if "unlove" in f.message]
        assert found


class TestContractDoc:
    def test_flags_doc_marking_a_group_unconditionally_required(self, repo):
        doc = (repo / "docs" / "plugins.md").read_text(encoding="utf-8")
        _write(repo / "docs" / "plugins.md",
               doc.replace("| `flow_type` | flow |", "| `flow_type` | yes |"))
        found = of_rule(run(repo), "contract.doc")
        assert found and "flow_type" in found[0].message

    def test_flags_undocumented_enforced_key(self, repo):
        doc = (repo / "docs" / "plugins.md").read_text(encoding="utf-8")
        dropped = [line for line in doc.splitlines() if "`receiver_port`" in line][0]
        _write(repo / "docs" / "plugins.md", doc.replace(dropped + "\n", ""))
        found = [f for f in of_rule(run(repo), "contract.doc") if "receiver_port" in f.message]
        assert found and "does not document" in found[0].message

    def test_flags_documented_key_the_schema_does_not_know(self, repo):
        doc = (repo / "docs" / "plugins.md").read_text(encoding="utf-8")
        _write(repo / "docs" / "plugins.md",
               doc.replace("| `receiver_port` |", "| `extra_key` | no | x |\n| `receiver_port` |"))
        found = [f for f in of_rule(run(repo), "contract.doc") if "extra_key" in f.message]
        assert found

    def test_ignores_other_tables_in_the_same_doc(self, repo):
        _write(repo / "docs" / "plugins.md",
               _schema_table() + "\n## Scrobble capability\n\n"
               "| Method | Returns |\n|---|---|\n| `scrobble(song_data)` | int |\n")
        assert not of_rule(run(repo), "contract.doc")

    def test_missing_table_is_a_tool_error(self, repo):
        _write(repo / "docs" / "plugins.md", "# Plugins\n\nno table here\n")
        with pytest.raises(tool.ToolError):
            run(repo)


class TestExemptions:
    def test_stale_exemption_is_reported(self, repo, monkeypatch):
        # A path the rule no longer scans (a deleted/renamed module) is the
        # realistic rot: nothing consumes the entry, so it lingers forever.
        monkeypatch.setitem(tool.EXEMPTIONS["theme.key"],
                            "app/ui/gone.py", "seeded by the test")
        found = [f for f in of_rule(run(repo), "exempt.hygiene") if "unused" in f.message]
        assert found and found[0].severity == "warn"
        assert "app/ui/gone.py" in found[0].message

    def test_absent_plugin_exemption_is_not_reported_as_stale(self, repo, monkeypatch):
        # integrations/* plugin dirs are gitignored, so a checkout that has
        # no plugin installed must not be told its per-plugin exemption is
        # rot: the rule never walked a file that is not there.
        monkeypatch.setitem(tool.EXEMPTIONS["theme.key"],
                            "integrations/absentplugin/integration.py", "seeded")
        assert not [f for f in of_rule(run(repo), "exempt.hygiene") if "unused" in f.message]

    def test_stale_plugin_exemption_is_reported_when_the_file_exists(
        self, repo, monkeypatch
    ):
        # The other half of the rule above: present-but-unflagged is still
        # reported, so the exemption cannot outlive its rule.
        plugin = repo / "integrations" / "demo"
        plugin.mkdir(parents=True, exist_ok=True)
        _write(plugin / "plugin.json",
               '{"id": "demo", "display_name": "Demo", "integration_class": "DemoIntegration"}')
        _write(plugin / "integration.py", INTEGRATION_SRC)
        monkeypatch.setitem(tool.EXEMPTIONS["theme.key"],
                            "integrations/demo/integration.py", "seeded")
        found = [f for f in of_rule(run(repo), "exempt.hygiene") if "unused" in f.message]
        assert found and "integrations/demo/integration.py" in found[0].message

    def test_exemption_without_a_reason_is_reported(self, repo, monkeypatch):
        monkeypatch.setitem(tool.EXEMPTIONS["theme.key"], "app/ui/card.py", "  ")
        found = [f for f in of_rule(run(repo), "exempt.hygiene") if "no reason" in f.message]
        assert found

    def test_exemption_for_an_unknown_rule_is_reported(self, repo, monkeypatch):
        monkeypatch.setitem(tool.EXEMPTIONS, "rule.that.was.removed", {"app/x.py": "why"})
        found = [f for f in of_rule(run(repo), "exempt.hygiene") if "unknown rule" in f.message]
        assert found and found[0].severity == "error"

    def test_exemption_suppresses_the_rule(self, repo, monkeypatch):
        _write(repo / "app" / "ui" / "card.py", CARD_SRC.replace('C["bg"]', 'C["nope"]'))
        monkeypatch.setitem(tool.EXEMPTIONS["theme.key"], "app/ui/card.py", "seeded")
        assert not of_rule(run(repo), "theme.key")

    def test_every_shipped_exemption_is_justified(self):
        for rule_id, table in tool.EXEMPTIONS.items():
            assert rule_id in tool.REGISTRY, rule_id
            for path, reason in table.items():
                assert reason.strip(), f"{rule_id} {path} has no reason"


class TestLayerImport:
    def test_clean_tree_has_no_upward_edge(self, repo):
        assert not of_rule(run(repo), "layer.import")

    def test_flags_a_static_upward_edge(self, repo):
        _write(repo / "app" / "utils" / "platform.py",
               "from services.integration import BaseIntegration\n")
        found = of_rule(run(repo), "layer.import")
        assert found and found[0].path == "app/utils/platform.py"
        assert found[0].severity == "error" and found[0].line == 1
        assert "services.integration" in found[0].message
        assert "utils" in found[0].message

    def test_flags_a_lazy_upward_edge_too(self, repo):
        # A function-level import is still an upward edge; the fix (make it
        # lazy) only helps if the rule knows it was static in the first place.
        _write(repo / "app" / "utils" / "icons.py",
               "def load():\n    from services.integration import BaseIntegration\n"
               "    return BaseIntegration\n")
        found = of_rule(run(repo), "layer.import")
        assert found and "services.integration" in found[0].message
        assert found[0].line == 2

    def test_flags_a_services_import_of_ui(self, repo):
        # services and ui are strictly ordered: only controllers<->ui is lateral.
        _write(repo / "app" / "services" / "song_manager.py",
               "from ui.card import PlaylistCard\n")
        found = of_rule(run(repo), "layer.import")
        assert found and "ui.card" in found[0].message
        assert "services" in found[0].message

    def test_controllers_to_ui_is_lateral_not_upward(self, repo):
        _write(repo / "app" / "controllers" / "dispatch.py",
               "from ui.dialogs import confirm\n")
        assert not of_rule(run(repo), "layer.import")

    def test_a_root_may_import_every_layer(self, repo):
        _write(repo / "app" / "main.py",
               "from services.integration import BaseIntegration\n"
               "from ui.card import PlaylistCard\n"
               "from utils.theme import C\n")
        assert not of_rule(run(repo), "layer.import")

    def test_a_shared_leaf_may_be_imported_from_above(self, repo):
        # plugin_loader.py / _version.py are leaves the diagram draws at or
        # below utils, so services importing them is downward.
        _write(repo / "app" / "_version.py", '__version__ = "1.0"\n')
        _write(repo / "app" / "services" / "song_manager.py",
               "from _version import __version__\n")
        assert not of_rule(run(repo), "layer.import")

    def test_a_stdlib_name_colliding_with_a_module_is_not_an_edge(self, repo):
        # `import platform` must not resolve to app/utils/platform.py.
        _write(repo / "app" / "utils" / "logging.py", "import platform\nimport os\n")
        assert not of_rule(run(repo), "layer.import")

    def test_the_shipped_exemption_covers_a_file_with_an_upward_edge(
        self, repo, monkeypatch
    ):
        # app/utils/thumbnail.py is the shape of the real one: a lazy
        # utils -> services edge, exempted with a reason.
        assert of_rule(run(repo), "layer.import") == []
        monkeypatch.setitem(tool.EXEMPTIONS["layer.import"],
                            "app/utils/config.py", "seeded")
        _write(repo / "app" / "utils" / "config.py",
               CONFIG_SRC + "\nfrom services.profile_store import auth_dir\n")
        assert not [f for f in of_rule(run(repo), "layer.import")
                    if f.path == "app/utils/config.py"]

    def test_a_shipped_exemption_goes_stale_when_the_edge_is_gone(
        self, repo, monkeypatch
    ):
        # The exemption covers the whole file, so it stays "used" while the
        # file exists; removing the file is what makes the entry rot.
        monkeypatch.setitem(tool.EXEMPTIONS["layer.import"],
                            "app/utils/gone.py", "seeded")
        found = [f for f in of_rule(run(repo), "exempt.hygiene") if "unused" in f.message]
        assert found and "app/utils/gone.py" in found[0].message
        assert found[0].severity == "warn"

    def test_an_exemption_covers_one_named_edge_not_the_file(self, repo, monkeypatch):
        # app/utils/thumbnail.py ships an exemption whose reason names the two
        # edges it covers.  A third, unnamed one is a warning, not silence.
        _write(repo / "app" / "utils" / "thumbnail.py",
               THUMBNAIL_SRC + "\nfrom services.integration import BaseIntegration\n")
        found = [f for f in of_rule(run(repo), "layer.import")
                 if "services.integration" in f.message]
        assert found and found[0].severity == "warn"
        assert "does not name it" in found[0].message

    def test_naming_the_edge_in_the_reason_silences_the_warning(
        self, repo, monkeypatch
    ):
        monkeypatch.setitem(
            tool.EXEMPTIONS["layer.import"], "app/utils/thumbnail.py",
            "services.duplicate_check, services.song_manager, services.integration (seeded)")
        _write(repo / "app" / "utils" / "thumbnail.py",
               THUMBNAIL_SRC + "\nfrom services.integration import BaseIntegration\n")
        assert not of_rule(run(repo), "layer.import")



class TestModulesDoc:
    def _doc(self, repo):
        return (repo / "docs" / "modules.md").read_text(encoding="utf-8")

    def _set_doc(self, repo, text):
        _write(repo / "docs" / "modules.md", text)

    def test_flags_an_import_the_uses_cell_omits(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               CARD_SRC + "\nfrom services.integration import BaseIntegration\n")
        found = of_rule(run(repo), "modules.doc")
        assert [f for f in found if "services.integration" in f.message]
        assert found[0].path == "app/ui/card.py" and found[0].line > 0

    def test_a_brace_group_covers_its_members(self, repo):
        # The fixture spells utils.{i18n,config,theme}; narrowing the cell to
        # one member must un-satisfy the other two, which is what proves the
        # expansion is what was satisfying them.
        _write(repo / "docs" / "modules.md",
               self._doc(repo).replace("`utils.{i18n,config,theme}`", "`utils.i18n`"))
        found = [f for f in of_rule(run(repo), "modules.doc")
                 if "utils.config" in f.message or "utils.theme" in f.message]
        assert found
        assert not [f for f in found if "utils.i18n" in f.message]

    def test_flags_a_module_with_no_row(self, repo):
        _write(repo / "app" / "utils" / "icons.py", "PASS = 1\n")
        found = of_rule(run(repo), "modules.doc")
        assert [f for f in found if "no row" in f.message]
        assert found[0].path == "app/utils/icons.py"
        assert found[0].severity == "error"

    def test_an_empty_package_marker_needs_no_row(self, repo):
        (repo / "app" / "ui" / "__init__.py").write_text("", encoding="utf-8")
        assert not [f for f in of_rule(run(repo), "modules.doc")
                    if "__init__.py" in f.path]

    def test_a_package_marker_with_content_warns(self, repo):
        (repo / "app" / "ui" / "__init__.py").write_text(
            "from .card import render\n", encoding="utf-8")
        found = [f for f in of_rule(run(repo), "modules.doc")
                 if f.path == "app/ui/__init__.py"]
        assert found and found[0].severity == "warn"

    def test_flags_a_row_for_a_module_that_is_gone(self, repo):
        self._set_doc(repo, self._doc(repo) + "\n## utils/\n\n"
                      "| File | Role | Used by | Uses |\n|---|---|---|---|\n"
                      "| `deleted.py` | role | someone | stdlib only |\n")
        found = of_rule(run(repo), "modules.doc")
        assert [f for f in found if "does not exist" in f.message]
        assert found[0].path == "docs/modules.md"

    def test_flags_a_row_with_an_unescaped_pipe(self, repo):
        text = self._doc(repo).replace(
            "| `i18n.py` | role | someone | stdlib only |",
            "| `i18n.py` | tr() returns `a | b` | someone | stdlib only |")
        self._set_doc(repo, text)
        found = [f for f in of_rule(run(repo), "modules.doc") if "4 cells" in f.message]
        assert found and found[0].path == "docs/modules.md" and found[0].line > 0

    def test_an_escaped_pipe_is_read_as_one_cell(self, repo):
        text = self._doc(repo).replace(
            "| `i18n.py` | role | someone | stdlib only |",
            "| `i18n.py` | tr() returns `a \\| b` | someone | stdlib only |")
        self._set_doc(repo, text)
        assert not [f for f in of_rule(run(repo), "modules.doc") if "4 cells" in f.message]

    def test_a_broken_row_does_not_also_report_a_missing_one(self, repo):
        # One defect, one finding: the row exists, it is just unreadable.
        text = self._doc(repo).replace(
            "| `i18n.py` | role | someone | stdlib only |",
            "| `i18n.py` | tr() returns `a | b` | someone | stdlib only |")
        self._set_doc(repo, text)
        assert not [f for f in of_rule(run(repo), "modules.doc") if "no row" in f.message]

    def test_a_type_checking_import_is_not_a_documented_edge(self, repo):
        # It annotates, it is not a runtime edge, and modules.md records the
        # one that exists by hand rather than in the "Uses" cell.  The same
        # import outside the block is a finding, so the test is not vacuous.
        guarded = (CARD_SRC + "\nfrom typing import TYPE_CHECKING\n\n"
                   "if TYPE_CHECKING:\n    from services.integration import BaseIntegration\n")
        _write(repo / "app" / "ui" / "card.py", guarded)
        assert not [f for f in of_rule(run(repo), "modules.doc")
                    if "services.integration" in f.message]
        _write(repo / "app" / "ui" / "card.py",
               guarded.replace("\nif TYPE_CHECKING:", "\nif True:"))
        assert [f for f in of_rule(run(repo), "modules.doc")
                if "services.integration" in f.message]

    def test_a_relative_import_resolves_against_app(self, repo):
        _write(repo / "app" / "ui" / "__init__.py",
               "from .card import render\n")
        (repo / "docs" / "modules.md").write_text(
            (repo / "docs" / "modules.md").read_text(encoding="utf-8").replace(
                "| `card.py` | role | someone | `utils.{i18n,config,theme}` |",
                "| `__init__.py` | role | someone | `card` |\n"
                "| `card.py` | role | someone | `utils.{i18n,config,theme}` |"),
            encoding="utf-8")
        assert not of_rule(run(repo), "modules.doc")

    def test_a_relative_import_of_a_sibling_module_is_an_edge(self, repo):
        # ``from . import card`` re-exports a name; when card is a sibling
        # *module* it is also a real edge, and modules.md has to say so.
        self._set_doc(repo, self._doc(repo).replace(
            "| `card.py` |", "| `__init__.py` | pkg | x | stdlib only |\n| `card.py` |"))
        _write(repo / "app" / "ui" / "__init__.py", "from . import card\n")
        found = of_rule(run(repo), "modules.doc")
        assert found and "ui.card" in found[0].message
        self._set_doc(repo, self._doc(repo).replace(
            "| `__init__.py` | pkg | x | stdlib only |",
            "| `__init__.py` | pkg | x | `card` |"))
        assert not of_rule(run(repo), "modules.doc")

    def test_missing_module_map_is_a_tool_error(self, repo):
        (repo / "docs" / "modules.md").unlink()
        with pytest.raises(tool.ToolError, match="module map"):
            run(repo)

    def test_renamed_section_is_a_tool_error(self, repo):
        self._set_doc(repo, self._doc(repo).replace("## utils/", "## helpers/"))
        with pytest.raises(tool.ToolError, match="renamed or removed"):
            run(repo)


class TestThreadAfter:
    def test_flags_an_unguarded_after_in_a_thread_target(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               "import threading\n\n\ndef work(root):\n"
               "    root.after(0, lambda: None)\n\n\n"
               "def go(root):\n    threading.Thread(target=work).start()\n")
        found = of_rule(run(repo), "thread.after")
        assert found and found[0].path == "app/ui/card.py"
        assert found[0].severity == "error" and found[0].line == 5
        assert "work()" in found[0].message and "_schedule_ui" in found[0].message

    def test_a_try_except_is_the_repo_guard(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               "import threading\n\n\ndef work(root):\n"
               "    try:\n        root.after(0, lambda: None)\n"
               "    except Exception:\n        pass\n\n\n"
               "def go(root):\n    threading.Thread(target=work).start()\n")
        assert not of_rule(run(repo), "thread.after")

    def test_a_main_thread_after_is_never_a_finding(self, repo):
        # Only a function handed to Thread is a worker; a sibling that merely
        # lives in the same file is not.
        _write(repo / "app" / "ui" / "card.py",
               "import threading\n\n\ndef render(root):\n"
               "    root.after(0, lambda: None)\n\n\n"
               "def go(root):\n    threading.Thread(target=render).start()\n")
        assert of_rule(run(repo), "thread.after")

    def test_a_positional_thread_target_is_tracked_too(self, repo):
        # Thread(group=None, target=None): the target is the *second*
        # positional.  Thread(work) passes work as `group` and raises at
        # start, so it is deliberately not read as a target.
        _write(repo / "app" / "ui" / "card.py",
               "import threading\n\n\ndef work(root):\n"
               "    root.after(0, lambda: None)\n\n\n"
               "def go(root):\n    threading.Thread(None, work).start()\n")
        assert of_rule(run(repo), "thread.after")

    def test_a_group_argument_is_not_a_target(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               "import threading\n\n\ndef work(root):\n"
               "    root.after(0, lambda: None)\n\n\n"
               "def go(root):\n    threading.Thread(work, daemon=True).start()\n")
        assert not of_rule(run(repo), "thread.after")

    def test_a_method_target_is_tracked_by_its_bare_name(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               "import threading\n\n\nclass Card:\n"
               "    def _load(self):\n        self.root.after(0, self._done)\n"
               "    def _done(self):\n        pass\n\n"
               "    def go(self):\n"
               "        threading.Thread(target=self._load, daemon=True).start()\n")
        found = of_rule(run(repo), "thread.after")
        assert found and found[0].line == 6

    def test_a_nested_after_inside_the_target_is_caught(self, repo):
        _write(repo / "app" / "ui" / "card.py",
               "import threading\n\n\ndef work(root):\n"
               "    def publish():\n        root.after(0, lambda: None)\n"
               "    publish()\n\n\n"
               "def go(root):\n    threading.Thread(target=work).start()\n")
        found = of_rule(run(repo), "thread.after")
        assert found and found[0].line == 6

    def test_a_lambda_target_is_not_tracked(self, repo):
        # No name to report and no second definition to confuse it with.
        _write(repo / "app" / "ui" / "card.py",
               "import threading\n\n\ndef go(root):\n"
               "    threading.Thread(target=lambda: root.after(0, print)).start()\n")
        assert not of_rule(run(repo), "thread.after")

    def test_a_file_with_no_thread_is_never_checked(self, repo):
        _write(repo / "app" / "ui" / "tooltip.py",
               "class T:\n    def show(self, root):\n        root.after(0, print)\n")
        assert not of_rule(run(repo), "thread.after")


class TestUnusedImports:
    def test_flags_a_binding_nothing_reads(self, repo):
        _write(repo / "app" / "utils" / "spare.py",
               "import os\n\nC = {}\n")
        found = of_rule(run(repo), "py.unused")
        assert found and found[0].line == 1
        assert found[0].severity == "error" and "'os'" in found[0].message

    def test_an_attribute_use_counts(self, repo):
        # ``import a.b`` binds ``a``, which appears only as the Name the
        # Attribute hangs off -- and as no node at all.
        _write(repo / "app" / "utils" / "spare.py",
               "from os import path\n\nC = {path.sep}\n")
        assert not of_rule(run(repo), "py.unused")

    def test_a_use_inside_a_function_counts(self, repo):
        _write(repo / "app" / "utils" / "spare.py",
               "import json\n\n\ndef f():\n    return json.dumps({})\n\n\nC = {}\n")
        assert not of_rule(run(repo), "py.unused")

    def test_a_future_import_is_a_directive_not_a_binding(self, repo):
        _write(repo / "app" / "utils" / "spare.py",
               "from __future__ import annotations\n\nC = {}\n")
        assert not of_rule(run(repo), "py.unused")

    def test_a_noqa_marks_a_deliberate_re_export(self, repo):
        _write(repo / "app" / "utils" / "spare.py",
               "from .config import THEME_MAP  # noqa: F401\n")
        assert not of_rule(run(repo), "py.unused")

    def test_dunder_all_counts_as_a_use(self, repo):
        _write(repo / "app" / "utils" / "spare.py",
               "from .config import load_theme\n\n__all__ = ['load_theme']\n")
        assert not of_rule(run(repo), "py.unused")

    def test_a_string_annotation_counts(self, repo):
        _write(repo / "app" / "utils" / "spare.py",
               "from services.song_manager import SongManager\n\n\n"
               "def f(x: \"SongManager\"):\n    return x\n")
        assert not of_rule(run(repo), "py.unused")

    def test_a_prose_string_does_not_count(self, repo):
        # Only annotation position is scanned: a docstring or an i18n key that
        # happens to contain the word must not mask a dead import.
        _write(repo / "app" / "utils" / "spare.py",
               "import json\n\n\n'''Writes json for the cache.'''\nC = {}\n")
        assert of_rule(run(repo), "py.unused")

    def test_a_shadowing_local_suppresses_the_finding(self, repo):
        # "Is this import read?" is unanswerable once a local reuses the
        # name, so the rule says nothing rather than guessing.
        _write(repo / "app" / "utils" / "spare.py",
               "import os\n\n\ndef f(paths):\n    for os in paths:\n        pass\n")
        assert not of_rule(run(repo), "py.unused")

    def test_a_rebound_module_is_not_reported(self, repo):
        # import logging; logging = configure() -- the import is dead, but
        # the name is genuinely in use, so a name-based scan cannot say.
        _write(repo / "app" / "utils" / "spare.py",
               "import logging\n\n\ndef configure():\n    return None\n\n"
               "logging = configure()\n")
        assert not of_rule(run(repo), "py.unused")

    def test_a_second_import_of_the_same_name_suppresses_it(self, repo):
        _write(repo / "app" / "utils" / "spare.py",
               "from .config import load_theme\nfrom .scaling import load_theme\n")
        assert not of_rule(run(repo), "py.unused")

    def test_a_star_import_is_skipped(self, repo):
        _write(repo / "app" / "utils" / "spare.py", "from .config import *\n")
        assert not of_rule(run(repo), "py.unused")

    def test_an_unparseable_file_is_one_finding_elsewhere(self, repo):
        _write(repo / "app" / "utils" / "spare.py", "import os\ndef (:\n")
        assert not of_rule(run(repo), "py.unused")

    def test_every_alias_in_one_statement_is_its_own_finding(self, repo):
        _write(repo / "app" / "utils" / "spare.py", "import os, sys\n\nC = {}\n")
        found = of_rule(run(repo), "py.unused")
        assert [f.line for f in found] == [1, 1]
        assert "'os'" in found[0].message and "'sys'" in found[1].message


class TestToolErrors:
    def test_renamed_catalog_source(self, repo):
        _write(repo / "app" / "utils" / "theme.py", "THEME_MAP = build()\n")
        with pytest.raises(tool.ToolError, match="literal"):
            run(repo)

    def test_missing_catalog_source(self, repo):
        (repo / "app" / "utils" / "i18n.py").unlink()
        with pytest.raises(tool.ToolError, match="missing"):
            run(repo)

    def test_renamed_contract_class(self, repo):
        _write(repo / "app" / "services" / "integration.py",
               CONTRACT_SRC.replace("class BaseFlowController:", "class Renamed:"))
        with pytest.raises(tool.ToolError, match="renamed or removed"):
            run(repo)

    def test_absent_integrations_dir_is_a_note_not_a_crash(self, repo):
        for child in (repo / "integrations").iterdir():
            for path in sorted(child.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            child.rmdir()
        ctx = tool.Context(root=repo)
        ctx.load()
        assert tool.run_checks(ctx) == []
        assert any("plugin rules skipped" in n for n in ctx.notes)


class TestDegradation:
    """A broken input degrades to one finding, never a traceback.

    An uncaught SyntaxError exits 1, the same code as "found something", so a
    half-typed file would look like a verdict on the repo.
    """

    def test_unparseable_app_file_is_one_finding(self, repo):
        _write(repo / "app" / "ui" / "card.py", CARD_SRC + "\n\ndef broken(\n")
        for rule_id in ("theme.key", "i18n.key", "settings.key"):
            found = [f for f in of_rule(run(repo), rule_id)
                     if f.path == "app/ui/card.py"]
            assert len(found) == 1, (rule_id, found)
            assert "does not parse" in found[0].message
            assert found[0].severity == "error"

    def test_unparseable_app_file_does_not_hide_other_files(self, repo):
        # The broken file is skipped; a *different* file's finding still lands.
        _write(repo / "app" / "ui" / "card.py", CARD_SRC + "\n\ndef broken(\n")
        _write(repo / "app" / "ui" / "panel.py",
               "from utils.theme import C\n\n\ndef render():\n    return C['nope']\n")
        found = of_rule(run(repo), "theme.key")
        assert any(f.path == "app/ui/card.py" and "does not parse" in f.message
                   for f in found)
        assert any(f.path == "app/ui/panel.py" and "nope" in f.message for f in found)

    def test_unparseable_plugin_file_does_not_cascade(self, repo):
        _write(repo / "integrations" / "demo" / "flow.py", "class DemoFlow(\n")
        found = of_rule(run(repo), "manifest.schema")
        assert [f for f in found if "does not parse" in f.message]
        # No "does not implement execute_flow()" for every base method.
        assert not [f for f in of_rule(run(repo), "contract.flow")
                    if "does not implement" in f.message]

    def test_unloadable_manifest_suppresses_the_flow_claim(self, repo):
        _write(repo / "integrations" / "demo" / "plugin.json", "{nope")
        found = of_rule(run(repo), "contract.flow")
        assert not [f for f in found if "no flow" in f.message]
        assert [f for f in of_rule(run(repo), "manifest.schema") if f.severity == "error"]

    def test_unreadable_plugin_manifest_is_reported(self, repo):
        (repo / "integrations" / "demo" / "plugin.json").unlink()
        ctx = tool.Context(root=repo)
        ctx.load()
        assert tool.run_checks(ctx) == []
        assert any("no plugin.json" in n for n in ctx.notes)

    def test_unparseable_contract_module_is_a_tool_error(self, repo):
        _write(repo / "app" / "services" / "integration.py", "class BaseIntegration(\n")
        with pytest.raises(tool.ToolError):
            run(repo)


class TestCli:
    def test_clean_repo_exits_zero(self, repo, capsys, monkeypatch):
        monkeypatch.setattr(tool, "REPO_ROOT", repo)
        assert tool.main([]) == tool.EXIT_OK

    def test_findings_exit_one(self, repo, capsys, monkeypatch):
        _write(repo / "app" / "ui" / "card.py", CARD_SRC.replace('C["bg"]', 'C["nope"]'))
        monkeypatch.setattr(tool, "REPO_ROOT", repo)
        assert tool.main([]) == tool.EXIT_FINDINGS
        assert "theme.key" in capsys.readouterr().out

    def test_quiet_prints_no_findings(self, repo, capsys, monkeypatch):
        _write(repo / "app" / "ui" / "card.py", CARD_SRC.replace('C["bg"]', 'C["nope"]'))
        monkeypatch.setattr(tool, "REPO_ROOT", repo)
        assert tool.main(["--quiet"]) == tool.EXIT_FINDINGS
        out = capsys.readouterr().out
        assert "1 error(s)" in out and "theme.key" not in out

    def test_disable_skips_a_rule(self, repo, capsys, monkeypatch):
        _write(repo / "app" / "ui" / "card.py", CARD_SRC.replace('C["bg"]', 'C["nope"]'))
        monkeypatch.setattr(tool, "REPO_ROOT", repo)
        assert tool.main(["--disable", "theme.key", "--quiet"]) == tool.EXIT_OK

    def test_unknown_rule_id_is_a_tool_error(self, repo, capsys, monkeypatch):
        monkeypatch.setattr(tool, "REPO_ROOT", repo)
        assert tool.main(["--disable", "nope.nope"]) == tool.EXIT_ERROR
        assert "unknown rule" in capsys.readouterr().err

    def test_strict_fails_on_warnings(self, repo, capsys, monkeypatch):
        (repo / "integrations" / "demo" / "logo.png").unlink()
        monkeypatch.setattr(tool, "REPO_ROOT", repo)
        assert tool.main(["--quiet"]) == tool.EXIT_OK
        assert tool.main(["--quiet", "--strict"]) == tool.EXIT_FINDINGS

    def test_list_rules(self, capsys):
        assert tool.main(["--list-rules"]) == tool.EXIT_OK
        out = capsys.readouterr().out
        for rule_id in tool.REGISTRY:
            assert rule_id in out


class TestRealRepository:
    """Dogfood: the checker passes the tree it ships with."""

    def test_repo_is_clean(self):
        ctx = tool.Context(root=REPO)
        ctx.load()
        findings = tool.run_checks(ctx)
        assert findings == [], "\n".join(f.render() for f in findings)

    def test_exemptions_are_all_consumed(self):
        ctx = tool.Context(root=REPO)
        ctx.load()
        tool.run_checks(ctx)
        expected = {(r, p) for r, table in tool.EXEMPTIONS.items() for p in table}
        assert expected <= ctx.used_exemptions, expected - ctx.used_exemptions

    def test_no_unused_exemption_entries(self):
        # A path the repo no longer has would make the entry rot silently.
        for rule_id, table in tool.EXEMPTIONS.items():
            for path in table:
                assert (REPO / path).exists(), f"{rule_id}: {path} does not exist"
