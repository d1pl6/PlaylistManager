"""SoundCloud plugin tests: profile-aware credential resolution + migration,
the delete contract (absent vs failed must be distinguishable), full
pagination of get_playlist_tracks (the fetch-modify-write add/remove paths
rewrite the whole array from this result), and the Retry-After guard on the
429 backoff."""

import json
import time
from types import SimpleNamespace

import pytest
import requests

from services import auth_setup


def _plugin_mod():
    try:
        import integrations.soundcloud.soundcloud as m
    except ImportError:
        pytest.skip("soundcloud plugin not installed in this checkout")
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
    legacy = tmp_path / "shared_gone" / "soundcloud.json"
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
        file = mod._soundcloud_auth_file()
        mod.save_soundcloud_credentials_file(**_creds())
        assert file.exists()
        assert file.stat().st_mode & 0o777 == 0o600
        assert mod.load_soundcloud_credentials() == _creds()

    def test_load_missing_returns_none(self, plugin):
        assert plugin.mod.load_soundcloud_credentials() is None

    def test_load_migrates_legacy_shared_file(self, plugin, tmp_path):
        mod = plugin.mod
        target = mod._soundcloud_auth_file()
        assert not target.exists()
        plugin.legacy.parent.mkdir(parents=True)
        plugin.legacy.write_text(json.dumps(_creds()), encoding="utf-8")
        assert mod.load_soundcloud_credentials() == _creds()
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == _creds()
        assert not plugin.legacy.exists()

    def test_delete_absent_returns_false(self, plugin):
        assert plugin.mod.delete_soundcloud_credentials() is False

    def test_delete_removes_file(self, plugin):
        mod = plugin.mod
        mod.save_soundcloud_credentials_file(**_creds())
        assert mod.delete_soundcloud_credentials() is True
        assert not mod._soundcloud_auth_file().exists()

    def test_delete_raises_on_unlink_failure(self, plugin):
        # A failed unlink must RAISE (distinguishable from "absent") so the
        # login dialog shows delete_failed instead of "no saved credentials".
        target = plugin.mod._soundcloud_auth_file()
        target.mkdir(parents=True)  # a directory: exists() but unlink() fails
        with pytest.raises(OSError):
            plugin.mod.delete_soundcloud_credentials()

    def test_delete_raises_propagates_through_auth_setup(self, plugin):
        # The app-side wrapper must pass OSError through (only ImportError
        # is caught) so the tile's except path reports the real failure.
        target = plugin.mod._soundcloud_auth_file()
        target.mkdir(parents=True)
        with pytest.raises(OSError):
            auth_setup.delete_soundcloud_credentials()


def _page(items, next_href=""):
    data = {"collection": items}
    if next_href:
        data["next_href"] = next_href
    return data


def _track(i):
    return {"id": i, "title": f"Track {i}"}


class TestGetPlaylistTracksPagination:
    def _api(self, monkeypatch, pages):
        mod = _plugin_mod()
        api = mod.SoundCloudAPI("CID", "SECRET", "RT")
        calls = []

        def fake_request(method, endpoint, **kwargs):
            calls.append(endpoint)
            return pages.pop(0)

        def fake_absolute(method, url, **kwargs):
            calls.append(url)
            return pages.pop(0)

        monkeypatch.setattr(api, "_request", fake_request)
        monkeypatch.setattr(api, "_request_absolute", fake_absolute)
        return api, calls

    def test_follows_all_pages(self, monkeypatch):
        api, calls = self._api(
            monkeypatch,
            [
                _page([_track(1), _track(2)], "https://api.soundcloud.com/playlists/5/tracks?offset=2"),
                _page([_track(3)], "https://api.soundcloud.com/playlists/5/tracks?offset=3"),
                _page([_track(4)]),
            ],
        )
        tracks = api.get_playlist_tracks("5")
        assert [t["id"] for t in tracks] == [1, 2, 3, 4]
        # First page goes through the relative endpoint, the rest through
        # the absolute next_href path.
        assert calls[0] == "/playlists/5/tracks"
        assert calls[1].startswith("https://")
        assert len(tracks) == 4

    def test_single_page_terminates(self, monkeypatch):
        api, calls = self._api(monkeypatch, [_page([_track(1), _track(2)])])
        tracks = api.get_playlist_tracks("5")
        assert [t["id"] for t in tracks] == [1, 2]
        assert len(calls) == 1

    def test_repeated_id_stops_next_href_loop(self, monkeypatch):
        # A next_href loop (page repeats an already-seen id) must terminate
        # instead of spinning forever.
        api, calls = self._api(
            monkeypatch,
            [
                _page([_track(1), _track(2)], "https://api.soundcloud.com/playlists/5/tracks?offset=2"),
                _page([_track(1)], "https://api.soundcloud.com/playlists/5/tracks?offset=3"),
            ],
        )
        tracks = api.get_playlist_tracks("5")
        assert [t["id"] for t in tracks] == [1, 2]
        assert len(calls) == 2

    def test_no_data_returns_empty(self, monkeypatch):
        api, calls = self._api(monkeypatch, [None])
        assert api.get_playlist_tracks("5") == []


def _resp(status, headers=None, body=b"{}"):
    r = requests.models.Response()
    r.status_code = status
    r.headers = headers or {}
    r._content = body
    return r


class TestRetryAfterGuard:
    def test_non_numeric_retry_after_does_not_crash(self, monkeypatch):
        mod = _plugin_mod()
        api = mod.SoundCloudAPI("CID", "SECRET", "RT")
        # Skip token refresh: pre-set a valid access token.
        api._access_token = "tok"
        api._token_expires = time.time() + 3600

        responses = iter(
            [
                # 429 with a garbage (HTTP-date) Retry-After, then success.
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
        monkeypatch.setattr(mod.time, "sleep", fake_sleep)

        resp = api._send_with_auth("GET", "https://api.soundcloud.com/me", "/me")
        assert resp is not None and resp.status_code == 200
        assert len(calls) == 2  # 429 -> backoff -> retry
        assert sleeps == [1]  # non-numeric Retry-After fell back to 1s

    def test_numeric_retry_after_respected(self, monkeypatch):
        mod = _plugin_mod()
        api = mod.SoundCloudAPI("CID", "SECRET", "RT")
        api._access_token = "tok"
        api._token_expires = time.time() + 3600

        responses = iter([_resp(429, {"Retry-After": "3"}), _resp(200)])
        sleeps = []

        def fake_request(method, url, headers=None, **kwargs):
            return next(responses)

        def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(api._session, "request", fake_request)
        monkeypatch.setattr(mod.time, "sleep", fake_sleep)

        resp = api._send_with_auth("GET", "https://api.soundcloud.com/me", "/me")
        assert resp.status_code == 200
        assert sleeps == [3]