"""Tests for :mod:`services.scrobble_service` - the shared
"capture the currently-playing song and scrobble it" orchestration.

The policy under test: iterate platforms in registry order, skip what
cannot produce a song (unauthenticated, no plugin, no flow, silent
capture), stop at the FIRST platform that captures a song, and never
let one platform's capture/scrobble failure sink the action - the next
platform gets the chance.

Also pins the two integration-selection modes: the default registry
lookup gates on ``is_authenticated()``, while an ``integration_provider``
replaces the lookup AND the gate (the CLI authenticates one platform at
a time, right before its capture attempt).

All integrations/plugins/flows are duck-typed fakes - no plugin code,
no network, no display.
"""

import pytest

from services import scrobble_service


class _FakePlugin:
    def __init__(self, flow_class=True):
        self.flow_class = flow_class


class _FakeRegistry:
    def __init__(self, ids=("youtube_music", "spotify"), plugins=None):
        self.ids = list(ids)
        self.plugins = (
            {pid: _FakePlugin() for pid in self.ids} if plugins is None else dict(plugins)
        )

    def get_platform_ids(self):
        return list(self.ids)

    def get(self, pid):
        return self.plugins.get(pid)


class _FakeFlow:
    """flow.capture(timeout) returns a fixed (url, song_data, error)."""

    def __init__(self, song_data=None, exc=None):
        self.song_data = song_data
        self.exc = exc
        self.calls = 0
        self.timeouts = []

    def capture(self, timeout):
        self.calls += 1
        self.timeouts.append(timeout)
        if self.exc is not None:
            raise self.exc
        return "https://song.example/1", self.song_data, ""


class _FakeIntegration:
    def __init__(self, authenticated=True):
        self.authenticated = authenticated

    def is_authenticated(self):
        return self.authenticated


class _FakeIntegrations:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, pid):
        return self.mapping.get(pid)


class _FakeScrobbler:
    def __init__(self, ts=123456789, exc=None):
        self.ts = ts
        self.exc = exc
        self.calls = []

    def scrobble(self, song_data):
        self.calls.append(song_data)
        if self.exc is not None:
            raise self.exc
        return self.ts


def _flow_provider(flows):
    """Returns a flow_provider serving the given per-platform flow map."""

    def provider(pid):
        return flows.get(pid)

    return provider


SONG = {"title": "Dance", "artists": ["D1"]}


