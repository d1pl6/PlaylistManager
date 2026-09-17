"""Spotify plugin tests: profile-aware credential resolution + migration,
the narrowed _request exception contract (transport failures map to None,
auth expiry propagates), the 429 Retry-After guard with capped backoff, the
50-id like_track chunk cap, and full get_playlist_tracks pagination with the
episode filter."""

import json
import time
from types import SimpleNamespace

import pytest
import requests

from services import auth_setup


def _plugin_mod():
    try:
        import integrations.spotify.spotify as m
    except ImportError:
        pytest.skip("spotify plugin not installed in this checkout")
    return m


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    mod = _plugin_mod()
    # Isolate the resolver in a per-test dir (the conftest temp tree is
    # shared across the whole session and would leak files between tests).
    auth = tmp_path / "auth"

    def resolve():
        auth.mkdir(parents=True, exist_ok=True, mode=0o700)
        return auth

    monkeypatch.setattr(mod, "_resolve_auth_dir", resolve)
    legacy = tmp_path / "shared_gone" / "spotify.json"
    monkeypatch.setattr(mod, "_legacy_shared_auth_file", staticmethod(lambda: legacy))
    return SimpleNamespace(mod=mod, legacy=legacy)


def _creds():
    return {
        "client_id": "CID",
        "client_secret": "SECRET",
        "refresh_token": "RT",
    }


class TestCredentialFiles:
    def test_save_and_load_round_trip(self, plugin):
        mod = plugin.mod
        file = mod._spotify_auth_file()
        mod.save_spotify_credentials_file(**_creds())
        assert file.exists()
        assert file.stat().st_mode & 0o777 == 0o600
        assert mod.load_spotify_credentials() == _creds()

    def test_save_through_auth_setup_writes_same_file(self, plugin):
        # The app-side save delegates to the plugin's single writer, which
        # must land on the profile-aware resolver path.
        auth_setup.save_spotify_credentials(**_creds())
        assert plugin.mod._spotify_auth_file().exists()

    def test_load_missing_returns_none(self, plugin):
        assert plugin.mod.load_spotify_credentials() is None

    def test_load_migrates_legacy_shared_file(self, plugin):
        mod = plugin.mod
        target = mod._spotify_auth_file()
        assert not target.exists()
        plugin.legacy.parent.mkdir(parents=True)
        plugin.legacy.write_text(json.dumps(_creds()), encoding="utf-8")
        assert mod.load_spotify_credentials() == _creds()
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == _creds()
        assert not plugin.legacy.exists()

    def test_setup_auth_absent_returns_false(self, plugin, monkeypatch):
        mod = plugin.mod
        # No file anywhere: must return False without a network round trip.
        assert mod.SpotifyAuthManager().setup_auth() is False

    def test_setup_auth_consumes_migrated_credentials(self, plugin, monkeypatch):
        # setup_auth reads through the plugin's load (profile-aware + one-time
        # migration); the /v1/me round trip is patched out.
        mod = plugin.mod
        plugin.legacy.parent.mkdir(parents=True)
        plugin.legacy.write_text(json.dumps(_creds()), encoding="utf-8")
        me = {"id": "u1", "display_name": "Test User"}
        monkeypatch.setattr(mod.SpotifyAPI, "get_me", lambda self: me)

        manager = mod.SpotifyAuthManager()
        assert manager.setup_auth() is True
        assert manager.get_api() is not None
        assert manager.get_api().client_id == "CID"
        assert plugin.legacy.exists() is False  # moved into the profile slot


def _resp(status, headers=None, body=b"{}"):
    r = requests.models.Response()
    r.status_code = status
    r.headers = headers or {}
    r._content = body
    return r


class TestRequestErrorContract:
    def _api(self, monkeypatch):
        mod = _plugin_mod()
        api = mod.SpotifyAPI("CID", "SECRET", "RT")
        # Skip token refresh: pre-set a valid access token.
        api._access_token = "tok"
        api._token_expires = time.time() + 3600
        return api

    def test_transport_error_maps_to_none(self, monkeypatch):
        api = self._api(monkeypatch)

        def boom(method, url, headers=None, **kwargs):
            raise requests.ConnectionError("network down")

        monkeypatch.setattr(api._session, "request", boom)
        assert api._request("GET", "/me") is None

    def test_auth_failure_propagates(self, monkeypatch):
        # A dead refresh token must NOT be swallowed as "API request failed":
        # _get_headers raises RuntimeError once _refresh_access_token fails,
        # and callers distinguish it from a transient transport failure.
        mod = _plugin_mod()
        api = mod.SpotifyAPI("CID", "SECRET", "RT")
        monkeypatch.setattr(api, "_refresh_access_token", lambda: False)
        with pytest.raises(RuntimeError):
            api._request("GET", "/me")


