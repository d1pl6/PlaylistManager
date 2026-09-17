"""Last.fm plugin tests: profile-aware credential resolution + migration,
delete delegation, loved-cache write-through (the in-flight fetch race
regression), envelope-driven loved-track pagination, and the 429-fatal
authorization poll."""

import json
import sys
from types import SimpleNamespace

import pytest
import requests

from services import auth_setup


def _plugin_mod():
    try:
        import integrations.lastfm.lastfm as m
    except ImportError:
        pytest.skip("lastfm plugin not installed in this checkout")
    return m


def _integration_mod():
    try:
        import integrations.lastfm.integration as m
    except ImportError:
        pytest.skip("lastfm plugin not installed in this checkout")
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
    legacy = tmp_path / "shared_gone" / "lastfm.json"
    monkeypatch.setattr(mod, "_legacy_shared_auth_file", staticmethod(lambda: legacy))
    return SimpleNamespace(mod=mod, legacy=legacy)


def _creds():
    return {
        "api_key": "KEY",
        "api_secret": "SECRET",
        "session_key": "SK",
        "username": "user",
    }


class TestCredentialFiles:
    def test_save_and_load_round_trip(self, plugin):
        mod = plugin.mod
        file = mod._lastfm_auth_file()
        mod.save_lastfm_credentials_file(**_creds())
        assert file.exists()
        assert file.stat().st_mode & 0o777 == 0o600
        assert mod.load_lastfm_credentials() == _creds()

    def test_load_missing_returns_empty(self, plugin):
        assert plugin.mod.load_lastfm_credentials() == {}

    def test_load_migrates_legacy_shared_file(self, plugin, tmp_path):
        mod = plugin.mod
        target = mod._lastfm_auth_file()
        assert not target.exists()
        plugin.legacy.parent.mkdir(parents=True)
        plugin.legacy.write_text(json.dumps(_creds()), encoding="utf-8")
        assert mod.load_lastfm_credentials() == _creds()
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == _creds()
        assert not plugin.legacy.exists()

    def test_delete_lastfm_credentials_delegates(self, plugin, monkeypatch):
        # Missing -> False, no raise.
        assert auth_setup.delete_lastfm_credentials() is False
        plugin.mod.save_lastfm_credentials_file(**_creds())
        # Point the app-side delete path at the same file the plugin wrote
        # so the delegation is exercised end to end (matching the default
        # profile, where both sides resolve to one place).
        monkeypatch.setitem(
            auth_setup.PLATFORM_CREDENTIAL_FILES,
            "lastfm",
            [plugin.mod._lastfm_auth_file()],
        )
        assert auth_setup.delete_lastfm_credentials() is True
        assert not plugin.mod._lastfm_auth_file().exists()


class TestLovedCacheWriteThrough:
    def test_love_and_unlove_cache_new_state(self, plugin):
        integ_mod = _integration_mod()
        from integrations.lastfm.lastfm import LastFMClient

        integ = integ_mod.LastFMIntegration()

        class FakeClient:
            def __init__(self):
                self.fail_lookup = False

            def love(self, artist, track):
                pass

            def unlove(self, artist, track):
                pass

            def get_loved(self, artist, track):
                if self.fail_lookup:
                    raise RuntimeError("network down")
                return None

        fake = FakeClient()
        integ.client = fake

        # Uncached lookup returns None (fake "unknown track") and caches it.
        assert integ.is_loved("Artist", "Track") is None
        # love() caches True directly - a stale in-flight fetch can no
        # longer overwrite it with the pre-love answer.
        assert integ.love("Artist", "Track") is True
        fake.fail_lookup = True
        assert integ.is_loved("Artist", "Track") is True
        # Same for unlove -> False.
        assert integ.unlove("Artist", "Track") is True
        assert integ.is_loved("Artist", "Track") is False


def _page(items, total_pages=None):
    root = {"lovedtracks": {"track": items}}
    if total_pages is not None:
        root["lovedtracks"]["@attr"] = {"totalPages": str(total_pages)}
    return root


def _item(i):
    return {"artist": {"#text": f"Artist {i}"}, "name": f"Track {i}"}


class TestGetLovedTracksPagination:
    def _client(self, responses):
        from integrations.lastfm.lastfm import LastFMClient

        client = LastFMClient("k", "s", session_key="sk", username="user")
        calls = []

        def fake_call(method, params, *, signed=False):
            calls.append(params["page"])
            return responses.pop(0)

        client._call = fake_call
        return client, calls

    def test_envelope_stops_at_total_pages(self, plugin):
        client, calls = self._client(
            [_page([_item(1)], total_pages=2), _page([_item(2)], total_pages=2)]
        )
        loved = client.get_loved_tracks()
        assert calls == [1, 2]
        assert loved == {("artist 1", "track 1"): True, ("artist 2", "track 2"): True}

    def test_envelope_capped_by_max_pages(self, plugin):
        client, calls = self._client([_page([_item(i)], total_pages=7) for i in range(5)])
        loved = client.get_loved_tracks()
        assert calls == [1, 2, 3, 4, 5]
        assert len(loved) == 5

    def test_count_heuristic_when_envelope_missing(self, plugin):
        long_page = _page(
            [_item(i) for i in range(1000)],
        )
        short_page = _page([_item(1000)])
        client, calls = self._client([long_page, short_page])
        loved = client.get_loved_tracks()
        assert calls == [1, 2]
        assert ("artist 999", "track 999") in loved

    def test_envelope_wins_over_short_page(self, plugin):
        # A single-item page with totalPages=2 must NOT stop early.
        client, calls = self._client(
            [_page([_item(1)], total_pages=2), _page([_item(2)], total_pages=2)]
        )
        loved = client.get_loved_tracks()
        assert calls == [1, 2]
        assert len(loved) == 2

    def test_without_username_returns_empty(self, plugin):
        from integrations.lastfm.lastfm import LastFMClient

        client = LastFMClient("k", "s")
        assert client.get_loved_tracks() == {}


def _http_error(status):
    resp = requests.models.Response()
    resp.status_code = status
    return requests.HTTPError(response=resp)


class TestGetSessionFatalCodes:
    def test_429_is_fatal(self, plugin, monkeypatch):
        from integrations.lastfm import lastfm
        from integrations.lastfm.lastfm import LastFMClient

        def boom(self, method, params, *, signed=False):
            raise _http_error(429)

        monkeypatch.setattr(LastFMClient, "_call", boom)
        with pytest.raises(lastfm.LastFMError, match="rate limit"):
            lastfm._get_session("k", "s", "token")