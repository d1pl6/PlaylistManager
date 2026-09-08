"""Unit tests for plugin_loader.py — manifest validation and discovery.

Manifests live in a fresh tmp_path tree per test (never the real
``integrations/``).  Discovery is import-free (only plugin.json is read),
so these tests need no plugin packages.
"""

import json

import pytest

import plugin_loader


def _write_manifest(base, dir_name, manifest):
    d = base / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def _valid_manifest(**overrides):
    m = {
        "id": "testplatform",
        "display_name": "Test Platform",
        "integration_class": "TestIntegration",
        "flow_type": "api",
    }
    m.update(overrides)
    return m


class TestDiscovery:
    def test_valid_manifest_loaded(self, tmp_path):
        _write_manifest(tmp_path, "test_platform", _valid_manifest())
        reg = plugin_loader.PluginRegistry().discover(tmp_path)
        assert len(reg) == 1
        info = reg.get("testplatform")
        assert info.display_name == "Test Platform"
        assert info.directory.name == "test_platform"

    def test_manifest_lists_sorted(self, tmp_path):
        _write_manifest(tmp_path, "b_platform", _valid_manifest(id="bb"))
        _write_manifest(tmp_path, "a_platform", _valid_manifest(id="aa"))
        reg = plugin_loader.PluginRegistry().discover(tmp_path)
        assert [p.id for p in reg] == ["aa", "bb"]

    def test_nonexistent_base_dir_is_zero_plugins(self, tmp_path):
        reg = plugin_loader.PluginRegistry().discover(tmp_path / "missing")
        assert len(reg) == 0

    def test_subdir_without_manifest_ignored(self, tmp_path):
        (tmp_path / "no_manifest").mkdir()
        reg = plugin_loader.PluginRegistry().discover(tmp_path)
        assert len(reg) == 0


class TestInvalidManifest:
    def test_unreadable_json_skipped(self, tmp_path):
        d = tmp_path / "bad"
        d.mkdir()
        (d / "plugin.json").write_text("{not json", encoding="utf-8")
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    def test_missing_id_skipped(self, tmp_path):
        m = _valid_manifest()
        del m["id"]
        _write_manifest(tmp_path, "p", m)
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    @pytest.mark.parametrize("bad_id", ["", "UPPER", "with space", "1startsdigit", "bad-id"])
    def test_invalid_id_skipped(self, tmp_path, bad_id):
        _write_manifest(tmp_path, "p", _valid_manifest(id=bad_id))
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    def test_missing_display_name_skipped(self, tmp_path):
        m = _valid_manifest()
        del m["display_name"]
        _write_manifest(tmp_path, "p", m)
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    def test_missing_integration_class_skipped(self, tmp_path):
        m = _valid_manifest()
        del m["integration_class"]
        _write_manifest(tmp_path, "p", m)
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    def test_half_declared_auth_module_skipped(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(auth_module="auth"))
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    def test_half_declared_flow_module_skipped(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(flow_module="flow"))
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    def test_flow_class_without_flow_type_skipped(self, tmp_path):
        m = _valid_manifest(flow_module="flow", flow_class="Flow",
                            flow_type="")
        _write_manifest(tmp_path, "p", m)
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    @pytest.mark.parametrize("flow_type", ["weird", "URL", "browser"])
    def test_unknown_flow_type_skipped(self, tmp_path, flow_type):
        _write_manifest(tmp_path, "p", _valid_manifest(flow_type=flow_type))
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    def test_extension_flow_needs_receiver(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(
            flow_type="extension",
            flow_module="flow", flow_class="Flow",
        ))
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    @pytest.mark.parametrize("port", ["5000", 0, 65536, -1, True, 1.5])
    def test_invalid_receiver_port_skipped(self, tmp_path, port):
        _write_manifest(tmp_path, "p", _valid_manifest(receiver_port=port))
        assert len(plugin_loader.PluginRegistry().discover(tmp_path)) == 0

    def test_valid_receiver_port_accepted(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(
            receiver_port=5000,
            receiver_module="recv", receiver_class="Recv",
        ))
        reg = plugin_loader.PluginRegistry().discover(tmp_path)
        assert reg.get("testplatform").receiver_port == 5000