class TestRetryAfterGuard:
    def _api(self, monkeypatch):
        mod = _plugin_mod()
        api = mod.SpotifyAPI("CID", "SECRET", "RT")
        api._access_token = "tok"
        api._token_expires = time.time() + 3600
        return api

    def test_non_numeric_retry_after_falls_back_and_retries(self, monkeypatch):
        api = self._api(monkeypatch)
        responses = iter(
            [
                _resp(429, {"Retry-After": "Wed, 21 Oct 2050 07:28:00 GMT"}),
                _resp(200),
            ]
        )
        calls = []
        sleeps = []

        def fake_request(method, url, headers=None, **kwargs):
            calls.append(url)
            return next(responses)

        def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(api._session, "request", fake_request)
        monkeypatch.setattr(_plugin_mod().time, "sleep", fake_sleep)

        data = api._request("GET", "/me")
        assert data == {}
        assert len(calls) == 2  # 429 -> backoff -> retry
        assert sleeps == [1]  # non-numeric Retry-After fell back to 1s

    def test_numeric_retry_after_respected(self, monkeypatch):
        api = self._api(monkeypatch)
        responses = iter([_resp(429, {"Retry-After": "3"}), _resp(200)])
        sleeps = []

        def fake_request(method, url, headers=None, **kwargs):
            return next(responses)

        def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(api._session, "request", fake_request)
        monkeypatch.setattr(_plugin_mod().time, "sleep", fake_sleep)

        assert api._request("GET", "/me") == {}
        assert sleeps == [3]

    def test_backoff_capped_at_five(self, monkeypatch):
        # An uncapped wait would freeze the CLI thread for the whole period.
        api = self._api(monkeypatch)
        responses = iter([_resp(429, {"Retry-After": "60"}), _resp(200)])
        sleeps = []

        def fake_request(method, url, headers=None, **kwargs):
            return next(responses)

        def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(api._session, "request", fake_request)
        monkeypatch.setattr(_plugin_mod().time, "sleep", fake_sleep)

        assert api._request("GET", "/me") == {}
        assert sleeps == [5]


class TestLikeTrackChunking:
    def test_chunks_at_fifty(self, monkeypatch):
        # Spotify caps PUT /me/tracks at 50 ids per request; 120 ids must
        # split into 50/50/20 instead of one oversized 100+ request.
        mod = _plugin_mod()
        api = mod.SpotifyAPI("CID", "SECRET", "RT")
        chunks = []

        def fake_request(method, endpoint, **kwargs):
            chunks.append(kwargs["json"]["ids"])
            return {}

        monkeypatch.setattr(api, "_request", fake_request)
        assert api.like_track([f"id{i}" for i in range(120)]) is True
        assert [len(c) for c in chunks] == [50, 50, 20]

    def test_empty_batch_returns_true(self, monkeypatch):
        mod = _plugin_mod()
        api = mod.SpotifyAPI("CID", "SECRET", "RT")
        chunks = []

        def fake_request(method, endpoint, **kwargs):
            chunks.append(kwargs["json"]["ids"])
            return {}

        monkeypatch.setattr(api, "_request", fake_request)
        assert api.like_track([]) is True
        assert chunks == []


class TestGetPlaylistTracksPagination:
    def test_follows_all_pages_and_filters_episodes(self, monkeypatch):
        mod = _plugin_mod()
        api = mod.SpotifyAPI("CID", "SECRET", "RT")
        pages = iter(
            [
                {
                    "items": [
                        {"track": {"id": "t1", "type": "track"}},
                        {"track": {"id": "e1", "type": "episode"}},
                    ],
                    "next": "https://api.spotify.com/v1/playlists/5/tracks?offset=100",
                },
                {
                    "items": [{"track": {"id": "t2", "type": "track"}}],
                    "next": None,
                },
            ]
        )
        calls = []

        def fake_request(method, endpoint, **kwargs):
            calls.append(endpoint)
            return next(pages)

        monkeypatch.setattr(api, "_request", fake_request)
        tracks = api.get_playlist_tracks("5")
        assert [t["id"] for t in tracks] == ["t1", "t2"]  # episode dropped
        assert calls == [
            "/playlists/5/tracks",
            "/playlists/5/tracks?offset=100",  # next stripped of the base URL
        ]

    def test_first_page_failure_returns_empty(self, monkeypatch):
        mod = _plugin_mod()
        api = mod.SpotifyAPI("CID", "SECRET", "RT")
        monkeypatch.setattr(api, "_request", lambda *a, **k: None)
        assert api.get_playlist_tracks("5") == []