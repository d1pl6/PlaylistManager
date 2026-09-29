"""Tidal plugin tests (headless, fake transport).

Tidal exposes no now-playing endpoint and ships no Linux desktop app, so the
plugin's track identity comes from a browser extension posting to a loopback
receiver, with a catalogue search as the fallback.  What can silently go
wrong, and is therefore covered here:

* the credential file - profile-aware path, 0600, atomic write - and the
  verify-first save the core's ``auth_setup`` wrapper relies on;
* the token dance: the PKCE pair, a *rotating* refresh token that must
  survive a restart, and ``_raw``'s exactly-one-retry-on-expiry contract;
* the write shape Tidal expects (``If-None-Match`` from the playlist ETag,
  the form fields, position-addressed deletes) and the add-flow invariant
  that a refusal RAISES instead of returning an empty list a caller could
  mistake for success;
* search scoring, where a loose match would add the wrong song;
* the receiver's loopback ``Host`` filter and its per-run token.

No network is touched.  Only two tests bind a loopback socket (the PKCE
callback server) and both use an ephemeral port.  The module skips when the
tidal plugin repo is absent - ``integrations/*`` is gitignored, so it is
missing on a fresh clone.
"""

import io
import json
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import unquote_plus, urlparse

import pytest
import requests

from services import auth_setup, health_check, song_manager


def _mod():
    try:
        import integrations.tidal.tidal as m
    except ImportError:
        pytest.skip("tidal plugin not installed in this checkout")
    return m


def _flow_mod():
    try:
        import integrations.tidal.flow as m
    except ImportError:
        pytest.skip("tidal plugin not installed in this checkout")
    return m


def _integration_mod():
    try:
        import integrations.tidal.integration as m
    except ImportError:
        pytest.skip("tidal plugin not installed in this checkout")
    return m


def _receiver_mod():
    try:
        import integrations.tidal.tidal_receiver as m
    except ImportError:
        pytest.skip("tidal plugin not installed in this checkout")
    return m


@pytest.fixture
def mod():
    return _mod()


@pytest.fixture
def plugin(mod, tmp_path, monkeypatch):
    """The plugin module with its credential dir isolated per test."""
    auth = tmp_path / "auth"

    def resolve():
        auth.mkdir(parents=True, exist_ok=True, mode=0o700)
        return auth

    monkeypatch.setattr(mod, "_resolve_auth_dir", resolve)
    return SimpleNamespace(mod=mod, auth=auth)


def _creds():
    return {
        "client_id": "CID",
        "client_secret": "SECRET",
        "refresh_token": "RT",
        "user_id": "u1",
        "country_code": "US",
        "session_id": "s1",
    }


@pytest.fixture
def api(plugin):
    """A client holding a live access token, so no token POST happens."""
    client = plugin.mod.TidalAPI(
        "CID", "SECRET", "RT", user_id="u1", country_code="US", session_id="s1"
    )
    client._access_token = "tok"
    client._token_expires = time.time() + 3600
    return client


def _resp(status, body=None, headers=None):
    r = requests.models.Response()
    r.status_code = status
    r.headers = headers or {}
    r._content = json.dumps({} if body is None else body).encode("utf-8")
    return r


def _empty_resp(status):
    r = requests.models.Response()
    r.status_code = status
    r.headers = {}
    r._content = b""
    return r


def _transport(client, responses):
    """Queue *responses* on the client's session; return the call recorder."""
    calls = []
    queue = list(responses)

    def fake_request(method, url, params=None, data=None, headers=None, **kw):
        calls.append(
            {
                "method": method,
                "url": url,
                "params": params or {},
                "data": data,
                "headers": headers or {},
            }
        )
        if not queue:
            raise AssertionError(f"unexpected request: {method} {url}")
        return queue.pop(0)

    client._session.request = fake_request
    return calls


# ---------------------------------------------------------------------------
# Credential file
# ---------------------------------------------------------------------------


class TestCredentialFile:
    def test_save_and_load_round_trip(self, plugin):
        m = plugin.mod
        m.save_tidal_credentials_file(**_creds())
        file = m.tidal_auth_file()
        assert file.exists()
        assert file.stat().st_mode & 0o777 == 0o600
        assert m.load_tidal_credentials() == _creds()

    def test_load_missing_returns_none(self, plugin):
        assert plugin.mod.load_tidal_credentials() is None

    def test_load_of_a_corrupt_file_returns_none(self, plugin):
        file = plugin.mod.tidal_auth_file()
        file.write_text("{not json", encoding="utf-8")
        assert plugin.mod.load_tidal_credentials() is None

    def test_save_failure_leaves_no_temp_file(self, plugin, monkeypatch):
        m = plugin.mod
        target = m.tidal_auth_file()
        monkeypatch.setattr(
            m.os,
            "replace",
            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
        )
        with pytest.raises(OSError):
            m.save_tidal_credentials_file(**_creds())
        assert not target.with_name(target.name + ".tmp").exists()

    def test_core_map_points_at_the_credential_file(self):
        # Logout, uninstall and the startup permission audit all resolve a
        # platform's credential paths through this one map.
        assert auth_setup.PLATFORM_CREDENTIAL_FILES["tidal"] == [
            auth_setup.TIDAL_FILE
        ]
        assert auth_setup.TIDAL_FILE.name == "tidal.json"

    def test_core_delete_removes_the_file_and_the_keyring_entry(
        self, plugin, monkeypatch
    ):
        plugin.mod.save_tidal_credentials_file(**_creds())
        file = plugin.mod.tidal_auth_file()
        # Point the app-side paths at the file the plugin wrote so the
        # delegation is exercised end to end.
        monkeypatch.setattr(auth_setup, "AUTH_DIR", plugin.auth)
        monkeypatch.setitem(
            auth_setup.PLATFORM_CREDENTIAL_FILES, "tidal", [file]
        )
        cleared = []
        monkeypatch.setattr(
            auth_setup.credential_store,
            "delete",
            lambda name: cleared.append(name),
        )
        deleted, missing = auth_setup.delete_platform_credentials("tidal")
        assert deleted == [file]
        assert missing == []
        assert not file.exists()
        assert cleared == ["tidal.json"]

    def test_delete_tidal_credentials_reports_whether_it_removed_anything(
        self, plugin
    ):
        m = plugin.mod
        assert m.delete_tidal_credentials() is False
        m.save_tidal_credentials_file(**_creds())
        assert m.delete_tidal_credentials() is True
        assert not m.tidal_auth_file().exists()


# ---------------------------------------------------------------------------
# PKCE login
# ---------------------------------------------------------------------------


def _free_port():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _callback(mod, target):
    """A stub request object for the loopback handler, plus its result dict.

    ``_CallbackHandler.do_GET`` only touches ``path``/``headers``/``result``
    and the ``_respond`` helpers, so the callback's routing rules can be
    tested without binding a socket.
    """
    parsed = urlparse(target)
    result = {}
    handler = SimpleNamespace(
        path=parsed.path + ("?" + parsed.query if parsed.query else ""),
        headers={"Host": "127.0.0.1:47831"},
        result=result,
        expected_state="STATE",
        status=None,
        wfile=io.BytesIO(),
    )
    handler.send_response = lambda status, *a, **k: setattr(
        handler, "status", status
    )
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda *a, **k: None
    # The real _respond, so the body/status assertions exercise it too.
    handler._respond = lambda status, title, detail="": mod._CallbackHandler._respond(
        handler, status, title, detail
    )
    return handler, result


class TestPkcePair:
    def test_pair_matches_the_s256_spec(self, mod):
        import base64
        import hashlib

        verifier, challenge = mod._pkce_pair()
        assert 43 <= len(verifier) <= 128
        assert "=" not in challenge
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected

    def test_pair_is_never_reused(self, mod):
        assert mod._pkce_pair()[0] != mod._pkce_pair()[0]


class TestAuthorizationUrl:
    def _params(self, mod):
        url = mod.build_authorization_url(
            "CID", "http://127.0.0.1:47831/callback", "CHAL", "STATE"
        )
        assert url.startswith("https://login.tidal.com/authorize?")
        return {
            k: unquote_plus(v)
            for k, v in (
                pair.split("=", 1) for pair in url.split("?", 1)[1].split("&")
            )
        }

    def test_carries_every_required_parameter(self, mod):
        p = self._params(mod)
        assert p["response_type"] == "code"
        assert p["client_id"] == "CID"
        assert p["code_challenge"] == "CHAL"
        assert p["code_challenge_method"] == "S256"
        assert p["state"] == "STATE"
        assert p["redirect_uri"] == "http://127.0.0.1:47831/callback"

    def test_the_scope_can_write(self, mod):
        # Without w_usr the token can read the library but not write to it.
        p = self._params(mod)
        assert set(p["scope"].split()) == set(mod.TIDAL_SCOPES.split())
        assert "w_usr" in p["scope"]

    def test_the_page_required_parameter_rides_along(self, mod):
        # Tidal's authorize page rejects a request without it.
        assert self._params(mod)["client_unique_key"]


