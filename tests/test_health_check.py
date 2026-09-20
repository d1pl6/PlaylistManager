"""
Tests for :mod:`services.health_check`: TCP probes and the authenticated
per-platform probes used by the Manage dialog's status refresh.

All authenticated-probe tests use duck-typed fakes - the probes reach into
the integration object's public handles (``spotify_api``, ``yt_client``,
``soundcloud_api``, ``deezer_api``, ``client``) without importing plugin
code, mirroring how the production callers hand over a live integration.
"""

import time

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
# TCP probes
# ---------------------------------------------------------------------------


def test_check_network_ok(monkeypatch):
    monkeypatch.setattr(
        health_check.socket, "create_connection", lambda *a, **kw: None
    )
    assert health_check.check_network() is True


def test_check_network_failure(monkeypatch):
    def _refused(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(health_check.socket, "create_connection", _refused)
    assert health_check.check_network() is False


def test_probe_platform_ok(monkeypatch):
    monkeypatch.setattr(
        health_check.socket, "create_connection", lambda *a, **kw: None
    )
    assert health_check.probe_platform("spotify") is True


def test_probe_platform_connect_failure(monkeypatch):
    def _timeout(*a, **kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(health_check.socket, "create_connection", _timeout)
    assert health_check.probe_platform("spotify") is False


def test_probe_platform_unknown_platform():
    # No target in PLATFORM_API_TARGETS -> nothing to reach -> False.
    assert health_check.probe_platform("mystery_platform") is False


def test_probe_platforms_filters_unknown(monkeypatch):
    monkeypatch.setattr(
        health_check.socket, "create_connection", lambda *a, **kw: None
    )
    assert health_check.probe_platforms({"spotify", "mystery"}) == {
        "spotify": True
    }


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