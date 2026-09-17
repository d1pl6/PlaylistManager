"""YT Music manual-headers auth tests.

Covers the plugin's ``yt_music.json`` save/load/fallback handling plus the
app-side ``auth_setup.save_and_verify_ytmusic_auth`` VERIFY-FIRST contract.
The ytmusicapi round trip is stubbed (never touches the network); the
plugin's credential paths are monkeypatched to a temp tree (the plugin
hardcodes the real platformdirs auth dir, which the test conftest does not
redirect).  The whole module skips when the youtube_music plugin repo is
not present (gitignored plugin dirs are absent on fresh clones).
"""

import json
import sys
from types import SimpleNamespace

import pytest

from services import auth_setup

HEADERS = {
    "Accept": "*/*",
    "Authorization": "PASTE_AUTHORIZATION",
    "Content-Type": "application/json",
    "X-Goog-AuthUser": "0",
    "x-origin": "https://music.youtube.com",
    "Cookie": "PASTE_COOKIE",
}


def _plugin_mod():
    try:
        import integrations.youtube_music.youtube_music as mod
    except ImportError:
        pytest.skip("youtube_music plugin not installed in this checkout")
    return mod


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    mod = _plugin_mod()
    auth = tmp_path / "auth"
    auth.mkdir()
    monkeypatch.setattr(mod, "AUTH_FOLDER", auth)
    monkeypatch.setattr(mod, "YT_MUSIC_FILE", auth / "yt_music.json")
    monkeypatch.setattr(mod, "BROWSER_FILE", auth / "browser.json")
    monkeypatch.setattr(
        mod, "BROWSER_FILE_FALLBACKS", [auth / "repo/browser.json"]
    )
    return mod


class _FakeYTMusic:
    """Stands in for ytmusicapi.YTMusic; records constructor args and can
    be told to fail the authenticated round trip."""

    fail_auth = False
    seen = []

    def __init__(self, headers, requests_session=None):
        _FakeYTMusic.seen.append((dict(headers), requests_session))

    def get_library_playlists(self, limit=25):
        if _FakeYTMusic.fail_auth:
            raise RuntimeError("Server returned HTTP 401: Unauthorized")


@pytest.fixture
def fake_ytmusic(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "ytmusicapi", SimpleNamespace(YTMusic=_FakeYTMusic)
    )
    _FakeYTMusic.seen.clear()
    _FakeYTMusic.fail_auth = False


class TestPluginFiles:
    def test_save_and_load_round_trip(self, plugin):
        path = plugin.YT_MUSIC_FILE
        assert plugin.save_ytmusic_headers_file(HEADERS) == path
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600
        assert plugin.load_ytmusic_headers_file() == HEADERS

    def test_load_none_when_missing(self, plugin):
        assert plugin.load_ytmusic_headers_file() is None

    def test_yt_music_file_has_priority_over_browser(self, plugin):
        plugin.BROWSER_FILE.write_text(
            json.dumps({"Cookie": "old"}), encoding="utf-8"
        )
        plugin.save_ytmusic_headers_file(HEADERS)
        assert plugin.load_ytmusic_headers_file() == HEADERS

    def test_browser_file_fallback_when_yt_file_absent(self, plugin):
        plugin.BROWSER_FILE.write_text(json.dumps(HEADERS), encoding="utf-8")
        assert plugin.load_ytmusic_headers_file() == HEADERS

    def test_legacy_fallback_copy(self, plugin):
        legacy = plugin.BROWSER_FILE_FALLBACKS[0]
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(HEADERS), encoding="utf-8")
        assert plugin.load_ytmusic_headers_file() == HEADERS

    def test_load_chmods_loose_file_to_0600(self, plugin):
        plugin.BROWSER_FILE.write_text(json.dumps(HEADERS), encoding="utf-8")
        plugin.BROWSER_FILE.chmod(0o644)
        plugin.load_ytmusic_headers_file()
        assert plugin.BROWSER_FILE.stat().st_mode & 0o777 == 0o600

    def test_find_browser_file_priority(self, plugin):
        from integrations.youtube_music.youtube_music import YouTubeAuthManager

        plugin.YT_MUSIC_FILE.write_text(json.dumps(HEADERS), encoding="utf-8")
        plugin.BROWSER_FILE.write_text("{}", encoding="utf-8")
        mgr = YouTubeAuthManager()
        assert mgr._find_browser_file() == plugin.YT_MUSIC_FILE
        plugin.YT_MUSIC_FILE.unlink()
        assert mgr._find_browser_file() == plugin.BROWSER_FILE
        plugin.BROWSER_FILE.unlink()
        assert mgr._find_browser_file() is None


class TestSaveAndVerify:
    def test_writes_file_only_after_success(self, fake_ytmusic, plugin):
        result = auth_setup.save_and_verify_ytmusic_auth("AUTHZ", "COOKIE")
        assert result.get("ok") is True
        assert "error" not in result
        assert plugin.YT_MUSIC_FILE.exists()
        assert plugin.YT_MUSIC_FILE.stat().st_mode & 0o777 == 0o600
        saved = json.loads(plugin.YT_MUSIC_FILE.read_text(encoding="utf-8"))
        assert saved["Authorization"] == "AUTHZ"
        assert saved["Cookie"] == "COOKIE"
        assert saved["x-origin"] == "https://music.youtube.com"
        assert saved["X-Goog-AuthUser"] == "0"

    def test_no_write_on_verify_failure(self, fake_ytmusic, plugin):
        _FakeYTMusic.fail_auth = True
        result = auth_setup.save_and_verify_ytmusic_auth("BAD", "BAD")
        assert result.get("ok") is False
        assert "error" in result
        assert not plugin.YT_MUSIC_FILE.exists()

    def test_empty_fields_rejected(self, plugin):
        result = auth_setup.save_and_verify_ytmusic_auth("  ", "x")
        assert result.get("ok") is False
        assert not plugin.YT_MUSIC_FILE.exists()