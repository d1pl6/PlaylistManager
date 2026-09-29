"""ListenBrainz plugin tests.

Covers the three things that can silently go wrong in this plugin:

* the credential file (profile-aware path, 0600, legacy migration, and the
  verify-first save contract ``auth_setup`` relies on);
* ``scrobble``'s return-value contract - a timestamp ONLY when ListenBrainz
  confirmed the listen, since the core writes that timestamp into the
  scrobble ledger and later deletes exactly that listen;
* the delete path, which needs a ``recording_msid`` the submit response does
  not return and therefore has to look the listen back up.  A lookup that
  cannot find the listen must leave the scrobble alone (False) instead of
  risking the deletion of an unrelated one.
"""

import json
import sys
from types import SimpleNamespace

import pytest

from services import auth_setup


def _plugin_mod():
    try:
        import integrations.listenbrainz.listenbrainz as m
    except ImportError:
        pytest.skip("listenbrainz plugin not installed in this checkout")
    return m


def _integration_mod():
    try:
        import integrations.listenbrainz.integration as m
    except ImportError:
        pytest.skip("listenbrainz plugin not installed in this checkout")
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
    legacy = tmp_path / "shared_gone" / "listenbrainz.json"
    monkeypatch.setattr(mod, "_legacy_shared_auth_file", staticmethod(lambda: legacy))
    return SimpleNamespace(mod=mod, legacy=legacy)


