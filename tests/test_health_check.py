"""
Tests for :mod:`services.health_check`: TCP probes and the authenticated
per-platform probes used by the Manage dialog's status refresh.

All authenticated-probe tests use duck-typed fakes - the probes reach into
the integration object's public handles (``spotify_api``, ``yt_client``,
``soundcloud_api``, ``deezer_api``, ``client``) without importing plugin
code, mirroring how the production callers hand over a live integration.
"""

import time
from socket import timeout as SocketTimeout

import pytest

from services import health_check


class _FakeClient:
    """Duck-typed stand-in for any plugin API handle.

    Every probe-relevant method (``get_me``, ``get_library_playlists``,
    ``get_loved_tracks``, ...) behaves identically via ``__getattr__``:
    record the call, optionally sleep, then return ``result`` or raise
    ``exc``.  ``username`` is a real attribute so the Last.fm guard can
    read it (a missing attribute would resolve through ``__getattr__``).
    """

    def __init__(self, result=None, exc=None, delay=0.0, username=None):
        self.result = result
        self.exc = exc
        self.delay = delay
        self.username = username
        self.calls = 0

    def __getattr__(self, name):
        def _call(*_args, **_kwargs):
            self.calls += 1
            if self.delay:
                time.sleep(self.delay)
            if self.exc is not None:
                raise self.exc
            return self.result

        return _call


def _integration(pid: str, **handles) -> object:
    """Build a duck-typed integration object with the given public handles."""
    integration = type("FakeIntegration", (), {})()
    integration.id = pid
    for name, value in handles.items():
        setattr(integration, name, value)
    return integration


# ---------------------------------------------------------------------------
# TCP probes (stubbed socket)
# ---------------------------------------------------------------------------


