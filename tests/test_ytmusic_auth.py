"""YT Music manual-headers auth + profile-aware resolution + cache tests.

Covers the plugin's ``yt_music.json`` save/load/fallback handling, the
profile-aware auth-dir resolver with legacy-shared migration, the app-side
``auth_setup.save_and_verify_ytmusic_auth`` VERIFY-FIRST contract, and the
flow's playlist-id negative cache.  The ytmusicapi round trip is stubbed
(never touches the network); the plugin's credential paths are
monkeypatched to a per-test temp tree (the plugin resolves through the
app's profile_store, which the test conftest redirects, but a per-test
patch keeps tests isolated from each other).  The whole module skips when
the youtube_music plugin repo is not present (gitignored plugin dirs are
absent on fresh clones).
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
    legacy_auth = tmp_path / "legacy_shared" / "auth"
    monkeypatch.setattr(mod, "_resolve_auth_dir", lambda: auth)
    monkeypatch.setattr(
        mod,
        "_legacy_shared_auth_file",
        lambda name: legacy_auth / name,
    )
    monkeypatch.setattr(
        mod, "BROWSER_FILE_FALLBACKS", [tmp_path / "repo/browser.json"]
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
        path = plugin._yt_music_file()
        assert plugin.save_ytmusic_headers_file(HEADERS) == path
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600
        assert plugin.load_ytmusic_headers_file() == HEADERS

    def test_load_none_when_missing(self, plugin):
        assert plugin.load_ytmusic_headers_file() is None

    def test_yt_music_file_has_priority_over_browser(self, plugin):
        plugin._browser_file().write_text(
            json.dumps({"Cookie": "old"}), encoding="utf-8"
        )
        plugin.save_ytmusic_headers_file(HEADERS)
        assert plugin.load_ytmusic_headers_file() == HEADERS

    def test_browser_file_fallback_when_yt_file_absent(self, plugin):
        plugin._browser_file().write_text(json.dumps(HEADERS), encoding="utf-8")
        assert plugin.load_ytmusic_headers_file() == HEADERS

    def test_legacy_fallback_copy(self, plugin):
        legacy = plugin.BROWSER_FILE_FALLBACKS[0]
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(HEADERS), encoding="utf-8")
        assert plugin.load_ytmusic_headers_file() == HEADERS

    def test_load_chmods_loose_file_to_0600(self, plugin):
        plugin._browser_file().write_text(json.dumps(HEADERS), encoding="utf-8")
        plugin._browser_file().chmod(0o644)
        plugin.load_ytmusic_headers_file()
        assert plugin._browser_file().stat().st_mode & 0o777 == 0o600

    def test_find_browser_file_priority(self, plugin):
        from integrations.youtube_music.youtube_music import YouTubeAuthManager

        plugin._yt_music_file().write_text(json.dumps(HEADERS), encoding="utf-8")
        plugin._browser_file().write_text("{}", encoding="utf-8")
        mgr = YouTubeAuthManager()
        assert mgr._find_browser_file() == plugin._yt_music_file()
        plugin._yt_music_file().unlink()
        assert mgr._find_browser_file() == plugin._browser_file()
        plugin._browser_file().unlink()
        assert mgr._find_browser_file() is None


class TestProfileAwareResolution:
    def test_save_lands_in_profile_dir(self, plugin):
        plugin.save_ytmusic_headers_file(HEADERS)
        assert plugin._yt_music_file().exists()
        assert not plugin._legacy_shared_auth_file("yt_music.json").exists()

    def test_legacy_yt_file_migrated_on_load(self, plugin):
        legacy_yt = plugin._legacy_shared_auth_file("yt_music.json")
        legacy_yt.parent.mkdir(parents=True)
        legacy_yt.write_text(json.dumps(HEADERS), encoding="utf-8")
        assert plugin.load_ytmusic_headers_file() == HEADERS
        assert plugin._yt_music_file().exists()
        assert not legacy_yt.exists()

    def test_legacy_browser_file_migrated_on_load(self, plugin):
        legacy = plugin._legacy_shared_auth_file("browser.json")
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(HEADERS), encoding="utf-8")
        assert plugin.load_ytmusic_headers_file() == HEADERS
        assert plugin._browser_file().exists()
        assert not legacy.exists()

    def test_migration_never_overwrites_profile_copy(self, plugin):
        plugin._yt_music_file().write_text(
            json.dumps({"A": 1}), encoding="utf-8"
        )
        legacy = plugin._legacy_shared_auth_file("yt_music.json")
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(HEADERS), encoding="utf-8")
        assert plugin.load_ytmusic_headers_file() == {"A": 1}
        assert legacy.exists()

    def test_browser_file_ready_mirrors_plugin_candidates(self, plugin):
        assert auth_setup.browser_file_ready() is False
        plugin._yt_music_file().write_text(json.dumps(HEADERS), encoding="utf-8")
        assert auth_setup.browser_file_ready() is True
        plugin._yt_music_file().unlink()
        plugin._browser_file().write_text(json.dumps(HEADERS), encoding="utf-8")
        assert auth_setup.browser_file_ready() is True


class TestSaveAndVerify:
    def test_writes_file_only_after_success(self, fake_ytmusic, plugin):
        result = auth_setup.save_and_verify_ytmusic_auth("AUTHZ", "COOKIE")
        assert result.get("ok") is True
        assert "error" not in result
        assert plugin._yt_music_file().exists()
        assert plugin._yt_music_file().stat().st_mode & 0o777 == 0o600
        saved = json.loads(plugin._yt_music_file().read_text(encoding="utf-8"))
        assert saved["Authorization"] == "AUTHZ"
        assert saved["Cookie"] == "COOKIE"
        assert saved["x-origin"] == "https://music.youtube.com"
        assert saved["X-Goog-AuthUser"] == "0"

    def test_no_write_on_verify_failure(self, fake_ytmusic, plugin):
        _FakeYTMusic.fail_auth = True
        result = auth_setup.save_and_verify_ytmusic_auth("BAD", "BAD")
        assert result.get("ok") is False
        assert "error" in result
        assert not plugin._yt_music_file().exists()

    def test_empty_fields_rejected(self, plugin):
        result = auth_setup.save_and_verify_ytmusic_auth("  ", "x")
        assert result.get("ok") is False
        assert not plugin._yt_music_file().exists()


class _ScanClient:
    """Stands in for a ytmusicapi client; counts full-library scans."""

    def __init__(self):
        self.scans = 0

    def get_library_playlists(self, limit=None):
        self.scans += 1
        return []


class TestBoundedSession:
    def test_requests_default_to_bounded_timeout(self, monkeypatch):
        """The runtime client's session must cap per-call timeouts (the
        flow's ytmusicapi default is a 30 s stall per call)."""
        mod = _plugin_mod()
        import requests as requests_mod

        seen = {}

        def fake_request(self, *args, **kwargs):
            seen["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(requests_mod.Session, "request", fake_request)
        session = mod._bounded_session(timeout=7)

        # No explicit timeout -> the bounded default is injected.
        session.request("GET", "http://example.invalid")
        assert seen["kwargs"].get("timeout") == 7

        # An explicit per-call timeout still wins.
        session.request("GET", "http://example.invalid", timeout=3)
        assert seen["kwargs"].get("timeout") == 3


class TestFlowPlaylistIdCache:
    def test_negative_result_cached_no_rescan(self):
        import integrations.youtube_music.flow as flow_mod

        client = _ScanClient()
        flow = flow_mod.YouTubeMusicFlow(
            SimpleNamespace(yt_client=client),
            song_manager=object(),
            url_receiver=None,
        )
        assert flow._get_playlist_id("Missing Playlist") is None
        assert flow._get_playlist_id("Missing Playlist") is None
        assert client.scans == 1

    def test_known_id_cached_without_scan(self):
        import integrations.youtube_music.flow as flow_mod

        client = _ScanClient()
        flow = flow_mod.YouTubeMusicFlow(
            SimpleNamespace(yt_client=client),
            song_manager=object(),
            url_receiver=None,
        )
        assert flow._get_playlist_id("Some Playlist", "PL123") == "PL123"
        assert flow._get_playlist_id("Some Playlist", "PL123") == "PL123"
        assert client.scans == 0


# A grid-less response the real ytmusicapi library parser accepts as a
# valid (zero-playlist) library answer.
_EMPTY_LIBRARY_RESPONSE = {
    "contents": {
        "singleColumnBrowseResultsRenderer": {
            "tabs": [
                {"tabRenderer": {"content": {"sectionListRenderer": {"contents": []}}}}
            ]
        }
    }
}


class _FakeLibraryClient:
    """Stands in for a ytmusicapi instance; routes browse requests by id."""

    def __init__(self, fail_browse_ids=()):
        self.fail_browse_ids = set(fail_browse_ids)
        self.requested = []

    def _check_auth(self):
        pass

    def _send_request(self, endpoint, body, additionalParams=""):
        browse_id = body["browseId"]
        self.requested.append(browse_id)
        if browse_id in self.fail_browse_ids:
            raise RuntimeError("Server returned HTTP 400: Bad Request.")
        return _EMPTY_LIBRARY_RESPONSE


class TestLibraryFetchFallback:
    """The library-fetch fallback must tolerate a dead browse endpoint.

    Regression: ``FEmusic_library_playlists`` returns HTTP 400 on the
    current YT Music backend.  The old fallback probed it first and
    re-raised its 400 whenever another browse legitimately returned zero
    playlists, so every re-auth (``_setup_browser_auth`` verifies auth via
    ``get_library_playlists``) and library scan died with that 400 and
    freshly pasted credentials never took effect.
    """

    def test_live_browse_ids_only(self):
        """The dead id is gone; only the id current ytmusicapi uses remains."""
        mod = _plugin_mod()
        assert mod._LIBRARY_BROWSE_IDS == ("FEmusic_liked_playlists",)
        assert "FEmusic_library_playlists" not in mod._LIBRARY_BROWSE_IDS

    def test_one_browse_failed_one_empty_returns_empty_not_error(self, monkeypatch):
        pytest.importorskip("ytmusicapi")
        mod = _plugin_mod()
        monkeypatch.setattr(
            mod,
            "_LIBRARY_BROWSE_IDS",
            ("FEmusic_library_playlists", "FEmusic_liked_playlists"),
        )
        client = _FakeLibraryClient(fail_browse_ids={"FEmusic_library_playlists"})

        result = mod._patched_get_library_playlists(client, limit=5)

        # The dead browse 400s but the empty liked browse is a valid answer:
        # nothing may be raised over it - this was the reported failure.
        assert result == []
        assert "FEmusic_liked_playlists" in client.requested

    def test_all_browses_failed_still_raises(self, monkeypatch):
        pytest.importorskip("ytmusicapi")
        mod = _plugin_mod()
        monkeypatch.setattr(mod, "_LIBRARY_BROWSE_IDS", ("FEmusic_liked_playlists",))
        client = _FakeLibraryClient(fail_browse_ids={"FEmusic_liked_playlists"})

        with pytest.raises(RuntimeError, match="HTTP 400"):
            mod._patched_get_library_playlists(client, limit=5)