def _creds():
    return {"token": "tok-123", "username": "listener"}


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Catch settings writes from the login path.

    This suite shares one settings file with everything else, so a login
    that wrote a key would leak into other tests.  The login writes none -
    the test below asserts that - but the fixture is what would show it.
    """
    written = {}
    monkeypatch.setattr(
        "utils.config.set_setting_value",
        lambda section, option, value: written.update({(section, option): value}),
    )
    return written


class TestCredentialFiles:
    def test_save_and_load_round_trip(self, plugin):
        mod = plugin.mod
        file = mod._listenbrainz_auth_file()
        mod.save_listenbrainz_credentials_file("tok-123", "listener")
        assert file.exists()
        assert file.stat().st_mode & 0o777 == 0o600
        assert mod.load_listenbrainz_credentials() == _creds()

    def test_load_missing_returns_empty(self, plugin):
        assert plugin.mod.load_listenbrainz_credentials() == {}

    def test_load_migrates_legacy_shared_file(self, plugin):
        mod = plugin.mod
        target = mod._listenbrainz_auth_file()
        assert not target.exists()
        plugin.legacy.parent.mkdir(parents=True)
        plugin.legacy.write_text(json.dumps(_creds()), encoding="utf-8")
        assert mod.load_listenbrainz_credentials() == _creds()
        assert target.exists()
        assert not plugin.legacy.exists()

    def test_load_of_a_corrupt_file_returns_empty(self, plugin):
        file = plugin.mod._listenbrainz_auth_file()
        file.write_text("{not json", encoding="utf-8")
        assert plugin.mod.load_listenbrainz_credentials() == {}

    def test_save_failure_leaves_no_temp_file(self, plugin, monkeypatch):
        mod = plugin.mod
        target = mod._listenbrainz_auth_file()
        monkeypatch.setattr(
            mod.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )
        with pytest.raises(OSError):
            mod.save_listenbrainz_credentials_file("tok", "listener")
        assert not target.with_name(target.name + ".tmp").exists()

    def test_delete_delegates_and_clears_the_file(self, plugin, monkeypatch):
        assert auth_setup.delete_listenbrainz_credentials() is False
        plugin.mod.save_listenbrainz_credentials_file("tok-123", "listener")
        # Point the app-side delete path at the file the plugin wrote so the
        # delegation is exercised end to end.
        monkeypatch.setitem(
            auth_setup.PLATFORM_CREDENTIAL_FILES,
            "listenbrainz",
            [plugin.mod._listenbrainz_auth_file()],
        )
        assert auth_setup.delete_listenbrainz_credentials() is True
        assert not plugin.mod._listenbrainz_auth_file().exists()

    def test_a_successful_login_writes_no_settings(
        self, plugin, monkeypatch, _isolated_settings
    ):
        # Logging in enables the service by itself (an installed,
        # authenticated backend is on unless `[<platform>] scrobbles = false`
        # says otherwise), so the login must NOT write a key - otherwise a
        # re-login would silently resurrect a service the user had switched
        # off.
        monkeypatch.setattr(
            plugin.mod.ListenBrainzClient,
            "validate_token",
            lambda self, token=None: {"ok": True, "username": "listener", "error": ""},
        )
        assert auth_setup.save_and_verify_listenbrainz_credentials("tok-123")["ok"]
        assert _isolated_settings == {}

    def test_a_failed_login_records_nothing(
        self, plugin, monkeypatch, _isolated_settings
    ):
        monkeypatch.setattr(
            plugin.mod.ListenBrainzClient,
            "validate_token",
            lambda self, token=None: {"ok": False, "username": "", "error": "Token invalid."},
        )
        assert not auth_setup.save_and_verify_listenbrainz_credentials("bad")["ok"]
        assert _isolated_settings == {}

    def test_credential_file_is_listed_for_logout(self):
        # The static map is what logout/uninstall delete when the manifest
        # cannot be read (plugin dir missing) - a plugin absent from it
        # would leave a credential file behind forever.
        assert "listenbrainz" in auth_setup.PLATFORM_CREDENTIAL_FILES


class TestVerifyBeforeSave:
    def test_bad_token_is_not_persisted(self, plugin, monkeypatch):
        mod = plugin.mod
        monkeypatch.setattr(
            mod.ListenBrainzClient,
            "validate_token",
            lambda self, token=None: {
                "ok": False,
                "username": "",
                "error": "Token invalid.",
            },
        )
        result = auth_setup.save_and_verify_listenbrainz_credentials("bad")
        assert result["ok"] is False
        # Verify-first: a typo must never destroy working credentials.
        assert not mod._listenbrainz_auth_file().exists()

    def test_good_token_is_persisted_with_its_username(self, plugin, monkeypatch):
        mod = plugin.mod
        monkeypatch.setattr(
            mod.ListenBrainzClient,
            "validate_token",
            lambda self, token=None: {"ok": True, "username": "listener", "error": ""},
        )
        result = auth_setup.save_and_verify_listenbrainz_credentials("tok-123")
        assert result["ok"] is True
        assert mod.load_listenbrainz_credentials() == _creds()

    def test_save_failure_is_reported_not_raised(self, plugin, monkeypatch):
        mod = plugin.mod
        monkeypatch.setattr(
            mod.ListenBrainzClient,
            "validate_token",
            lambda self, token=None: {"ok": True, "username": "listener", "error": ""},
        )
        monkeypatch.setattr(
            mod,
            "save_listenbrainz_credentials_file",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        result = auth_setup.save_and_verify_listenbrainz_credentials("tok-123")
        assert result["ok"] is False
        assert "disk full" in result["error"]

    def test_validate_token_reads_the_valid_flag_not_the_status(self, plugin, monkeypatch):
        # /1/validate-token answers 200 for an INVALID token too, so the
        # status code alone would accept any garbage.
        mod = plugin.mod
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {"code": 200, "message": "Token invalid.", "valid": False},
        )
        monkeypatch.setattr(
            mod._HTTP, "get", lambda *a, **k: response, raising=False
        )
        result = mod.ListenBrainzClient("garbage").validate_token()
        assert result["ok"] is False
        assert result["username"] == ""

    def test_validate_token_returns_the_user_name(self, plugin, monkeypatch):
        mod = plugin.mod
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {"code": 200, "valid": True, "user_name": "listener"},
        )
        monkeypatch.setattr(
            mod._HTTP, "get", lambda *a, **k: response, raising=False
        )
        result = mod.ListenBrainzClient("tok").validate_token()
        assert result == {"ok": True, "username": "listener", "error": ""}


class TestScrobble:
    @pytest.fixture
    def integ(self, plugin):
        integ = _integration_mod().ListenBrainzIntegration()
        integ.client = SimpleNamespace(
            submit_listen=lambda *a, **k: None,
            find_listen_msid=lambda *a, **k: None,
            delete_listen=lambda *a, **k: None,
            latest_listen_msid=lambda *a, **k: None,
        )
        return integ

    def test_returns_the_timestamp_it_submitted(self, integ):
        sent = {}

        def submit(artist, track, ts, **kw):
            sent.update(artist=artist, track=track, ts=ts, **kw)

        integ.client.submit_listen = submit
        assert integ.scrobble(
            {"title": "Track", "artists": ["Artist"], "played_at": 1_700_000_000}
        ) == 1_700_000_000
        assert sent["artist"] == "Artist"
        assert sent["track"] == "Track"
        assert sent["ts"] == 1_700_000_000

    def test_millisecond_timestamps_are_normalised(self, integ):
        # Some captures carry epoch milliseconds.  Forwarded as-is, the
        # listen would land in the far future and its ledger timestamp
        # could never match the stored listened_at, so the delete would
        # silently find nothing.
        integ.client.submit_listen = lambda *a, **k: None
        assert integ.scrobble(
            {"title": "T", "artists": ["A"], "played_at": 1_700_000_000_000}
        ) == 1_700_000_000

    def test_missing_artist_or_title_returns_none(self, integ):
        assert integ.scrobble({"title": "T"}) is None
        assert integ.scrobble({"artists": ["A"]}) is None
        assert integ.scrobble({"title": "", "artists": ["A"]}) is None

    def test_a_failed_submission_returns_none(self, integ):
        def boom(*a, **k):
            raise RuntimeError("401")

        integ.client.submit_listen = boom
        assert integ.scrobble({"title": "T", "artists": ["A"]}) is None

    def test_unauthenticated_returns_none(self, integ):
        integ.client = None
        assert integ.scrobble({"title": "T", "artists": ["A"]}) is None

    def test_love_capability_is_absent(self, integ):
        # Deliberate: ListenBrainz love/hate is keyed on a MusicBrainz
        # recording MBID this app cannot resolve.  Absence (rather than a
        # stub returning False) is what keeps the core's like button on a
        # backend that can actually do it - see services/scrobble.py.
        for method in ("love", "unlove", "is_loved", "get_loved_set"):
            assert not callable(getattr(integ, method, None)), method


class TestDeleteScrobble:
    @pytest.fixture
    def integ(self, plugin):
        integ = _integration_mod().ListenBrainzIntegration()
        integ.client = SimpleNamespace()
        return integ

    def test_looks_the_msid_up_and_deletes_that_listen(self, integ):
        calls = []
        integ.client.find_listen_msid = lambda a, t, ts: "msid-1"
        integ.client.delete_listen = lambda ts, msid: calls.append((ts, msid))
        assert integ.delete_scrobble("Artist", "Track", 1_700_000_000) is True
        assert calls == [(1_700_000_000, "msid-1")]

    def test_unmatched_listen_is_left_alone(self, integ):
        # The whole point of the ledger timestamp is to delete THIS listen;
        # guessing would erase a different, legitimate one.
        integ.client.find_listen_msid = lambda *a, **k: None
        integ.client.delete_listen = lambda *a, **k: pytest.fail("must not delete")
        assert integ.delete_scrobble("Artist", "Track", 1_700_000_000) is False

    def test_a_failed_delete_returns_false(self, integ):
        integ.client.find_listen_msid = lambda *a, **k: "msid-1"

        def boom(*a, **k):
            raise RuntimeError("500")

        integ.client.delete_listen = boom
        assert integ.delete_scrobble("Artist", "Track", 1) is False

    def test_timestamp_less_delete_uses_the_newest_listen(self, integ):
        calls = []
        integ.client.latest_listen_msid = lambda a, t: (111, "msid-2")
        integ.client.delete_listen = lambda ts, msid: calls.append((ts, msid))
        assert integ.delete_scrobble("Artist", "Track") is True
        assert calls == [(111, "msid-2")]

    def test_unauthenticated_returns_false(self, integ):
        integ.client = None
        assert integ.delete_scrobble("Artist", "Track", 1) is False


class TestMsidLookup:
    def _client(self, plugin, listens):
        mod = plugin.mod
        client = mod.ListenBrainzClient("tok", "listener")
        client._listens = lambda params: listens
        return client

    def test_prefers_the_metadata_match(self, plugin):
        listens = [
            {"listened_at": 100, "recording_msid": "other",
             "track_metadata": {"artist_name": "X", "track_name": "Y"}},
            {"listened_at": 100, "recording_msid": "right",
             "track_metadata": {"artist_name": "artist", "track_name": "track"}},
        ]
        client = self._client(plugin, listens)
        assert client.find_listen_msid("Artist", "Track", 100) == "right"

    def test_a_different_track_in_the_same_second_is_not_deleted(self, plugin):
        # The ledger timestamp exists to address ONE listen.  A different
        # track scrobbled in the same second is a listen the user still
        # wants, so it must never be returned as a match.
        listens = [
            {"listened_at": 100, "recording_msid": "only",
             "track_metadata": {"artist_name": "X", "track_name": "Y"}},
        ]
        client = self._client(plugin, listens)
        assert client.find_listen_msid("Artist", "Track", 100) is None

    def test_a_listen_without_an_msid_is_not_deletable(self, plugin):
        listens = [{"listened_at": 100, "recording_msid": None}]
        client = self._client(plugin, listens)
        assert client.find_listen_msid("Artist", "Track", 100) is None

    def test_another_second_is_not_matched(self, plugin):
        listens = [{"listened_at": 99, "recording_msid": "old"}]
        client = self._client(plugin, listens)
        assert client.find_listen_msid("Artist", "Track", 100) is None

    def test_lookup_failure_is_swallowed(self, plugin, monkeypatch):
        mod = plugin.mod
        client = mod.ListenBrainzClient("tok", "listener")

        def boom(params):
            raise mod.ListenBrainzError("401")

        client._listens = boom
        assert client.find_listen_msid("Artist", "Track", 100) is None

    def test_max_ts_is_bounded_and_min_ts_is_not_sent(self, plugin):
        # The endpoint documents "max_ts or min_ts, but not both".
        seen = {}

        def capture(params):
            seen.update(params)
            return []

        client = self._client(plugin, [])
        client._listens = capture
        client.find_listen_msid("Artist", "Track", 100)
        assert "min_ts" not in seen
        assert seen["max_ts"] == 102


class TestSubmitPayload:
    def test_payload_shape_and_client_identification(self, plugin, monkeypatch):
        mod = plugin.mod
        captured = {}

        def request(method, path, **kwargs):
            captured.update(method=method, path=path, **kwargs)
            return SimpleNamespace(
                status_code=200, json=lambda: {"status": "ok"}, text="", headers={}
            )

        monkeypatch.setattr(mod._HTTP, "request", request)
        client = mod.ListenBrainzClient("tok", "listener")
        client.submit_listen("Artist", "Track", 1_700_000_000, album="Alb",
                             track_number="3", duration=210)

        assert captured["method"] == "POST"
        assert captured["path"] == "https://api.listenbrainz.org/1/submit-listens"
        assert captured["headers"]["Authorization"] == "Token tok"
        body = captured["json"]
        assert body["listen_type"] == "single"
        listen = body["payload"][0]
        assert listen["listened_at"] == 1_700_000_000
        meta = listen["track_metadata"]
        assert meta["artist_name"] == "Artist"
        assert meta["track_name"] == "Track"
        assert meta["release_name"] == "Alb"
        info = meta["additional_info"]
        assert info["duration_ms"] == 210_000
        assert info["tracknumber"] == "3"
        assert info["submission_client"] == "PlaylistManager"
        # Guessing a music_service would pollute ListenBrainz's mapping.
        assert "music_service" not in info
        assert "origin_url" not in info

    def test_absent_metadata_is_omitted_not_sent_empty(self, plugin, monkeypatch):
        mod = plugin.mod
        captured = {}

        def request(method, path, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status_code=200, json=lambda: {"status": "ok"}, text="", headers={}
            )

        monkeypatch.setattr(mod._HTTP, "request", request)
        mod.ListenBrainzClient("tok", "u").submit_listen("A", "T", 1_700_000_000)
        meta = captured["json"]["payload"][0]["track_metadata"]
        assert "release_name" not in meta
        info = meta["additional_info"]
        assert "duration_ms" not in info
        assert "tracknumber" not in info

    def test_a_200_carrying_an_error_is_not_a_success(self, plugin, monkeypatch):
        mod = plugin.mod

        def request(method, path, **kwargs):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"status": "failed", "error": "nope"},
                text="",
                headers={},
            )

        monkeypatch.setattr(mod._HTTP, "request", request)
        with pytest.raises(mod.ListenBrainzError):
            mod.ListenBrainzClient("tok", "u").submit_listen("A", "T", 1_700_000_000)

    def test_a_missing_artist_or_track_is_refused(self, plugin):
        mod = plugin.mod
        client = mod.ListenBrainzClient("tok", "u")
        with pytest.raises(mod.ListenBrainzError):
            client.submit_listen("", "T", 1_700_000_000)
        with pytest.raises(mod.ListenBrainzError):
            client.submit_listen("A", "", 1_700_000_000)

    def test_auth_header_is_the_token_scheme(self, plugin):
        mod = plugin.mod
        assert mod._headers("tok")["Authorization"] == "Token tok"


class TestAuthenticate:
    def test_requires_a_username_to_look_logged_in(self, plugin):
        integ = _integration_mod().ListenBrainzIntegration()
        plugin.mod.save_listenbrainz_credentials_file("tok-123", "")
        # Without a username the listens/delete endpoints cannot even be
        # addressed, so reporting "authenticated" would make every later
        # delete fail while the UI claimed to be logged in.
        assert integ.authenticate() is False
        assert integ.is_authenticated() is False

    def test_loads_the_stored_token_and_username(self, plugin):
        integ = _integration_mod().ListenBrainzIntegration()
        plugin.mod.save_listenbrainz_credentials_file("tok-123", "listener")
        assert integ.authenticate() is True
        assert integ.is_authenticated() is True
        assert integ.client.token == "tok-123"
        assert integ.client.username == "listener"

    def test_missing_file_is_not_authenticated(self, plugin):
        integ = _integration_mod().ListenBrainzIntegration()
        assert integ.authenticate() is False
        assert integ.refresh_auth() is False