class _FakeSocket:
    """A connected-socket stand-in that records whether it was released."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _stub_connect(monkeypatch, *, exc=None):
    """Replace ``socket.create_connection`` with a recording stub.

    Returns ``(calls, sockets)``: *calls* records each
    ``(host, port, timeout)``; *sockets* holds the fake socket handed back
    on success (empty when *exc* is given).
    """
    calls = []
    sockets = []

    def _create_connection(address, timeout=None):
        host, port = address
        calls.append((host, port, timeout))
        if exc is not None:
            raise exc
        sock = _FakeSocket()
        sockets.append(sock)
        return sock

    monkeypatch.setattr(
        health_check.socket, "create_connection", _create_connection
    )
    return calls, sockets


class TestCheckNetwork:
    def test_ok_probes_dns_endpoint_and_releases_socket(self, monkeypatch):
        calls, sockets = _stub_connect(monkeypatch)
        assert health_check.check_network() is True
        assert calls == [("1.1.1.1", 53, health_check._INTERNET_TIMEOUT_S)]
        # The connected socket is closed deterministically, not left to GC.
        assert len(sockets) == 1
        assert sockets[0].closed is True

    def test_forwards_timeout(self, monkeypatch):
        calls, _ = _stub_connect(monkeypatch)
        health_check.check_network(timeout=0.25)
        assert calls == [("1.1.1.1", 53, 0.25)]

    def test_refused_returns_false(self, monkeypatch):
        calls, sockets = _stub_connect(monkeypatch, exc=OSError("refused"))
        assert health_check.check_network() is False
        assert len(calls) == 1
        assert sockets == []

    def test_timed_out_returns_false(self, monkeypatch):
        _stub_connect(monkeypatch, exc=SocketTimeout("timed out"))
        assert health_check.check_network() is False


class TestProbePlatform:
    @pytest.mark.parametrize(
        "platform, host, port",
        [
            ("youtube_music", "music.youtube.com", 443),
            ("spotify", "api.spotify.com", 443),
            ("soundcloud", "api.soundcloud.com", 443),
            ("deezer", "pipe.deezer.com", 443),
        ],
    )
    def test_targets_and_releases_socket(self, monkeypatch, platform, host, port):
        calls, sockets = _stub_connect(monkeypatch)
        assert health_check.probe_platform(platform) is True
        assert calls == [(host, port, health_check._API_TIMEOUT_S)]
        assert len(sockets) == 1
        assert sockets[0].closed is True

    def test_forwards_timeout(self, monkeypatch):
        calls, _ = _stub_connect(monkeypatch)
        health_check.probe_platform("spotify", timeout=0.1)
        assert calls == [("api.spotify.com", 443, 0.1)]

    def test_unknown_platform_never_touches_socket(self, monkeypatch):
        calls, _ = _stub_connect(monkeypatch)
        assert health_check.probe_platform("mystery_platform") is False
        assert calls == []

    def test_connect_failure_returns_false(self, monkeypatch):
        calls, sockets = _stub_connect(monkeypatch, exc=OSError("refused"))
        assert health_check.probe_platform("spotify") is False
        assert len(calls) == 1
        assert sockets == []

    def test_timeout_returns_false(self, monkeypatch):
        _stub_connect(monkeypatch, exc=SocketTimeout("timed out"))
        assert health_check.probe_platform("spotify") is False


class TestProbePlatforms:
    def test_filters_unknown_and_releases_each_socket(self, monkeypatch):
        calls, sockets = _stub_connect(monkeypatch)
        result = health_check.probe_platforms({"spotify", "deezer", "mystery"})
        assert result == {"spotify": True, "deezer": True}
        # Set iteration order is unspecified - compare as sets.
        assert set(calls) == {
            ("api.spotify.com", 443, health_check._API_TIMEOUT_S),
            ("pipe.deezer.com", 443, health_check._API_TIMEOUT_S),
        }
        assert len(sockets) == 2
        assert all(sock.closed for sock in sockets)

    def test_empty_set_no_probes(self, monkeypatch):
        calls, _ = _stub_connect(monkeypatch)
        assert health_check.probe_platforms(set()) == {}
        assert calls == []


# ---------------------------------------------------------------------------
# Authenticated probe: dispatch + capability hook
# ---------------------------------------------------------------------------


def test_probe_authenticated_unknown_platform():
    assert health_check.probe_authenticated(_integration("mystery_platform")) == (
        None,
        None,
    )


def test_probe_authenticated_empty_id():
    assert health_check.probe_authenticated(_integration("")) == (None, None)


def test_probe_authenticated_plugin_capability_takes_precedence():
    integration = type(
        "Cap",
        (),
        {
            "id": "spotify",  # map probe would run - capability wins
            "probe_authenticated": lambda self: True,
        },
    )()
    assert health_check.probe_authenticated(integration) == (True, None)


def test_capability_false_and_none_and_tuple():
    integration = type(
        "Cap", (), {"id": "custom", "probe_authenticated": lambda self: False}
    )()
    assert health_check.probe_authenticated(integration) == (False, None)

    integration.probe_authenticated = lambda: None
    assert health_check.probe_authenticated(integration) == (None, None)

    integration.probe_authenticated = lambda: (False, "401 Unauthorized")
    assert health_check.probe_authenticated(integration) == (
        False,
        "401 Unauthorized",
    )


def test_capability_non_boolean_ok_coerced_to_none():
    # Contract-violating ok values must not render as a verdict.
    integration = type(
        "Cap",
        (),
        {"id": "custom", "probe_authenticated": lambda self: ("truthy", "x")},
    )()
    assert health_check.probe_authenticated(integration) == (None, "x")


def test_capability_exception_reported_as_failed():
    integration = type(
        "Cap",
        (),
        {"id": "custom", "probe_authenticated": lambda self: 1 / 0},
    )()
    ok, detail = health_check.probe_authenticated(integration)
    assert ok is False
    assert "division" in detail


def test_probe_authenticated_timeout_returns_unknown():
    integration = type(
        "Cap",
        (),
        {
            "id": "custom",
            "probe_authenticated": lambda self: time.sleep(0.5) or True,
        },
    )()
    # The watchdog thread outlives the call; the verdict stays unknown.
    assert health_check.probe_authenticated(integration, timeout=0.02) == (
        None,
        None,
    )


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------


def test_spotify_missing_api_no_probe():
    assert health_check.probe_authenticated(_integration("spotify")) == (
        None,
        None,
    )


def test_spotify_auth_ok():
    api = _FakeClient(result={"id": "u1", "display_name": "Me"})
    integration = _integration("spotify", spotify_api=api)
    assert health_check.probe_authenticated(integration) == (True, None)
    assert api.calls == 1


def test_spotify_auth_rejected():
    # get_me returns None on a failed call / failed post-401 refresh.
    api = _FakeClient(result=None)
    integration = _integration("spotify", spotify_api=api)
    assert health_check.probe_authenticated(integration) == (False, None)


def test_spotify_refresh_token_dead():
    # The failed token refresh surfaces as a RuntimeError - the definitive
    # "credentials are dead" signal.
    api = _FakeClient(exc=RuntimeError("Failed to refresh Spotify access token"))
    integration = _integration("spotify", spotify_api=api)
    ok, detail = health_check.probe_authenticated(integration)
    assert ok is False
    assert "refresh" in detail


def test_spotify_unexpected_error():
    api = _FakeClient(exc=ValueError("boom"))
    integration = _integration("spotify", spotify_api=api)
    ok, detail = health_check.probe_authenticated(integration)
    assert ok is False
    assert detail == "boom"


# ---------------------------------------------------------------------------
# YouTube Music
# ---------------------------------------------------------------------------


def test_ytmusic_missing_client_no_probe():
    assert health_check.probe_authenticated(_integration("youtube_music")) == (
        None,
        None,
    )


def test_ytmusic_auth_ok():
    client = _FakeClient(result=[])
    integration = _integration("youtube_music", yt_client=client)
    assert health_check.probe_authenticated(integration) == (True, None)
    assert client.calls == 1


def test_ytmusic_auth_rejected():
    client = _FakeClient(exc=RuntimeError("token expired"))
    integration = _integration("youtube_music", yt_client=client)
    ok, detail = health_check.probe_authenticated(integration)
    assert ok is False
    assert "token" in detail


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------


def test_soundcloud_missing_api_no_probe():
    assert health_check.probe_authenticated(_integration("soundcloud")) == (
        None,
        None,
    )


def test_soundcloud_auth_ok():
    api = _FakeClient(result={"id": 42})
    integration = _integration("soundcloud", soundcloud_api=api)
    assert health_check.probe_authenticated(integration) == (True, None)


def test_soundcloud_auth_rejected():
    api = _FakeClient(result=None)
    integration = _integration("soundcloud", soundcloud_api=api)
    assert health_check.probe_authenticated(integration) == (False, None)


# ---------------------------------------------------------------------------
# Deezer
# ---------------------------------------------------------------------------


def test_deezer_missing_api_no_probe():
    assert health_check.probe_authenticated(_integration("deezer")) == (
        None,
        None,
    )


def test_deezer_auth_ok():
    api = _FakeClient(result={"me": {"id": "7"}})
    integration = _integration("deezer", deezer_api=api)
    assert health_check.probe_authenticated(integration) == (True, None)


def test_deezer_auth_rejected():
    api = _FakeClient(result=None)
    integration = _integration("deezer", deezer_api=api)
    assert health_check.probe_authenticated(integration) == (False, None)


# ---------------------------------------------------------------------------
# Last.fm (service plugin, client + username)
# ---------------------------------------------------------------------------


def test_lastfm_missing_client_no_probe():
    assert health_check.probe_authenticated(_integration("lastfm")) == (
        None,
        None,
    )


def test_lastfm_no_username_no_round_trip():
    # Without a username get_loved_tracks returns {} without calling the
    # API - that would be a false positive, so no probe applies.
    client = _FakeClient(result={}, username="")
    integration = _integration("lastfm", client=client)
    assert health_check.probe_authenticated(integration) == (None, None)
    assert client.calls == 0


def test_lastfm_auth_ok():
    client = _FakeClient(result={("a", "b"): True}, username="d1pl")
    integration = _integration("lastfm", client=client)
    assert health_check.probe_authenticated(integration) == (True, None)
    assert client.calls == 1


def test_lastfm_session_key_dead():
    # A dead session key makes the API respond with an error, raised as
    # LastFMError by the client.
    client = _FakeClient(exc=RuntimeError("Invalid session key"), username="d1pl")
    integration = _integration("lastfm", client=client)
    ok, detail = health_check.probe_authenticated(integration)
    assert ok is False
    assert "session" in detail