class TestScrobbleCurrentSong:
    def test_happy_path_returns_song_and_scrobble_ts(self):
        flows = {"spotify": _FakeFlow(song_data=SONG)}
        integ = _FakeIntegration()
        scrobbler = _FakeScrobbler(ts=42)
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": integ}),
            _FakeRegistry(ids=("spotify",)),
            _flow_provider(flows),
            scrobbler,
        )
        assert result == (SONG, 42)
        assert flows["spotify"].timeouts == [30]
        assert scrobbler.calls == [SONG]

    def test_custom_timeout_forwarded(self):
        flows = {"spotify": _FakeFlow(song_data=SONG)}
        scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration()}),
            _FakeRegistry(ids=("spotify",)),
            _flow_provider(flows),
            _FakeScrobbler(),
            timeout=12,
        )
        assert flows["spotify"].timeouts == [12]

    def test_unauthenticated_integration_skipped_by_default(self):
        # Default mode gates on is_authenticated() - the flow must never
        # even be built for an unauthenticated platform.
        flows = {"spotify": _FakeFlow(song_data=SONG)}
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration(authenticated=False)}),
            _FakeRegistry(ids=("spotify",)),
            _flow_provider(flows),
            _FakeScrobbler(),
        )
        assert result is None
        assert flows["spotify"].calls == 0

    def test_provider_replaces_lookup_and_auth_gate(self):
        # The CLI provider authenticates platforms itself right before
        # the capture - the unauthenticated entry in `integrations` must
        # not gate the provider-supplied integration.
        flows = {"spotify": _FakeFlow(song_data=SONG)}
        integ = _FakeIntegration()
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration(authenticated=False)}),
            _FakeRegistry(ids=("spotify",)),
            _flow_provider(flows),
            _FakeScrobbler(),
            integration_provider=lambda pid: integ,
        )
        assert result is not None
        assert integ.authenticated  # provider chose it, gate not consulted

    def test_provider_returning_none_skips_platform(self):
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration()}),
            _FakeRegistry(ids=("spotify",)),
            _flow_provider({"spotify": _FakeFlow(song_data=SONG)}),
            _FakeScrobbler(),
            integration_provider=lambda pid: None,
        )
        assert result is None

    def test_missing_plugin_skipped(self):
        registry = _FakeRegistry(ids=("spotify",))
        registry.plugins["spotify"] = None
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration()}),
            registry,
            _flow_provider({"spotify": _FakeFlow(song_data=SONG)}),
            _FakeScrobbler(),
        )
        assert result is None

    def test_plugin_without_flow_class_skipped(self):
        registry = _FakeRegistry(ids=("spotify",))
        registry.plugins["spotify"] = _FakePlugin(flow_class=None)
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration()}),
            registry,
            _flow_provider({"spotify": _FakeFlow(song_data=SONG)}),
            _FakeScrobbler(),
        )
        assert result is None

    def test_flow_provider_returning_none_skipped(self):
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration()}),
            _FakeRegistry(ids=("spotify",)),
            lambda pid: None,
            _FakeScrobbler(),
        )
        assert result is None

    def test_first_platform_with_a_song_wins(self):
        silent = _FakeFlow(song_data=None)
        live = _FakeFlow(song_data=SONG)
        flows = {"youtube_music": silent, "spotify": live}
        scrobbler = _FakeScrobbler()
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations(
                {"youtube_music": _FakeIntegration(), "spotify": _FakeIntegration()}
            ),
            _FakeRegistry(ids=("youtube_music", "spotify")),
            _flow_provider(flows),
            scrobbler,
        )
        assert result == (SONG, 123456789)
        assert silent.calls == 1 and live.calls == 1
        assert scrobbler.calls == [SONG]

    def test_scrobble_failure_tries_next_platform(self):
        song_a = {"title": "A"}
        song_b = {"title": "B"}
        flows = {"youtube_music": _FakeFlow(song_data=song_a), "spotify": _FakeFlow(song_data=song_b)}
        scrobbled = []

        def _flaky(song):
            scrobbled.append(song)
            if song == song_a:
                raise RuntimeError("Last.fm refused")
            return 42

        scrobbler = _FakeScrobbler()
        scrobbler.scrobble = _flaky
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations(
                {"youtube_music": _FakeIntegration(), "spotify": _FakeIntegration()}
            ),
            _FakeRegistry(ids=("youtube_music", "spotify")),
            _flow_provider(flows),
            scrobbler,
        )
        assert result == (song_b, 42)
        assert scrobbled == [song_a, song_b]

    def test_all_platforms_failing_scrobble_returns_none(self):
        # A scrobble rejection must not fabricate a success: when every
        # captured song is refused, the orchestration reports None.
        song_a = {"title": "A"}
        song_b = {"title": "B"}
        flows = {"youtube_music": _FakeFlow(song_data=song_a), "spotify": _FakeFlow(song_data=song_b)}
        scrobbler = _FakeScrobbler(exc=RuntimeError("Last.fm refused"))
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations(
                {"youtube_music": _FakeIntegration(), "spotify": _FakeIntegration()}
            ),
            _FakeRegistry(ids=("youtube_music", "spotify")),
            _flow_provider(flows),
            scrobbler,
        )
        assert result is None
        assert len(scrobbler.calls) == 2

    def test_every_backend_gets_the_song(self):
        # Two services switched on, one captured song, both scrobbled - the
        # whole point of the per-platform switch.
        flows = {"spotify": _FakeFlow(song_data=SONG)}
        lastfm, listenbrainz = _FakeScrobbler(ts=111), _FakeScrobbler(ts=222)
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration()}),
            _FakeRegistry(ids=("spotify",)),
            _flow_provider(flows),
            [lastfm, listenbrainz],
        )
        assert result == (SONG, 111)  # first accepted timestamp
        assert lastfm.calls == [SONG]
        assert listenbrainz.calls == [SONG]

    def test_one_backend_failing_keeps_the_other_scrobble(self):
        flows = {"spotify": _FakeFlow(song_data=SONG)}
        dead = _FakeScrobbler(exc=RuntimeError("token revoked"))
        live = _FakeScrobbler(ts=222)
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration()}),
            _FakeRegistry(ids=("spotify",)),
            _flow_provider(flows),
            [dead, live],
        )
        assert result == (SONG, 222)
        assert live.calls == [SONG]

    def test_every_backend_failing_tries_the_next_platform(self):
        # Nothing accepted, so the pre-existing retry kicks in: the next
        # source platform gets its own capture + its own attempt at each
        # service.  Two platforms x two backends = four calls.
        flows = {
            "youtube_music": _FakeFlow(song_data={"title": "A"}),
            "spotify": _FakeFlow(song_data={"title": "B"}),
        }
        dead = _FakeScrobbler(exc=RuntimeError("service down"))
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations(
                {"youtube_music": _FakeIntegration(), "spotify": _FakeIntegration()}
            ),
            _FakeRegistry(ids=("youtube_music", "spotify")),
            _flow_provider(flows),
            [dead, dead],
        )
        assert result is None
        assert len(dead.calls) == 4

    def test_capture_failure_tries_next_platform(self):
        broken = _FakeFlow(song_data=SONG, exc=RuntimeError("receiver down"))
        live = _FakeFlow(song_data=SONG)
        flows = {"youtube_music": broken, "spotify": live}
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations(
                {"youtube_music": _FakeIntegration(), "spotify": _FakeIntegration()}
            ),
            _FakeRegistry(ids=("youtube_music", "spotify")),
            _flow_provider(flows),
            _FakeScrobbler(),
        )
        assert result == (SONG, 123456789)
        assert broken.calls == 1 and live.calls == 1

    def test_no_platform_produces_a_song_returns_none(self):
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations({"spotify": _FakeIntegration()}),
            _FakeRegistry(ids=("spotify",)),
            _flow_provider({"spotify": _FakeFlow(song_data=None)}),
            _FakeScrobbler(),
        )
        assert result is None

    def test_empty_registry_returns_none(self):
        assert (
            scrobble_service.scrobble_current_song(
                _FakeIntegrations({}), _FakeRegistry(ids=()), lambda pid: None,
                _FakeScrobbler(),
            )
            is None
        )

    def test_registry_order_is_respected(self):
        # get_platform_ids defines the attempt order: youtube first.
        song_yt = {"title": "YT"}
        flows = {"youtube_music": _FakeFlow(song_data=song_yt), "spotify": _FakeFlow(song_data=SONG)}
        result = scrobble_service.scrobble_current_song(
            _FakeIntegrations(
                {"youtube_music": _FakeIntegration(), "spotify": _FakeIntegration()}
            ),
            _FakeRegistry(ids=("youtube_music", "spotify")),
            _flow_provider(flows),
            _FakeScrobbler(),
        )
        assert result[0] == song_yt