class TestCallbackHandler:
    def test_a_foreign_host_is_refused(self, mod):
        handler, _ = _callback(mod, "http://127.0.0.1:47831/callback?code=C")
        handler.headers = {"Host": "attacker.example.com"}
        mod._CallbackHandler.do_GET(handler)
        assert handler.status == 403
        assert not handler.result

    def test_a_state_mismatch_is_refused(self, mod):
        handler, _ = _callback(
            mod, "http://127.0.0.1:47831/callback?code=C&state=OTHER"
        )
        mod._CallbackHandler.do_GET(handler)
        assert handler.status == 400
        assert handler.result["error"] == "state_mismatch"
        # A code we cannot attribute to this attempt is never exchanged.
        assert "code" not in handler.result

    def test_a_tidal_error_is_surfaced(self, mod):
        handler, _ = _callback(
            mod, "http://127.0.0.1:47831/callback?error=access_denied"
        )
        mod._CallbackHandler.do_GET(handler)
        assert handler.result["error"] == "access_denied"

    def test_a_callback_without_a_code_is_refused(self, mod):
        handler, _ = _callback(
            mod, "http://127.0.0.1:47831/callback?state=STATE"
        )
        mod._CallbackHandler.do_GET(handler)
        assert handler.status == 400
        assert not handler.result

    def test_the_matching_code_is_accepted(self, mod):
        handler, result = _callback(
            mod, "http://127.0.0.1:47831/callback?code=THE_CODE&state=STATE"
        )
        mod._CallbackHandler.do_GET(handler)
        assert handler.status == 200
        assert result["code"] == "THE_CODE"
        assert b"close this tab" in handler.wfile.getvalue()

    def test_a_hung_up_browser_does_not_raise(self, mod):
        handler, result = _callback(
            mod, "http://127.0.0.1:47831/callback?code=THE_CODE&state=STATE"
        )

        class _Broken(io.BytesIO):
            def write(self, _data):
                raise OSError("connection reset")

        handler.wfile = _Broken()
        mod._CallbackHandler.do_GET(handler)
        # The result dict is what the caller reads, not the socket.
        assert result["code"] == "THE_CODE"


class TestWaitForCode:
    def test_a_non_loopback_redirect_is_refused(self, mod):
        with pytest.raises(mod.TidalError, match="loopback"):
            mod.wait_for_authorization_code(
                "https://example.com/callback", "STATE", timeout=1
            )

    def test_a_port_already_in_use_is_reported_clearly(self, mod):
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            with pytest.raises(mod.TidalError, match="in use"):
                mod.wait_for_authorization_code(
                    f"http://127.0.0.1:{port}/callback", "STATE", timeout=1
                )
        finally:
            sock.close()

    def test_a_timeout_is_reported(self, mod):
        with pytest.raises(mod.TidalError, match="Timed out"):
            mod.wait_for_authorization_code(
                f"http://127.0.0.1:{_free_port()}/callback", "STATE", timeout=1
            )

    def test_the_served_redirect_yields_the_code(self, mod):
        # The one end-to-end path: a real loopback socket, a real browser-
        # shaped request, the code handed back to the caller.
        import threading as _threading

        port = _free_port()
        codes = []
        errors = []

        def wait():
            try:
                codes.append(
                    mod.wait_for_authorization_code(
                        f"http://127.0.0.1:{port}/callback", "STATE", timeout=10
                    )
                )
            except Exception as e:  # surfaced in the assertion below
                errors.append(e)

        thread = _threading.Thread(target=wait)
        thread.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    resp = requests.get(
                        f"http://127.0.0.1:{port}/callback?code=REAL&state=STATE",
                        timeout=2,
                    )
                    break
                except requests.RequestException:
                    time.sleep(0.05)
            else:
                pytest.fail("the callback server never came up")
            assert resp.status_code == 200
        finally:
            thread.join(timeout=10)
        assert errors == []
        assert codes == ["REAL"]


class TestPkceLogin:
    def test_the_code_is_exchanged_with_its_verifier(self, plugin, monkeypatch):
        m = plugin.mod
        posted = {}
        seen = {}

        def fake_wait(redirect_uri, state, timeout=None):
            seen["redirect_uri"] = redirect_uri
            return "THE_CODE"

        def fake_post(data):
            posted.update(data)
            return {"access_token": "AT", "refresh_token": "RT"}

        monkeypatch.setattr(m, "wait_for_authorization_code", fake_wait)
        monkeypatch.setattr(m, "_token_post", fake_post)
        monkeypatch.setattr(m.webbrowser, "open", lambda url: True)

        token = m.perform_pkce_login("CID", "SECRET")
        assert token["refresh_token"] == "RT"
        assert posted["grant_type"] == "authorization_code"
        assert posted["code"] == "THE_CODE"
        assert posted["client_id"] == "CID"
        assert posted["code_verifier"]
        assert posted["redirect_uri"] == m.DEFAULT_REDIRECT_URI
        assert seen["redirect_uri"] == m.DEFAULT_REDIRECT_URI

    def test_the_url_is_handed_to_the_caller_for_a_headless_run(
        self, plugin, monkeypatch
    ):
        m = plugin.mod
        urls = []
        monkeypatch.setattr(
            m, "wait_for_authorization_code", lambda *a, **k: "C"
        )
        monkeypatch.setattr(
            m,
            "_token_post",
            lambda data: {"access_token": "AT", "refresh_token": "RT"},
        )
        m.perform_pkce_login("CID", "SECRET", on_url=urls.append, open_browser=False)
        assert len(urls) == 1
        assert "client_id=CID" in urls[0]

    def test_a_browser_that_will_not_open_is_not_fatal(self, plugin, monkeypatch):
        m = plugin.mod

        def boom(url):
            raise RuntimeError("no display")

        monkeypatch.setattr(m.webbrowser, "open", boom)
        monkeypatch.setattr(
            m, "wait_for_authorization_code", lambda *a, **k: "C"
        )
        monkeypatch.setattr(
            m,
            "_token_post",
            lambda data: {"access_token": "AT", "refresh_token": "RT"},
        )
        assert m.perform_pkce_login("CID", "SECRET")["access_token"] == "AT"

    def test_a_missing_client_id_is_refused(self, plugin):
        with pytest.raises(_mod().TidalError, match="client id"):
            _mod().perform_pkce_login("", "SECRET", open_browser=False)


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


class TestTokenPost:
    def test_success_returns_the_parsed_body(self, plugin, monkeypatch):
        m = plugin.mod
        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured.update(url=url, data=data, headers=headers, timeout=timeout)
            return _resp(200, {"access_token": "AT"})

        monkeypatch.setattr(m.requests, "post", fake_post)
        assert m._token_post({"grant_type": "refresh_token"}) == {
            "access_token": "AT"
        }
        assert captured["url"] == m.TIDAL_TOKEN_URL
        # Tidal's edge 403s anything that does not look like a browser, and
        # an unbounded call would pin the caller's thread.
        assert "Mozilla" in captured["headers"]["User-Agent"]
        assert captured["timeout"] == m.DEFAULT_TIMEOUT

    def test_a_rejection_surfaces_tidals_own_description(self, plugin, monkeypatch):
        m = plugin.mod
        monkeypatch.setattr(
            m.requests,
            "post",
            lambda *a, **k: _resp(
                400,
                {
                    "error": "invalid_grant",
                    "error_description": "Refresh token is invalid",
                },
            ),
        )
        with pytest.raises(m.TidalError, match="Refresh token is invalid"):
            m._token_post({"grant_type": "refresh_token"})

    def test_a_non_json_failure_still_raises(self, plugin, monkeypatch):
        m = plugin.mod

        def fake_post(*a, **k):
            r = requests.models.Response()
            r.status_code = 503
            r.headers = {}
            r._content = b"<html>gateway</html>"
            return r

        monkeypatch.setattr(m.requests, "post", fake_post)
        with pytest.raises(m.TidalError, match="503"):
            m._token_post({"grant_type": "refresh_token"})

    def test_a_transport_failure_becomes_a_tidal_error(self, plugin, monkeypatch):
        m = plugin.mod

        def boom(*a, **k):
            raise requests.ConnectionError("network down")

        monkeypatch.setattr(m.requests, "post", boom)
        with pytest.raises(m.TidalError, match="token endpoint"):
            m._token_post({"grant_type": "refresh_token"})

    def test_a_malformed_success_body_is_reported(self, plugin, monkeypatch):
        m = plugin.mod

        def fake_post(*a, **k):
            r = requests.models.Response()
            r.status_code = 200
            r.headers = {}
            r._content = b"not json"
            return r

        monkeypatch.setattr(m.requests, "post", fake_post)
        with pytest.raises(m.TidalError, match="malformed"):
            m._token_post({"grant_type": "refresh_token"})

    def test_the_secret_is_registered_for_redaction(self, plugin, monkeypatch):
        m = plugin.mod
        monkeypatch.setattr(
            m.requests, "post", lambda *a, **k: _resp(200, {"access_token": "AT"})
        )
        from utils import logging_config

        logging_config.register_secret("a-very-long-tidal-secret")
        m._token_post(
            {
                "grant_type": "refresh_token",
                "client_secret": "a-very-long-tidal-secret",
            }
        )
        assert "a-very-long-tidal-secret" in logging_config._REGISTERED_SECRETS
        # And it is masked when it turns up in a log line.
        assert "a-very-long-tidal-secret" not in logging_config.redact(
            "failed with a-very-long-tidal-secret"
        )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class TestTransport:
    def test_session_and_country_ride_as_query_parameters(self, api):
        calls = _transport(api, [_resp(200, {"items": []})])
        api.get_playlists()
        # Query parameters, not headers - that is how Tidal's own clients
        # pass them, and a call without them is rejected.
        assert calls[0]["params"]["sessionId"] == "s1"
        assert calls[0]["params"]["countryCode"] == "US"
        assert calls[0]["url"] == "https://api.tidal.com/v1/users/u1/playlists"
        assert calls[0]["headers"]["authorization"] == "Bearer tok"
        assert calls[0]["headers"]["x-tidal-client-version"]

    def test_the_session_call_never_sends_a_session_id(self, api):
        # The session endpoint PRODUCES the id; sending a stale one there is
        # at best ignored and at worst rejected.
        calls = _transport(
            api,
            [_resp(200, {"sessionId": "s9", "countryCode": "DE", "userId": "u9"})],
        )
        api.get_session()
        assert calls[0]["url"].endswith("/v1/sessions")
        assert "sessionId" not in calls[0]["params"]
        assert (api.session_id, api.country_code, api.user_id) == ("s9", "DE", "u9")

    def test_the_session_fields_are_persisted(self, api, plugin):
        _transport(
            api, [_resp(200, {"sessionId": "s9", "countryCode": "DE", "userId": "u9"})]
        )
        api.get_session()
        stored = json.loads(
            plugin.mod.tidal_auth_file().read_text(encoding="utf-8")
        )
        assert stored["session_id"] == "s9"
        assert stored["country_code"] == "DE"
        assert stored["user_id"] == "u9"

    def test_a_401_refreshes_once_and_retries(self, api, plugin, monkeypatch):
        m = plugin.mod
        monkeypatch.setattr(
            m, "_token_post", lambda data: {"access_token": "AT2", "expires_in": 3600}
        )
        calls = _transport(
            api,
            [_resp(401, {"userMessage": "Unauthorized"}), _resp(200, {"items": []})],
        )
        assert api.get_playlists() == []
        assert len(calls) == 2
        assert calls[1]["headers"]["authorization"] == "Bearer AT2"

    def test_an_expired_token_body_also_triggers_the_retry(
        self, api, plugin, monkeypatch
    ):
        # Tidal does not always answer 401: a 4xx whose userMessage starts
        # "The token has expired." means exactly the same thing.
        m = plugin.mod
        monkeypatch.setattr(
            m,
            "_token_post",
            lambda data: {"access_token": "AT2", "expires_in": 3600},
        )
        _transport(
            api,
            [
                _resp(400, {"userMessage": "The token has expired."}),
                _resp(200, {"items": []}),
            ],
        )
        assert api.get_playlists() == []

    def test_a_second_failure_is_real_and_raises(self, api, plugin, monkeypatch):
        m = plugin.mod
        monkeypatch.setattr(
            m,
            "_token_post",
            lambda data: {"access_token": "AT2", "expires_in": 3600},
        )
        calls = _transport(
            api,
            [
                _resp(401, {"userMessage": "Unauthorized"}),
                _resp(401, {"userMessage": "Unauthorized"}),
            ],
        )
        with pytest.raises(m.TidalError, match="Unauthorized"):
            api.get_playlists()
        # Exactly one retry: a loop against a revoked token would hammer the
        # endpoint on every keybind press.
        assert len(calls) == 2

    def test_a_4xx_carries_tidals_reason_and_status(self, api):
        m = _mod()
        _transport(
            api, [_resp(404, {"userMessage": "The requested resource was not found"})]
        )
        with pytest.raises(m.TidalError) as exc:
            api.get_track("1")
        assert exc.value.status == 404
        assert "was not found" in str(exc.value)

    def test_a_transport_failure_becomes_a_tidal_error(self, api):
        def boom(*a, **k):
            raise requests.ConnectionError("network down")

        api._session.request = boom
        with pytest.raises(_mod().TidalError, match="failed"):
            api.get_playlists()

    def test_an_empty_body_is_none_not_an_error(self, api):
        api._session.request = lambda *a, **k: _empty_resp(204)
        assert api._request("DELETE", "users/u1/favorites/tracks/1") is None

    def test_a_dead_refresh_token_fails_the_headers(self, api, monkeypatch):
        monkeypatch.setattr(api, "_refresh_access_token", lambda: False)
        api._access_token = None
        with pytest.raises(_mod().TidalError, match="log in again"):
            api._headers()