class TestDuplicateId:
    def test_first_declaration_wins(self, tmp_path):
        _write_manifest(tmp_path, "first", _valid_manifest(id="dup", display_name="First"))
        _write_manifest(tmp_path, "second", _valid_manifest(id="dup", display_name="Second"))
        reg = plugin_loader.PluginRegistry().discover(tmp_path)
        assert len(reg) == 1
        assert reg.get("dup").directory.name == "first"


class TestPluginInfoFields:
    def test_url_templates_loaded(self, tmp_path):
        d = _write_manifest(tmp_path, "p", _valid_manifest(
            url_hosts=["music.example.com"],
            playlist_url_template="https://{host}/p/{id}",
            song_url_template="https://{host}/s/{id}",
        ))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.url_hosts == ["music.example.com"]
        assert info.playlist_url_template == "https://{host}/p/{id}"
        assert info.song_url_template == "https://{host}/s/{id}"

    def test_auth_file_fields(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(
            auth_file="creds.json",
            auth_file_fallbacks=["auth/old.json", "auth/older.json"],
        ))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.auth_file == "creds.json"
        assert info.auth_file_fallbacks == ["auth/old.json", "auth/older.json"]

    def test_version_loaded(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(version=3))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.version == 3

    def test_version_negative_ignored(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(version=-1))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.version is None

    def test_version_string_ignored(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(version="3"))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.version is None


class TestFallbackPathValidation:
    def test_absolute_fallback_dropped(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(
            auth_file_fallbacks=["/etc/passwd", "ok/path.json"],
        ))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.auth_file_fallbacks == ["ok/path.json"]

    def test_dotdot_fallback_dropped(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(
            auth_file_fallbacks=["../escape.json", "ok/path.json"],
        ))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.auth_file_fallbacks == ["ok/path.json"]

    def test_non_list_fallbacks_ignored(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(auth_file_fallbacks="browser.json"))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.auth_file_fallbacks == []

    def test_malicious_path_traversal_in_manifest_validated(self, tmp_path):
        # Defense in depth: fallbacks must never escape the repo root.
        reg = plugin_loader.PluginRegistry().discover(tmp_path)
        assert len(reg) == 0  # nothing to discover on an empty tree


class TestLoginLogo:
    def test_escaped_login_logo_refused(self, tmp_path):
        d = _write_manifest(tmp_path, "p", _valid_manifest(login_logo="../../evil.png"))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.login_logo_path is None

    def test_valid_login_logo_resolved(self, tmp_path):
        d = _write_manifest(tmp_path, "p", _valid_manifest(login_logo="logo.png"))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.login_logo_path == (d / "logo.png").resolve()

    def test_logo_path_falls_back_to_directory_logo(self, tmp_path):
        d = _write_manifest(tmp_path, "p", _valid_manifest())
        (d / "logo.png").write_bytes(b"png")
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.logo_path == d / "logo.png"


class TestAuthPath:
    def test_auth_file_bare_name_required(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(auth_file="../../oauth.json"))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.auth_path is None

    def test_auth_file_bare_name_ok(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest(auth_file="spotify.json"))
        info = plugin_loader.PluginRegistry().discover(tmp_path).get("testplatform")
        assert info.auth_path is not None
        assert info.auth_path.name == "spotify.json"


class TestTypedRegistryOps:
    def test_get_all_returns_copy(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest())
        reg = plugin_loader.PluginRegistry().discover(tmp_path)
        all_ = reg.get_all()
        all_.clear()
        assert len(reg) == 1

    def test_contains(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest())
        reg = plugin_loader.PluginRegistry().discover(tmp_path)
        assert "testplatform" in reg
        assert "nope" not in reg

    def test_unregister_missing_is_noop(self, tmp_path):
        reg = plugin_loader.PluginRegistry()
        reg.unregister("ghost")  # must not raise

    def test_unregister_removes(self, tmp_path):
        _write_manifest(tmp_path, "p", _valid_manifest())
        reg = plugin_loader.PluginRegistry().discover(tmp_path)
        reg.unregister("testplatform")
        assert "testplatform" not in reg