class TestTokenRefresh:
    @staticmethod
    def _expired(client):
        """Drop the fixture's live token so a refresh really runs."""
        client._access_token = None
        client._token_expires = 0.0

    def test_a_rotated_refresh_token_is_persisted(self, api, plugin, monkeypatch):
        m = plugin.mod
        self._expired(api)
        monkeypatch.setattr(
            m,
            "_token_post",
            lambda data: {
                "access_token": "AT2",
                "refresh_token": "RT2",
                "expires_in": 3600,
            },
        )
        assert api._refresh_access_token() is True
        assert api.refresh_token == "RT2"
        stored = json.loads(
            m.tidal_auth_file().read_text(encoding="utf-8")
        )
        # Without this the next run would present a spent refresh token and
        # the user would be logged out.
        assert stored["refresh_token"] == "RT2"

    def test_the_expiry_carries_a_margin(self, api, plugin, monkeypatch):
        m = plugin.mod
        self._expired(api)
        monkeypatch.setattr(
            m, "_token_post", lambda data: {"access_token": "AT2", "expires_in": 120}
        )
        before = time.time()
        api._refresh_access_token()
        assert before < api._token_expires < before + 120

    def test_a_failed_refresh_keeps_the_old_token(self, api, plugin, monkeypatch):
        m = plugin.mod
        self._expired(api)

        def boom(data):
            raise m.TidalError("Refresh token is invalid")

        monkeypatch.setattr(m, "_token_post", boom)
        assert api._refresh_access_token() is False
        assert api.refresh_token == "RT"

    def test_two_threads_share_one_refresh(self, api, plugin, monkeypatch):
        # Tidal rotates the refresh token, so a second concurrent exchange
        # would spend the one the first thread is about to store.
        m = plugin.mod
        self._expired(api)
        calls = []
        barrier = threading.Barrier(2)

        def fake(data):
            calls.append(data)
            time.sleep(0.05)
            return {"access_token": "AT2", "expires_in": 3600}

        monkeypatch.setattr(m, "_token_post", fake)
        results = []

        def refresh():
            barrier.wait()
            results.append(api._refresh_access_token())

        threads = [threading.Thread(target=refresh) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert results == [True, True]
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class TestAddToPlaylist:
    def test_the_write_shape_and_added_ids(self, api):
        calls = _transport(
            api,
            [
                _resp(200, {"uuid": "p1"}, headers={"etag": "etag-1"}),
                _resp(200, {"addedItemIds": ["1"]}),
            ],
        )
        assert api.add_tracks_to_playlist("p1", [1, "2"]) == ["1"]
        post = calls[1]
        assert post["method"] == "POST"
        assert post["url"] == "https://api.tidal.com/v1/playlists/p1/items"
        assert post["data"] == {
            "onArtifactNotFound": "SKIP",
            "trackIds": "1,2",
            "onDupes": "SKIP",
        }
        # The write must not clobber a concurrent edit.
        assert post["headers"]["If-None-Match"] == "etag-1"

    def test_a_caller_supplied_etag_skips_the_extra_get(self, api):
        calls = _transport(api, [_resp(200, {"addedItemIds": ["1"]})])
        assert api.add_tracks_to_playlist("p1", ["1"], etag="known") == ["1"]
        assert len(calls) == 1
        assert calls[0]["headers"]["If-None-Match"] == "known"

    def test_duplicates_can_be_allowed_explicitly(self, api):
        calls = _transport(
            api,
            [
                _resp(200, {}, headers={"etag": "e"}),
                _resp(200, {"addedItemIds": ["1"]}),
            ],
        )
        api.add_tracks_to_playlist("p1", ["1"], allow_duplicates=True)
        assert calls[1]["data"]["onDupes"] == "ADD"

    def test_a_missing_etag_falls_back_to_the_wildcard(self, api):
        calls = _transport(
            api, [_resp(200, {"uuid": "p1"}), _resp(200, {"addedItemIds": ["1"]})]
        )
        api.add_tracks_to_playlist("p1", ["1"])
        assert calls[1]["headers"]["If-None-Match"] == "*"

    def test_a_refusal_raises_with_tidals_reason(self, api):
        _transport(
            api,
            [
                _resp(200, {"uuid": "p1"}, headers={"etag": "e"}),
                _resp(400, {"userMessage": "Playlist is read only"}),
            ],
        )
        with pytest.raises(_mod().TidalError, match="read only"):
            api.add_tracks_to_playlist("p1", ["1"])

    def test_a_2xx_that_added_nothing_returns_an_empty_list(self, api):
        # Tidal answers 2xx with no addedItemIds when the track is not
        # available in the account's country.  The flow reads the empty list
        # as a rejection, so it must not look like success.
        _transport(
            api,
            [_resp(200, {"uuid": "p1"}, headers={"etag": "e"}), _resp(200, {})],
        )
        assert api.add_tracks_to_playlist("p1", ["1"]) == []

    def test_an_empty_id_list_raises_before_any_request(self, api):
        calls = _transport(api, [])
        with pytest.raises(_mod().TidalError, match="No track ids"):
            api.add_tracks_to_playlist("p1", ["", "  "])
        assert calls == []

    def test_a_playlist_lookup_failure_aborts_the_add(self, api):
        calls = _transport(api, [_resp(404, {"userMessage": "not found"})])
        with pytest.raises(_mod().TidalError):
            api.add_tracks_to_playlist("p1", ["1"])
        assert [c["method"] for c in calls] == ["GET"]


def _items(*ids):
    return {
        "items": [
            {"type": "TRACK", "item": {"id": i, "title": f"t{i}"}} for i in ids
        ]
    }


class TestRemoveFromPlaylist:
    def test_the_position_of_the_track_is_deleted(self, api):
        calls = _transport(
            api,
            [
                _resp(200, _items("1", "2", "3")),
                _resp(200, {"uuid": "p1"}, headers={"etag": "e"}),
                _empty_resp(204),
            ],
        )
        assert api.remove_track_from_playlist("p1", "3") is True
        delete = calls[2]
        # Tidal addresses playlist items by position, not by id.
        assert delete["method"] == "DELETE"
        assert delete["url"] == "https://api.tidal.com/v1/playlists/p1/items/2"
        assert delete["headers"]["If-None-Match"] == "e"

    def test_a_video_entry_with_the_same_id_is_not_the_target(self, api):
        calls = _transport(
            api,
            [
                _resp(
                    200,
                    {
                        "items": [
                            {"type": "TRACK", "item": {"id": "1"}},
                            {"type": "VIDEO", "item": {"id": "3"}},
                            {"type": "TRACK", "item": {"id": "3"}},
                        ]
                    },
                ),
                _resp(200, {"uuid": "p1"}, headers={"etag": "e"}),
                _empty_resp(204),
            ],
        )
        assert api.remove_track_from_playlist("p1", "3") is True
        assert calls[2]["url"].endswith("/items/2")

    def test_an_absent_track_is_reported_without_a_delete(self, api):
        calls = _transport(api, [_resp(200, _items("1"))])
        assert api.remove_track_from_playlist("p1", "9") is False
        assert [c["method"] for c in calls] == ["GET"]

    def test_a_rejected_delete_is_raised(self, api, plugin, monkeypatch):
        m = plugin.mod
        # A 403 is ambiguous - a dead token and "not the owner" look the
        # same - so the client spends one refresh before believing it.
        monkeypatch.setattr(
            m, "_token_post", lambda data: {"access_token": "AT2", "expires_in": 3600}
        )
        calls = _transport(
            api,
            [
                _resp(200, _items("1")),
                _resp(200, {"uuid": "p1"}, headers={"etag": "e"}),
                _resp(403, {"userMessage": "You are not the owner"}),
                _resp(403, {"userMessage": "You are not the owner"}),
            ],
        )
        with pytest.raises(m.TidalError, match="not the owner"):
            api.remove_track_from_playlist("p1", "1")
        # One refresh + one retry, then the reason is surfaced.
        assert [c["method"] for c in calls] == ["GET", "GET", "DELETE", "DELETE"]


class TestPagination:
    def test_all_items_follows_the_offsets(self, api):
        page1 = {"items": [{"type": "TRACK", "item": {"id": str(i)}} for i in range(100)]}
        page2 = {
            "items": [
                {"type": "TRACK", "item": {"id": str(100 + i)}} for i in range(3)
            ]
        }
        calls = _transport(api, [_resp(200, page1), _resp(200, page2)])
        assert len(api.get_all_playlist_items("p1")) == 103
        assert [c["params"]["offset"] for c in calls] == [0, 100]

    def test_an_exactly_full_page_is_followed_by_an_empty_one(self, api):
        full = {"items": [{"type": "TRACK", "item": {"id": str(i)}} for i in range(100)]}
        calls = _transport(api, [_resp(200, full), _resp(200, {"items": []})])
        assert len(api.get_all_playlist_items("p1")) == 100
        assert len(calls) == 2


class TestFavourites:
    def test_favorite_posts_the_track_id(self, api):
        calls = _transport(api, [_resp(200, {})])
        assert api.favorite_track(7) is True
        assert calls[0]["method"] == "POST"
        assert calls[0]["url"].endswith("/users/u1/favorites/tracks")
        assert calls[0]["data"] == {"trackId": "7"}

    def test_unfavorite_deletes_by_id(self, api):
        calls = _transport(api, [_empty_resp(204)])
        assert api.unfavorite_track("7") is True
        assert calls[0]["method"] == "DELETE"
        assert calls[0]["url"].endswith("/users/u1/favorites/tracks/7")

    def test_a_missing_user_id_is_recovered_from_the_session(self, api):
        api.user_id = ""
        api.session_id = ""
        calls = _transport(
            api, [_resp(200, {"userId": "u2", "sessionId": "s2"}), _resp(200, {})]
        )
        assert api.favorite_track("1") is True
        assert calls[1]["url"].endswith("/users/u2/favorites/tracks")

    def test_a_session_without_a_user_id_is_reported(self, api):
        api.user_id = ""
        api.session_id = ""
        _transport(api, [_resp(200, {"sessionId": "s2"})])
        with pytest.raises(_mod().TidalError, match="user id"):
            api.favorite_track("1")


# ---------------------------------------------------------------------------
# Catalogue search
# ---------------------------------------------------------------------------


def _cand(track_id="t1", title="Song", artist="Artist", album="Album"):
    return {
        "id": track_id,
        "title": title,
        "artists": [{"name": artist}],
        "album": {"title": album},
        "duration": 210,
    }


class TestScoreTrack:
    def test_an_exact_title_and_artist_match_scores_highest(self, mod):
        assert mod.score_track(_cand(), "Song", "Artist") >= 150

    def test_a_title_only_match_is_kept(self, mod):
        assert mod.score_track(_cand(), "Song") == 100

    def test_a_containment_match_is_weaker_but_accepted(self, mod):
        # "(Radio Edit)" / "(Remastered)" - the same song.
        assert 0 < mod.score_track(_cand(title="Song (Radio Edit)"), "Song") < 100

    def test_a_different_artist_ranks_below_every_artist_match(self, mod):
        assert mod.score_track(_cand(artist="Other"), "Song", "Artist") < 100

    def test_a_different_title_is_refused(self, mod):
        # Adding the wrong song is worse than reporting "not found".
        assert mod.score_track(_cand(title="Something Else"), "Song") == 0

    def test_the_album_only_breaks_ties(self, mod):
        assert mod.score_track(_cand(), "Song", "Artist", "Album") > mod.score_track(
            _cand(), "Song", "Artist"
        )
        # It never rescues a title mismatch.
        assert mod.score_track(_cand(), "Other", "Artist", "Album") == 0

    def test_a_candidate_without_a_title_scores_zero(self, mod):
        assert mod.score_track({"id": "1", "artists": []}, "Song") == 0

    def test_an_empty_request_scores_zero(self, mod):
        assert mod.score_track(_cand(), "") == 0

    def test_unicode_and_punctuation_do_not_block_a_match(self, mod):
        assert mod.score_track(_cand(title="Don't Stop Me Now!"), "dont stop me now")


class TestSearchTrack:
    def test_the_best_candidate_wins(self, api, monkeypatch):
        monkeypatch.setattr(
            api,
            "search_tracks",
            lambda q, limit=20: [
                _cand("t2", artist="Wrong"),
                _cand("t1", artist="Artist"),
            ],
        )
        assert api.search_track("Song", "Artist")["id"] == "t1"

    def test_nothing_that_scores_returns_none(self, api, monkeypatch):
        monkeypatch.setattr(
            api, "search_tracks", lambda q, limit=20: [_cand(title="Other")]
        )
        assert api.search_track("Song", "Artist") is None

    def test_an_empty_query_never_hits_the_api(self, api, monkeypatch):
        calls = []
        monkeypatch.setattr(api, "search_tracks", lambda q, limit=20: calls.append(q))
        assert api.search_track("") is None
        assert calls == []

    def test_the_query_carries_title_and_artist(self, api, monkeypatch):
        seen = {}

        def fake(q, limit=20):
            seen.update(q=q, limit=limit)
            return []

        monkeypatch.setattr(api, "search_tracks", fake)
        api.search_track("Song", "Artist")
        assert seen["q"] == "Song Artist"
        assert seen["limit"] == 20

    def test_the_search_request_shape(self, api):
        calls = _transport(
            api, [_resp(200, {"tracks": {"items": [_cand()]}})]
        )
        assert api.search_tracks("Song") == [_cand()]
        assert calls[0]["url"] == "https://api.tidal.com/v1/search"
        assert calls[0]["params"]["types"] == "TRACKS"
        assert calls[0]["params"]["query"] == "Song"

    def test_a_malformed_search_body_is_empty(self, api):
        _transport(api, [_resp(200, {"tracks": {}})])
        assert api.search_tracks("Song") == []

    def test_isrc_lookup_uses_the_v2_catalogue(self, api):
        # v1 has no ISRC endpoint; v2 does, and it is read-only/public.
        calls = _transport(api, [_resp(200, {"data": [{"id": "x"}]})])
        assert api.get_tracks_by_isrc("USRC17607839") == [{"id": "x"}]
        assert calls[0]["url"] == "https://openapi.tidal.com/v2/tracks"
        assert calls[0]["params"]["filter[isrc]"] == "USRC17607839"


class TestCoverUpgrade:
    def test_the_80px_variant_is_bumped(self, mod):
        assert mod.upgrade_cover_url(
            "https://resources.tidal.com/images/uuid/80x80.jpg"
        ).endswith("/320x320.jpg")

    def test_an_already_large_cover_is_left_alone(self, mod):
        assert mod.upgrade_cover_url(
            "https://resources.tidal.com/images/uuid/1280x1280.jpg", "640x640"
        ).endswith("/640x640.jpg")

    def test_a_url_without_a_size_segment_is_unchanged(self, mod):
        # Tidal occasionally serves covers from another host; rewriting that
        # would 404 the card.
        assert (
            mod.upgrade_cover_url("https://example.com/a/b.jpg")
            == "https://example.com/a/b.jpg"
        )

    def test_junk_is_none(self, mod):
        assert mod.upgrade_cover_url(None) is None
        assert mod.upgrade_cover_url("") is None
        assert mod.upgrade_cover_url(12345) is None


# ---------------------------------------------------------------------------
# Auth manager / login helpers
# ---------------------------------------------------------------------------


SESSION = {
    "sessionId": "s1",
    "countryCode": "US",
    "userId": "u1",
    "user": {"username": "listener", "email": "a@b.c"},
}


class TestAuthManager:
    def test_no_credentials_means_not_authenticated(self, plugin):
        auth = plugin.mod.TidalAuthManager()
        assert auth.setup_auth() is False
        assert auth.is_authenticated() is False
        assert auth.get_api() is None

    def test_a_file_without_a_refresh_token_is_refused(self, plugin):
        m = plugin.mod
        m.save_tidal_credentials_file("CID", "SECRET", "")
        auth = m.TidalAuthManager()
        assert auth.setup_auth() is False
        assert auth.get_api() is None

    def test_a_good_file_authenticates(self, plugin, monkeypatch):
        m = plugin.mod
        m.save_tidal_credentials_file(**_creds())
        monkeypatch.setattr(m.TidalAPI, "get_session", lambda self: SESSION)
        auth = m.TidalAuthManager()
        assert auth.setup_auth() is True
        assert auth.is_authenticated() is True
        assert auth.get_api().user_id == "u1"

    def test_a_rejected_session_is_not_authenticated(self, plugin, monkeypatch):
        m = plugin.mod
        m.save_tidal_credentials_file(**_creds())
        monkeypatch.setattr(m.TidalAPI, "get_session", lambda self: None)
        auth = m.TidalAuthManager()
        assert auth.setup_auth() is False
        assert auth.get_api() is None

    def test_a_transport_error_is_not_authenticated(self, plugin, monkeypatch):
        m = plugin.mod
        m.save_tidal_credentials_file(**_creds())

        def boom(self):
            raise m.TidalError("Could not reach Tidal")

        monkeypatch.setattr(m.TidalAPI, "get_session", boom)
        assert m.TidalAuthManager().setup_auth() is False


class TestLoginHelpers:
    def test_verify_reports_the_display_name(self, plugin, monkeypatch):
        m = plugin.mod
        monkeypatch.setattr(
            m.TidalAuthManager,
            "verify_credentials",
            staticmethod(lambda *a, **k: (SESSION, None)),
        )
        assert m.verify_tidal_credentials("CID", "SECRET", "RT") == {
            "ok": True,
            "username": "u1",
            "display_name": "listener",
        }

    def test_a_rejected_token_reports_a_reason(self, plugin, monkeypatch):
        m = plugin.mod
        monkeypatch.setattr(
            m.TidalAuthManager,
            "verify_credentials",
            staticmethod(lambda *a, **k: (None, None)),
        )
        result = m.verify_tidal_credentials("CID", "SECRET", "RT")
        assert result["ok"] is False
        assert result["error"]

    def test_verify_first_save_writes_only_on_success(self, plugin, monkeypatch):
        m = plugin.mod
        monkeypatch.setattr(
            m,
            "verify_tidal_credentials",
            lambda *a, **k: {"ok": False, "error": "nope"},
        )
        assert m.save_and_verify_tidal_credentials("CID", "SECRET", "RT")["ok"] is False
        # A typo must never destroy working auth.
        assert not m.tidal_auth_file().exists()

    def test_a_successful_save_persists_the_session_fields(
        self, plugin, monkeypatch
    ):
        m = plugin.mod
        monkeypatch.setattr(
            m,
            "verify_tidal_credentials",
            lambda *a, **k: {"ok": True, "username": "u1"},
        )
        monkeypatch.setattr(m.TidalAPI, "get_session", lambda self: SESSION)
        assert m.save_and_verify_tidal_credentials("CID", "SECRET", "RT")["ok"] is True
        stored = json.loads(
            m.tidal_auth_file().read_text(encoding="utf-8")
        )
        assert stored["user_id"] == "u1"
        assert stored["session_id"] == "s1"
        assert stored["country_code"] == "US"

    def test_a_token_rotated_by_the_round_trip_is_what_gets_stored(
        self, plugin, monkeypatch
    ):
        m = plugin.mod
        monkeypatch.setattr(
            m,
            "_token_post",
            lambda data: {
                "access_token": "AT2",
                "refresh_token": "RT2",
                "expires_in": 3600,
            },
        )

        def session(self):
            # The real round trip refreshes first, and Tidal may rotate the
            # refresh token: the file must carry the NEW one.
            self._refresh_access_token()
            return SESSION

        monkeypatch.setattr(m.TidalAPI, "get_session", session)
        assert m.save_and_verify_tidal_credentials("CID", "SECRET", "RT")["ok"] is True
        stored = json.loads(
            m.tidal_auth_file().read_text(encoding="utf-8")
        )
        assert stored["refresh_token"] == "RT2"

    def test_a_login_without_a_refresh_token_is_refused(self, plugin, monkeypatch):
        m = plugin.mod
        monkeypatch.setattr(
            m, "perform_pkce_login", lambda *a, **k: {"access_token": "AT"}
        )
        result = m.login_with_browser("CID", "SECRET")
        assert result["ok"] is False
        assert "refresh token" in result["error"]
        assert not m.tidal_auth_file().exists()

    def test_a_pkce_failure_is_reported_not_raised(self, plugin, monkeypatch):
        m = plugin.mod

        def boom(*a, **k):
            raise m.TidalError("Timed out after 300s")

        monkeypatch.setattr(m, "perform_pkce_login", boom)
        assert m.login_with_browser("CID", "SECRET") == {
            "ok": False,
            "error": "Timed out after 300s",
        }

    def test_a_successful_login_persists_and_reports(self, plugin, monkeypatch):
        m = plugin.mod
        monkeypatch.setattr(
            m,
            "perform_pkce_login",
            lambda *a, **k: {"access_token": "AT", "refresh_token": "RT"},
        )
        monkeypatch.setattr(m.TidalAPI, "get_session", lambda self: SESSION)
        result = m.login_with_browser("CID", "SECRET")
        assert result["ok"] is True
        assert result["display_name"] == "listener"
        assert m.load_tidal_credentials()["refresh_token"] == "RT"


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------


class TestReceiverHelpers:
    def test_only_loopback_hosts_are_allowed(self):
        mod = _receiver_mod()
        assert mod.is_loopback_host("127.0.0.1:5003")
        assert mod.is_loopback_host("localhost")
        assert mod.is_loopback_host("LOCALHOST:5003")
        assert not mod.is_loopback_host("attacker.example.com")
        # A suffix trick must not pass the prefix match.
        assert not mod.is_loopback_host("127.0.0.1.evil.com:5003")
        assert not mod.is_loopback_host(None)
        assert not mod.is_loopback_host("")

    def test_track_ids_must_be_numeric(self):
        mod = _receiver_mod()
        assert mod.extract_track_id(" 12345 ") == "12345"
        assert mod.extract_track_id("12345") == "12345"
        assert mod.extract_track_id("12/34") is None
        assert mod.extract_track_id("12345; DROP TABLE") is None
        assert mod.extract_track_id("") is None
        assert mod.extract_track_id(None) is None
        assert mod.extract_track_id(12345) is None

    def test_free_text_is_capped(self):
        mod = _receiver_mod()
        assert mod.clean_text("  x  ") == "x"
        assert mod.clean_text("") is None
        assert mod.clean_text(None) is None
        assert len(mod.clean_text("a" * 5000)) == mod.MAX_TEXT_FIELD

    def test_the_port_matches_the_extension_manifest(self):
        mod = _receiver_mod()
        # Two files that must stay in sync by hand.
        import pathlib

        assert mod.DEFAULT_RECEIVER_PORT == 5003
        manifest = (
            pathlib.Path(mod.__file__).parent / "tidal-extension" / "manifest.json"
        )
        perms = " ".join(json.loads(manifest.read_text())["host_permissions"])
        assert "127.0.0.1:5003" in perms


@pytest.fixture
def receiver():
    """A receiver with its Flask app built; no socket is ever bound."""
    mod = _receiver_mod()
    manager = mod.TidalReceiverManager()
    manager._ensure_app()
    return SimpleNamespace(mod=mod, manager=manager, client=manager.app.test_client())


def _token_for(receiver):
    receiver.manager.set_waiting(True)
    return receiver.client.get("/status").get_json()["token"]


class TestReceiverRoutes:
    def test_a_foreign_host_is_refused_before_any_route(self, receiver):
        # DNS rebinding: a page whose domain resolves to 127.0.0.1 can send a
        # well-formed Host header naming that domain, and CORS only stops
        # browsers from *reading* the answer.
        assert (
            receiver.client.get("/status", headers={"Host": "evil.example.com"}).status_code
            == 403
        )
        r = receiver.client.post(
            "/receive-track",
            json={"track_id": "1"},
            headers={"Host": "evil.example.com"},
        )
        assert r.status_code == 403

    def test_status_is_not_ready_until_the_flow_waits(self, receiver):
        assert receiver.client.get("/status").get_json() == {
            "ready": False,
            "token": None,
        }
        receiver.manager.set_waiting(True)
        body = receiver.client.get("/status").get_json()
        assert body["ready"] is True
        assert body["token"]
        receiver.manager.set_waiting(False)
        assert receiver.client.get("/status").get_json()["ready"] is False

    def test_each_run_gets_a_fresh_token(self, receiver):
        receiver.manager.set_waiting(True)
        first = receiver.client.get("/status").get_json()["token"]
        receiver.manager.set_waiting(True)
        second = receiver.client.get("/status").get_json()["token"]
        assert first != second

    def test_a_stale_track_is_drained_when_a_new_run_starts(self, receiver):
        receiver.manager.track_queue.put({"title": "old"})
        receiver.manager.set_waiting(True)
        assert receiver.manager.track_queue.empty()

    def test_a_post_without_a_token_is_refused(self, receiver):
        receiver.manager.set_waiting(True)
        assert receiver.client.post("/receive-track", json={"track_id": "1"}).status_code == 403

    def test_a_post_with_a_wrong_token_is_refused(self, receiver):
        receiver.manager.set_waiting(True)
        r = receiver.client.post(
            "/receive-track", json={"track_id": "1"}, headers={"X-PM-Token": "deadbeef"}
        )
        assert r.status_code == 403

    def test_an_empty_payload_is_refused(self, receiver):
        token = _token_for(receiver)
        r = receiver.client.post(
            "/receive-track", json={}, headers={"X-PM-Token": token}
        )
        assert r.status_code == 400
        assert "No track data" in r.get_json()["error"]

    def test_a_non_numeric_track_id_is_refused(self, receiver):
        token = _token_for(receiver)
        r = receiver.client.post(
            "/receive-track",
            json={"track_id": "../../etc/passwd", "title": "Song"},
            headers={"X-PM-Token": token},
        )
        assert r.status_code == 400
        assert r.get_json()["error"] == "Invalid track ID"

    def test_a_title_only_report_is_accepted(self, receiver):
        # The search fallback exists exactly for this: the id is only
        # available when a hook caught the song starting.
        token = _token_for(receiver)
        r = receiver.client.post(
            "/receive-track",
            json={"title": " Song ", "artist": "Artist", "album": "Album"},
            headers={"X-PM-Token": token},
        )
        assert r.status_code == 200
        assert receiver.manager.get_received_track(timeout=1) == {
            "track_id": None,
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "playing": True,
        }
        # The token is single-use: the run is over.
        assert receiver.client.get("/status").get_json()["ready"] is False

    def test_a_report_with_an_id_and_a_paused_player(self, receiver):
        token = _token_for(receiver)
        r = receiver.client.post(
            "/receive-track",
            json={"track_id": "777", "playing": False},
            headers={"X-PM-Token": token},
        )
        assert r.status_code == 200
        assert r.get_json()["track_id"] == "777"
        track = receiver.manager.get_received_track(timeout=1)
        assert track["track_id"] == "777"
        # Advisory only: a keybind means "add what is loaded".
        assert track["playing"] is False

    def test_a_flood_is_rate_limited(self, receiver):
        receiver.manager._rate_limiter = receiver.mod._RateLimiter(
            max_requests=1, window_seconds=60
        )
        token = _token_for(receiver)
        first = receiver.client.post(
            "/receive-track", json={"title": "a"}, headers={"X-PM-Token": token}
        )
        assert first.status_code == 200
        # Re-arm the consumed token so the limiter is what answers next.
        receiver.manager._token = token
        second = receiver.client.post(
            "/receive-track", json={"title": "b"}, headers={"X-PM-Token": token}
        )
        assert second.status_code == 429

    def test_get_received_track_times_out(self, receiver):
        with pytest.raises(TimeoutError):
            receiver.manager.get_received_track(timeout=1)

    def test_a_stop_while_waiting_is_reported(self, receiver):
        receiver.manager._running = True
        receiver.manager.stop()
        with pytest.raises(RuntimeError, match="stopped"):
            receiver.manager.get_received_track(timeout=1)

    def test_starting_twice_is_a_no_op(self, receiver):
        # start() would bind 5003 for real; the running flag is the guard.
        receiver.manager._running = True
        assert receiver.manager.start() is None
        assert receiver.manager.is_running() is True
        receiver.manager._running = False


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.statuses = []
        self.errors = []
        self.successes = []

    def on_status(self, msg):
        self.statuses.append(msg)

    def on_error(self, msg):
        self.errors.append(msg)

    def on_success(self, result):
        self.successes.append(result)


class _NoopReceiver:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.waiting = None

    def is_running(self):
        return False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def set_waiting(self, waiting):
        self.waiting = waiting

    def get_received_track(self, timeout=None):
        raise TimeoutError("no track")


class _StubApi:
    """Stands in for TidalAPI in the flow/integration tests."""

    user_id = ""

    def __init__(self, **kw):
        self.added = []
        self.favorited = []
        self.search_calls = []
        self.add_result = ["1"]
        self.add_error = None
        self.track = {
            "id": 1,
            "title": "Song",
            "artists": [{"name": "Artist"}],
            "album": {
                "title": "Album",
                "cover": "https://resources.tidal.com/images/u/80x80.jpg",
            },
            "duration": 210,
        }
        self.search_result = None
        self.__dict__.update(kw)

    def get_track(self, track_id):
        return self.track

    def search_track(self, title, artist="", album=""):
        self.search_calls.append((title, artist, album))
        return self.search_result

    def add_tracks_to_playlist(self, playlist_id, track_ids, **kw):
        self.added.append((playlist_id, list(track_ids)))
        if self.add_error:
            raise self.add_error
        return self.add_result

    def favorite_track(self, track_id):
        self.favorited.append(track_id)
        return True


class _StubSongManager:
    def __init__(self, exists=False):
        self.exists = exists
        self.written = []

    def song_exists(self, *a, **k):
        return self.exists

    def get_all_songs(self, *a, **k):
        return []

    def add_song_by_info(
        self, playlist_name, title, artists, duration, sid, thumb, **kw
    ):
        self.written.append((playlist_name, title, sid))
        return 99


def _make_flow(api, song_manager=None, playlist_id="p1", url_receiver=None):
    flow_mod = _flow_mod()
    integration = SimpleNamespace(
        tidal_api=api, get_playlist_id=lambda name: playlist_id
    )
    return flow_mod.TidalFlow(
        integration,
        song_manager or _StubSongManager(),
        url_receiver=url_receiver or _NoopReceiver(),
    )


@pytest.fixture
def registered(monkeypatch):
    flow_mod = _flow_mod()
    monkeypatch.setattr(
        flow_mod.PlaylistStore,
        "playlist_still_registered",
        staticmethod(lambda *a, **k: True),
    )
    return flow_mod


def _run(flow, **kw):
    rec = _Recorder()
    flow.execute_flow(
        "Chill Mix",
        rec.on_status,
        rec.on_error,
        rec.on_success,
        skip_duplicate_check=True,
        **kw,
    )
    return rec


class TestResolveTrack:
    def test_an_id_is_authoritative(self):
        flow = _make_flow(_StubApi())
        track_id, song = flow._resolve_track({"track_id": "1", "title": "Wrong"})
        assert track_id == "1"
        assert song["title"] == "Song"
        assert song["artists"] == ["Artist"]
        assert song["duration"] == 210
        # The 80px cover is bumped so a 64px card stays crisp on HiDPI.
        assert song["thumbnail"].endswith("/320x320.jpg")

    def test_a_missing_id_falls_back_to_search(self):
        api = _StubApi()
        api.search_result = _StubApi().track
        flow = _make_flow(api)
        track_id, song = flow._resolve_track(
            {"title": "Song", "artist": "Artist", "album": "Album"}
        )
        assert track_id == "1"
        assert api.search_calls == [("Song", "Artist", "Album")]

    def test_an_empty_report_resolves_to_nothing(self):
        api = _StubApi()
        assert _make_flow(api)._resolve_track({}) == (None, None)
        assert api.search_calls == []

    def test_an_unmatched_search_resolves_to_nothing(self):
        api = _StubApi(search_result=None)
        assert _make_flow(api)._resolve_track({"title": "Nothing Like It"}) == (
            None,
            None,
        )

    def test_a_search_hit_without_an_id_is_refused(self):
        api = _StubApi(search_result={"title": "Song", "artists": []})
        assert _make_flow(api)._resolve_track({"title": "Song"}) == (None, None)

    def test_no_client_is_a_human_message_not_an_attribute_error(self):
        flow = _make_flow(_StubApi())
        flow.tidal_api = None
        with pytest.raises(RuntimeError, match="not authenticated"):
            flow._resolve_track({"track_id": "1"})

    def test_an_unknown_track_is_reported(self):
        with pytest.raises(ValueError, match="not found"):
            _make_flow(_StubApi(track=None)).fetch_track_details("1")

    def test_a_track_without_artists_still_shapes_up(self):
        api = _StubApi(track={"id": 3, "title": "Song", "duration": 10})
        _track_id, song = _make_flow(api)._resolve_track({"track_id": "3"})
        assert song["artists"] == ["Unknown Artist"]
        assert song["thumbnail"] is None


class TestExecuteFlow:
    def test_the_platform_is_written_first_then_the_local_db(self, registered):
        api = _StubApi()
        sm = _StubSongManager()
        rec = _run(_make_flow(api, sm), song_data={"track_id": "1", "title": "Song"})
        assert api.added == [("p1", ["1"])]
        assert sm.written == [("Chill Mix", "Song", "1")]
        assert rec.errors == []
        assert rec.successes[0]["status"] == "added"

    def test_a_refusal_aborts_before_the_local_write(self, registered):
        api = _StubApi(add_error=_mod().TidalError("Playlist is read only"))
        sm = _StubSongManager()
        rec = _run(_make_flow(api, sm), song_data={"track_id": "1", "title": "Song"})
        assert len(rec.errors) == 1
        assert "read only" in rec.errors[0]
        assert rec.successes == []
        # The add-flow invariant: never a local-only row.
        assert sm.written == []

    def test_a_2xx_that_added_nothing_is_treated_as_a_refusal(self, registered):
        api = _StubApi(add_result=[])
        sm = _StubSongManager()
        rec = _run(_make_flow(api, sm), song_data={"track_id": "1", "title": "Song"})
        assert "did not add" in rec.errors[0]
        assert sm.written == []

    def test_a_favourites_target_routes_to_the_favourites_api(self, registered):
        api = _StubApi()
        sm = _StubSongManager()
        rec = _run(
            _make_flow(api, sm, playlist_id="__favorites__"),
            song_data={"track_id": "1", "title": "Song"},
        )
        assert api.favorited == ["1"]
        assert api.added == []
        assert rec.successes[0]["status"] == "added"

    def test_an_unknown_playlist_aborts(self, registered):
        api = _StubApi()
        sm = _StubSongManager()
        rec = _run(
            _make_flow(api, sm, playlist_id=None),
            song_data={"track_id": "1", "title": "Song"},
        )
        assert "Could not find Tidal playlist" in rec.errors[0]
        assert sm.written == []

    def test_an_already_present_song_does_not_touch_the_platform(self, registered):
        api = _StubApi()
        rec = _run(
            _make_flow(api, _StubSongManager(exists=True)),
            song_data={"track_id": "1", "title": "Song"},
        )
        assert rec.successes[0]["status"] == "exists"
        assert api.added == []

    def test_a_record_without_an_id_is_refused_before_any_call(self, registered):
        api = _StubApi()
        rec = _run(_make_flow(api), song_data={"title": "Song"})
        assert "No track id" in rec.errors[0]
        assert api.added == []

    def test_a_playlist_removed_between_the_checks_keeps_the_platform_write(
        self, monkeypatch
    ):
        flow_mod = _flow_mod()
        calls = []

        def registered_check(name, platform, playlist_id):
            calls.append(name)
            return len(calls) == 1

        monkeypatch.setattr(
            flow_mod.PlaylistStore,
            "playlist_still_registered",
            staticmethod(registered_check),
        )
        api = _StubApi()
        sm = _StubSongManager()
        rec = _run(_make_flow(api, sm), song_data={"track_id": "1", "title": "Song"})
        # The platform write already happened; the local row is skipped
        # rather than written for a playlist that no longer exists.
        assert api.added == [("p1", ["1"])]
        assert sm.written == []
        assert rec.successes[0]["status"] == "added"
        assert rec.successes[0]["song_id"] is None

    def test_a_playlist_removed_before_the_add_aborts(self, monkeypatch):
        flow_mod = _flow_mod()
        monkeypatch.setattr(
            flow_mod.PlaylistStore,
            "playlist_still_registered",
            staticmethod(lambda *a, **k: False),
        )
        api = _StubApi()
        rec = _run(_make_flow(api), song_data={"track_id": "1", "title": "Song"})
        assert "removed while the flow" in rec.errors[0]
        assert api.added == []

    def test_an_unidentified_capture_is_reported(self, registered):
        api = _StubApi()
        flow = _make_flow(api)
        flow._get_track_from_receiver = lambda timeout=30: {"title": "Nothing"}
        rec = _run(flow)
        assert "Could not identify the track" in rec.errors[0]
        assert api.added == []

    def test_an_empty_capture_is_reported(self, registered):
        api = _StubApi()
        flow = _make_flow(api)
        flow._get_track_from_receiver = lambda timeout=30: {}
        rec = _run(flow)
        assert "No track data received" in rec.errors[0]

    def test_a_receiver_timeout_is_reported(self, registered):
        flow = _make_flow(_StubApi())
        rec = _run(flow)
        assert rec.errors and rec.errors[0].startswith("Timeout")

    def test_the_receiver_is_stopped_even_on_failure(self, registered):
        api = _StubApi(add_error=_mod().TidalError("nope"))
        receiver = _NoopReceiver()
        receiver.is_running = lambda: True
        _run(
            _make_flow(api, url_receiver=receiver),
            song_data={"track_id": "1", "title": "Song"},
        )
        assert receiver.stopped is True

    def test_the_playlist_id_lookup_is_cached(self):
        flow_mod = _flow_mod()
        calls = []
        integration = SimpleNamespace(
            tidal_api=_StubApi(),
            get_playlist_id=lambda n: calls.append(n) or "p1",
        )
        flow = flow_mod.TidalFlow(
            integration, _StubSongManager(), _NoopReceiver()
        )
        assert flow._get_playlist_id("Chill Mix") == "p1"
        assert flow._get_playlist_id("Chill Mix") == "p1"
        assert calls == ["Chill Mix"]

    def test_a_negative_lookup_is_cached_too(self):
        # A stale name must not trigger a full library scan per keybind.
        flow_mod = _flow_mod()
        calls = []
        integration = SimpleNamespace(
            tidal_api=_StubApi(),
            get_playlist_id=lambda n: calls.append(n) or None,
        )
        flow = flow_mod.TidalFlow(
            integration, _StubSongManager(), _NoopReceiver()
        )
        assert flow._get_playlist_id("Gone") is None
        assert flow._get_playlist_id("Gone") is None
        assert calls == ["Gone"]

    def test_a_known_id_short_circuits_the_lookup(self):
        flow_mod = _flow_mod()
        integration = SimpleNamespace(
            tidal_api=_StubApi(),
            get_playlist_id=lambda n: pytest.fail("must not be called"),
        )
        flow = flow_mod.TidalFlow(
            integration, _StubSongManager(), _NoopReceiver()
        )
        assert flow._get_playlist_id("Chill Mix", known_id="p9") == "p9"


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


def _integration(api=None):
    m = _integration_mod()
    integration = m.TidalIntegration()
    integration.tidal_api = api
    return integration, m


class TestIntegration:
    def test_unauthenticated_calls_degrade(self):
        integration, _ = _integration()
        assert integration.is_authenticated() is False
        assert integration.get_library_playlists() == []
        assert integration.get_playlist_details("p1") == {}
        assert integration.get_playlist_tracks("p1") == []
        assert integration.get_playlist_id("Chill Mix") is None
        assert integration.add_tracks_to_playlist("p1", ["1"]) is False
        assert integration.favorite_track("1") is False
        assert integration.remove_track("p1", "1") is False

    def test_authenticate_takes_the_clients_api(self):
        integration, _ = _integration()
        api = _StubApi()
        integration.auth_manager = SimpleNamespace(get_api=lambda: api)
        assert integration.authenticate() is True
        assert integration.tidal_api is api

    def test_authenticate_without_a_manager_is_false(self):
        integration, _ = _integration()
        assert integration.authenticate() is False

    def test_the_library_offers_favorites_and_upgraded_covers(self):
        api = _StubApi()
        api.get_playlists = lambda: [
            {
                "uuid": "p1",
                "title": "Chill Mix",
                "numberOfTracks": 3,
                "image": "https://resources.tidal.com/images/u/80x80.jpg",
            },
            {"title": "No uuid"},
        ]
        integration, m = _integration(api)
        out = integration.get_library_playlists()
        assert [p["playlistId"] for p in out] == ["p1", m.FAVORITES_ID]
        assert out[0]["thumbnail"].endswith("/320x320.jpg")
        assert out[0]["trackCount"] == 3
        assert out[1]["title"] == m.FAVORITES_TITLE

    def test_a_followed_playlist_is_kept_out_of_the_picker(self):
        # Tidal's playlist list can carry playlists the user only follows,
        # and those reject adds: an entry there is a keybind that can only
        # ever fail.
        api = _StubApi(
            user_id="u1",
            get_playlists=lambda: [
                {"uuid": "own", "title": "Mine", "owner": {"id": "u1"}},
                {
                    "uuid": "collab",
                    "title": "Shared",
                    "owner": {"id": "u2"},
                    "isCollaborative": True,
                },
                {"uuid": "followed", "title": "Theirs", "owner": {"id": "u2"}},
            ],
        )
        integration, m = _integration(api)
        out = integration.get_library_playlists()
        assert [p["playlistId"] for p in out] == ["own", "collab", m.FAVORITES_ID]

    def test_a_followed_playlist_cannot_be_addressed_by_name(self):
        # Same filter in the lookup, so a stale name fails fast with the
        # flow's own message instead of a 403 in the middle of an add.
        api = _StubApi(
            user_id="u1",
            get_playlists=lambda: [
                {"uuid": "own", "title": "Mine", "owner": {"id": "u1"}},
                {"uuid": "followed", "title": "Theirs", "owner": {"id": "u2"}},
            ],
        )
        integration, _ = _integration(api)
        assert integration.get_playlist_id("Mine") == "own"
        assert integration.get_playlist_id("Theirs") is None

    def test_an_unknown_user_id_drops_nothing(self):
        # Without the id nothing can be told apart, so the picker stays
        # complete and the write surfaces the platform's own reason.
        api = _StubApi(
            get_playlists=lambda: [
                {"uuid": "p1", "title": "Theirs", "owner": {"id": "u2"}}
            ]
        )
        integration, _ = _integration(api)
        assert [p["playlistId"] for p in integration.get_library_playlists()][:1] == [
            "p1"
        ]
        assert integration.get_playlist_id("Theirs") == "p1"

    def test_a_stale_user_id_empties_nothing(self):
        # The credential file can outlive an account change.  Filtering on
        # an id that owns nothing would leave the picker with no real entry
        # at all - a worse failure than a playlist whose write is refused.
        api = _StubApi(
            user_id="old-account",
            get_playlists=lambda: [
                {"uuid": "p1", "title": "Theirs", "owner": {"id": "u2"}}
            ],
        )
        integration, _ = _integration(api)
        assert [p["playlistId"] for p in integration.get_library_playlists()][:1] == [
            "p1"
        ]
        assert integration.get_playlist_id("Theirs") == "p1"

    def test_remove_track_reports_a_refusal_instead_of_raising(self):
        # The core calls this from a worker thread with no handler of its
        # own: a raise would leave the local row neither deleted nor
        # recorded as failed.
        api = _StubApi()

        def boom(*a, **k):
            raise _mod().TidalError("You are not the owner")

        api.remove_track_from_playlist = boom
        integration, _ = _integration(api)
        assert integration.remove_track("p1", "1") is False

    def test_unfavoriting_reports_a_refusal_instead_of_raising(self):
        api = _StubApi()

        def boom(*a, **k):
            raise _mod().TidalError("nope")

        api.unfavorite_track = boom
        integration, m = _integration(api)
        assert integration.remove_track(m.FAVORITES_ID, "1") is False

    def test_favoriting_reports_a_refusal_instead_of_raising(self):
        api = _StubApi()

        def boom(*a, **k):
            raise _mod().TidalError("Track is not available")

        api.favorite_track = boom
        integration, _ = _integration(api)
        assert integration.favorite_track("1") is False

    def test_a_failing_library_scan_degrades_to_empty(self):
        api = _StubApi()

        def boom(*a, **k):
            raise _mod().TidalError("nope")

        api.get_playlists = boom
        integration, _ = _integration(api)
        assert integration.get_library_playlists() == []

    def test_favorites_is_addressable_by_name(self):
        integration, m = _integration(_StubApi())
        assert integration.get_playlist_id(m.FAVORITES_TITLE) == m.FAVORITES_ID

    def test_favorites_details_are_never_empty(self):
        # An empty dict reads as "unreachable" to the reload path, which
        # would then refuse to touch the local DB.
        integration, m = _integration(_StubApi())
        details = integration.get_playlist_details(m.FAVORITES_ID)
        assert details["title"] == m.FAVORITES_TITLE
        assert "thumbnails" in details

    def test_playlist_details_upgrade_the_cover(self):
        api = _StubApi()
        api.get_playlist = lambda pid: (
            {
                "title": "Chill Mix",
                "numberOfTracks": 7,
                "image": "https://resources.tidal.com/images/u/80x80.jpg",
                "owner": {"id": 5},
                "isCollaborative": True,
            },
            "etag",
        )
        integration, _ = _integration(api)
        details = integration.get_playlist_details("p1")
        assert details["title"] == "Chill Mix"
        assert details["trackCount"] == 7
        assert details["owner_id"] == "5"
        assert details["collaborative"] is True
        assert details["thumbnails"][0]["url"].endswith("/320x320.jpg")

    def test_favorites_tracks_come_from_the_favourites_api(self):
        api = _StubApi()
        api.get_favorite_tracks = lambda: [{"id": 1}]
        api.get_all_playlist_items = lambda pid: pytest.fail("wrong API")
        integration, m = _integration(api)
        assert integration.get_playlist_tracks(m.FAVORITES_ID) == [{"id": 1}]

    def test_a_refused_add_is_reported_as_false(self):
        api = _StubApi(add_error=_mod().TidalError("read only"))
        integration, _ = _integration(api)
        assert integration.add_tracks_to_playlist("p1", ["1"]) is False

    def test_an_empty_add_is_reported_as_false(self):
        api = _StubApi(add_result=[])
        integration, _ = _integration(api)
        assert integration.add_tracks_to_playlist("p1", ["1"]) is False

    def test_removing_from_favorites_unfavourites(self):
        api = _StubApi()
        api.unfavorite_track = lambda tid: True
        integration, m = _integration(api)
        assert integration.remove_track(m.FAVORITES_ID, "1") is True

    def test_a_playlist_lookup_is_cached(self):
        api = _StubApi()
        calls = []

        def playlists():
            calls.append(1)
            return [{"uuid": "p1", "title": "Chill Mix"}]

        api.get_playlists = playlists
        integration, _ = _integration(api)
        assert integration.get_playlist_id("Chill Mix") == "p1"
        assert integration.get_playlist_id("Chill Mix") == "p1"
        assert len(calls) == 1

    def test_a_lookup_failure_is_reported_not_raised(self):
        api = _StubApi()

        def boom(*a, **k):
            raise _mod().TidalError("nope")

        api.get_playlists = boom
        integration, _ = _integration(api)
        assert integration.get_playlist_id("Chill Mix") is None

    def test_a_failed_re_auth_drops_the_client_and_the_cache(self):
        api = _StubApi()
        api.get_playlists = lambda: [{"uuid": "p1", "title": "Chill Mix"}]
        integration, _ = _integration(api)
        assert integration.get_playlist_id("Chill Mix") == "p1"
        integration.auth_manager = SimpleNamespace(get_api=lambda: None)
        assert integration.refresh_auth() is False
        assert integration.tidal_api is None
        assert integration._playlist_id_cache == {}


# ---------------------------------------------------------------------------
# Core wiring
# ---------------------------------------------------------------------------


class TestSongManagerExtractor:
    def test_a_playlist_item_is_unwrapped(self):
        entry = {
            "type": "TRACK",
            "item": {
                "id": 12345,
                "title": "Song",
                "artists": [{"name": "A"}, {"name": "B"}, {}],
                "duration": 210,
                "album": {"cover": "https://resources.tidal.com/images/u/80x80.jpg"},
            },
        }
        title, artists, duration, track_id, thumb = song_manager._extract_tidal_track(
            entry
        )
        assert (title, artists, duration, track_id) == ("Song", ["A", "B"], 210, "12345")
        assert thumb.endswith("/320x320.jpg")

    def test_a_bare_track_object_works_too(self):
        title, artists, duration, track_id, thumb = song_manager._extract_tidal_track(
            {"id": 1, "title": "Song", "duration": 100}
        )
        assert artists == ["Unknown Artist"]
        assert (duration, track_id, thumb) == (100, "1", None)

    def test_an_entry_without_an_id_is_skipped(self):
        assert song_manager._extract_tidal_track({"item": {"title": "x"}}) is None

    def test_the_platform_is_registered(self):
        assert (
            song_manager._TRACK_EXTRACTORS["tidal"]
            is song_manager._extract_tidal_track
        )


class TestHealthProbe:
    def test_no_client_means_no_probe(self):
        integration, _ = _integration()
        assert health_check._probe_tidal(integration) == (None, None)

    def test_a_live_session_is_healthy(self):
        api = _StubApi()
        api.get_me = lambda: {"id": "u1"}
        integration, _ = _integration(api)
        assert health_check._probe_tidal(integration) == (True, None)

    def test_a_dead_token_is_unhealthy_with_a_reason(self):
        api = _StubApi()

        def boom():
            raise _mod().TidalError("Failed to refresh the Tidal access token")

        api.get_me = boom
        integration, _ = _integration(api)
        ok, err = health_check._probe_tidal(integration)
        assert ok is False
        assert "refresh" in err

    def test_an_empty_profile_is_not_healthy(self):
        api = _StubApi(get_me=lambda: None)
        integration, _ = _integration(api)
        assert health_check._probe_tidal(integration) == (False, None)

    def test_the_tcp_target_and_the_probe_are_registered(self):
        assert health_check.PLATFORM_API_TARGETS["tidal"] == ("api.tidal.com", 443)
        assert health_check._AUTH_PROBES["tidal"] is health_check._probe_tidal


class TestAuthSetupWrapper:
    def test_load_returns_empty_when_there_is_no_file(self, plugin, monkeypatch):
        monkeypatch.setattr(auth_setup, "TIDAL_FILE", plugin.auth / "tidal.json")
        assert auth_setup.load_tidal_credentials() == {}

    def test_load_serves_the_plugin_file(self, plugin, monkeypatch):
        plugin.mod.save_tidal_credentials_file(**_creds())
        monkeypatch.setattr(auth_setup, "TIDAL_FILE", plugin.mod.tidal_auth_file())
        assert auth_setup.load_tidal_credentials() == _creds()

    def test_a_missing_plugin_degrades_instead_of_raising(self, plugin, monkeypatch):
        monkeypatch.setattr(auth_setup, "TIDAL_FILE", plugin.auth / "tidal.json")
        monkeypatch.setitem(sys.modules, "integrations.tidal.tidal", None)
        assert auth_setup.load_tidal_credentials() == {}

    def test_a_browser_login_mirrors_the_keyring_on_success(
        self, plugin, monkeypatch
    ):
        mirrored = []
        monkeypatch.setattr(
            auth_setup.credential_store, "mirror", lambda name: mirrored.append(name)
        )
        monkeypatch.setattr(
            plugin.mod,
            "login_with_browser",
            lambda cid, secret, on_url=None: {"ok": True, "display_name": "listener"},
        )
        assert auth_setup.tidal_browser_login("CID", "SECRET") == {
            "ok": True,
            "display_name": "listener",
        }
        # Without the mirror, logout/uninstall could not clear the token.
        assert mirrored == ["tidal.json"]

    def test_a_failed_login_mirrors_nothing(self, plugin, monkeypatch):
        mirrored = []
        monkeypatch.setattr(
            auth_setup.credential_store, "mirror", lambda name: mirrored.append(name)
        )
        monkeypatch.setattr(
            plugin.mod,
            "login_with_browser",
            lambda cid, secret, on_url=None: {"ok": False, "error": "nope"},
        )
        assert auth_setup.tidal_browser_login("CID", "SECRET")["ok"] is False
        assert mirrored == []

    def test_an_unexpected_error_is_reported_not_raised(self, plugin, monkeypatch):
        def boom(cid, secret, on_url=None):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(plugin.mod, "login_with_browser", boom)
        assert auth_setup.tidal_browser_login("CID", "SECRET") == {
            "ok": False,
            "error": "kaboom",
        }
