"""Tests for :mod:`services.playlist_sync` - the track import/reload
service and its policy invariants.

The module is policy-heavy: fetch-before-delete ordering (a failed
refresh must never lose the cached DB), delete-before-reinsert, the
mid-flight-uninstall guard (a stale worker must not resurrect a DB the
uninstall cleanup deleted), follower-count / thumbnail persistence, the
YouTube library-listing TTL cache, and the flow-gate serialization of
the threaded reload path.

All platform, store, DB, thumbnail-download and threading surfaces are
stubbed (duck-typed fakes + recorders); ``ThumbnailService.from_data``
is exercised for real (pure dict extraction).  No network, no display,
no real threads.
"""

from types import SimpleNamespace

import pytest

from services import playlist_sync


# ---------------------------------------------------------------------------
# Shared fakes / recorders
# ---------------------------------------------------------------------------


class _Events:
    """Append-only ordered event list shared by the recorders below."""

    def __init__(self):
        self.events = []

    def append(self, name):
        self.events.append(name)

    def tag(self, name):
        def _recorder(*args, **kwargs):
            self.events.append(name)

        return _recorder


class _Stub:
    """Callable returning a fixed result and recording calls."""

    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _Raise:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, *args, **kwargs):
        raise self.exc


class _SyncThread:
    """Runs the target inline - keeps the threaded API deterministic."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class _FakeGate:
    """Duck-typed flow gate: records acquire/release, no real blocking."""

    def __init__(self, acquired=True):
        self.acquired = acquired
        self.acquire_calls = 0
        self.timeout_seen = None
        self.released = 0

    def acquire(self, timeout=None):
        self.acquire_calls += 1
        self.timeout_seen = timeout
        return self.acquired

    def release(self):
        self.released += 1


class _FakeIntegration:
    """Duck-typed platform integration: details / tracks / library."""

    def __init__(
        self,
        details=None,
        tracks=None,
        library=(),
        details_exc=None,
        events=None,
    ):
        self.details = details if details is not None else {"thumbnail": "tb"}
        self.tracks = list(tracks) if tracks else []
        self.library = list(library)
        self.details_exc = details_exc
        self.events = events
        self.library_calls = 0

    def get_playlist_details(self, playlist_id):
        if self.events is not None:
            self.events.append("details")
        if self.details_exc is not None:
            raise self.details_exc
        return self.details

    def get_playlist_tracks(self, playlist_id):
        if self.events is not None:
            self.events.append("tracks")
        return list(self.tracks)

    def get_library_playlists(self):
        self.library_calls += 1
        return list(self.library)


class _SequencedIntegrations:
    """Returns the integration for the first N gets, then None - simulates
    a platform uninstall landing while a fetch was in flight."""

    def __init__(self, integration, first_n=1):
        self.integration = integration
        self.first_n = first_n
        self.calls = 0

    def get(self, platform):
        self.calls += 1
        return self.integration if self.calls <= self.first_n else None


class _SongManagerShim:
    """Stand-in for SongManager: records add_songs_bulk arguments."""

    instances = []

    def __init__(self, events=None):
        _SongManagerShim.instances.append(self)
        self.events = events
        self.add_calls = []

    def add_songs_bulk(
        self, playlist_name, tracks, platform="youtube_music", playlist_id=""
    ):
        self.add_calls.append((playlist_name, list(tracks), platform, playlist_id))
        if self.events is not None:
            self.events.append("add")
        return len(tracks)


@pytest.fixture(autouse=True)
def _clear_library_cache():
    playlist_sync._library_listing_cache.clear()
    yield
    playlist_sync._library_listing_cache.clear()


@pytest.fixture
def events():
    return _Events()


@pytest.fixture
def clock(monkeypatch):
    """Mutable monotonic clock - drives the TTL cache expiry logic."""
    state = {"now": 1000.0}
    monkeypatch.setattr(playlist_sync.time, "monotonic", lambda: state["now"])
    return state


@pytest.fixture
def stub_song_manager(monkeypatch, events):
    _SongManagerShim.instances = []
    monkeypatch.setattr(
        playlist_sync, "SongManager", lambda: _SongManagerShim(events)
    )
    return _SongManagerShim


@pytest.fixture
def stub_side_effects(monkeypatch, events):
    monkeypatch.setattr(
        playlist_sync.DatabaseManager, "delete_playlist_db", events.tag("db_delete")
    )
    monkeypatch.setattr(
        playlist_sync.PlaylistStore, "update_thumbnail", events.tag("thumb")
    )
    monkeypatch.setattr(
        playlist_sync.PlaylistStore, "update_metadata", events.tag("metadata")
    )
    return events


@pytest.fixture
def inline_threads(monkeypatch):
    monkeypatch.setattr(playlist_sync.threading, "Thread", _SyncThread)


@pytest.fixture
def stub_warm_cache(monkeypatch):
    warm = _Stub()
    monkeypatch.setattr(playlist_sync.ThumbnailService, "warm_song_cache", warm)
    return warm


# ---------------------------------------------------------------------------
# import_tracks_sync
# ---------------------------------------------------------------------------


class TestImportTracksSync:
    def test_missing_playlist_id_raises(self, stub_song_manager):
        service = playlist_sync.PlaylistSyncService(SimpleNamespace())
        with pytest.raises(RuntimeError):
            service.import_tracks_sync("Dance", "spotify", "")

    def test_missing_integration_raises(self, stub_song_manager):
        service = playlist_sync.PlaylistSyncService(SimpleNamespace(get=lambda p: None))
        with pytest.raises(RuntimeError):
            service.import_tracks_sync("Dance", "spotify", "SP1")

    def test_no_tracks_short_circuits(self, stub_song_manager):
        integration = _FakeIntegration(tracks=[])
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: integration)
        )
        assert service.import_tracks_sync("Dance", "spotify", "SP1") == (0, "no_tracks")
        assert _SongManagerShim.instances == []

    def test_happy_path_persists_tracks(self, stub_song_manager):
        tracks = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
        integration = _FakeIntegration(tracks=tracks)
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: integration)
        )
        assert service.import_tracks_sync("Dance", "spotify", "SP1") == (3, "ok")
        shim = _SongManagerShim.instances[-1]
        assert shim.add_calls == [("Dance", tracks, "spotify", "SP1")]

    def test_uninstalled_mid_flight_does_not_resurrect_db(self, stub_song_manager):
        # The integration disappeared while the fetch was in flight - an
        # uninstall deleted db/<platform>/; a stale worker must not
        # silently recreate it.
        integration = _FakeIntegration(tracks=[{"title": "a"}])
        integrations = _SequencedIntegrations(integration, first_n=1)
        service = playlist_sync.PlaylistSyncService(integrations)
        with pytest.raises(RuntimeError):
            service.import_tracks_sync("Dance", "spotify", "SP1")
        assert _SongManagerShim.instances == []


# ---------------------------------------------------------------------------
# reload_database_sync
# ---------------------------------------------------------------------------


class TestReloadDatabaseSync:
    def test_missing_playlist_id_raises(self, stub_song_manager, stub_side_effects):
        service = playlist_sync.PlaylistSyncService(SimpleNamespace())
        with pytest.raises(RuntimeError):
            service.reload_database_sync("Dance", "spotify", "")

    def test_missing_integration_raises(self, stub_song_manager, stub_side_effects):
        service = playlist_sync.PlaylistSyncService(SimpleNamespace(get=lambda p: None))
        with pytest.raises(RuntimeError):
            service.reload_database_sync("Dance", "spotify", "SP1")

    def test_unreachable_details_aborts_before_any_destruction(
        self, stub_song_manager, stub_side_effects, events
    ):
        # A failed details fetch must NOT delete the local DB - the cached
        # tracks survive a deleted/private playlist or network error.
        integration = _FakeIntegration(details={})
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: integration)
        )
        with pytest.raises(RuntimeError) as e:
            service.reload_database_sync("Dance", "spotify", "SP1")
        assert "could not fetch details" in str(e.value)
        assert "db_delete" not in events.events
        assert "tracks" not in events.events

    def test_details_fetch_exception_propagates(
        self, stub_song_manager, stub_side_effects, events
    ):
        integration = _FakeIntegration(details_exc=ValueError("boom"))
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: integration)
        )
        with pytest.raises(ValueError):
            service.reload_database_sync("Dance", "spotify", "SP1")
        assert "db_delete" not in events.events

    def test_happy_path_order_is_fetch_then_destroy_then_persist(
        self, stub_song_manager, stub_side_effects, events
    ):
        # The policy order: details BEFORE delete (playlist reachable?),
        # tracks BEFORE delete (slowest step serves reads from the old DB),
        # delete BEFORE reinsert.
        integration = _FakeIntegration(
            details={"thumbnail": "tb", "followerCount": 42},
            tracks=[{"title": "a"}, {"title": "b"}],
            events=events,
        )
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: integration)
        )
        result = service.reload_database_sync("Dance", "spotify", "SP1")
        assert result == (2, "ok", "tb")
        assert events.events == [
            "details", "metadata", "tracks", "db_delete", "thumb", "add"
        ]

    def test_follower_count_only_persisted_when_present(
        self, stub_song_manager, stub_side_effects, events
    ):
        integration = _FakeIntegration(
            details={"thumbnail": "tb"}, tracks=[{"title": "a"}], events=events
        )
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: integration)
        )
        service.reload_database_sync("Dance", "spotify", "SP1")
        assert "metadata" not in events.events

    def test_thumbnail_only_persisted_when_present(
        self, stub_song_manager, stub_side_effects, events
    ):
        # A reachable playlist with no thumbnail: cover persistence must
        # be skipped ({} would be "unreachable details" - the abort path,
        # tested elsewhere).
        integration = _FakeIntegration(
            details={"title": "Dance"}, tracks=[{"title": "a"}], events=events
        )
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: integration)
        )
        result = service.reload_database_sync("Dance", "spotify", "SP1")
        assert result[0] == 1
        assert result[2] is None
        assert "thumb" not in events.events

    def test_empty_tracks_still_refresh_cover(
        self, stub_song_manager, stub_side_effects, events
    ):
        # An empty playlist must still get its cover refreshed, and the
        # DB must be rebuilt (deleted) even with nothing to insert.
        integration = _FakeIntegration(details={"thumbnail": "tb"}, tracks=[], events=events)
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: integration)
        )
        assert service.reload_database_sync("Dance", "spotify", "SP1") == (
            0, "no_tracks", "tb"
        )
        assert events.events == ["details", "tracks", "db_delete", "thumb"]

    def test_uninstalled_mid_flight_aborts_before_delete(
        self, stub_song_manager, stub_side_effects, events
    ):
        integration = _FakeIntegration(
            details={"thumbnail": "tb"}, tracks=[{"title": "a"}], events=events
        )
        integrations = _SequencedIntegrations(integration, first_n=1)
        service = playlist_sync.PlaylistSyncService(integrations)
        with pytest.raises(RuntimeError):
            service.reload_database_sync("Dance", "spotify", "SP1")
        assert events.events == ["details", "tracks"]
        assert "db_delete" not in events.events


# ---------------------------------------------------------------------------
# prefer_library_thumbnail (YouTube library-listing TTL cache)
# ---------------------------------------------------------------------------


class TestPreferLibraryThumbnail:
    def test_non_youtube_passthrough(self, clock):
        integration = _FakeIntegration(
            library=[{"playlistId": "SP1", "thumbnail": "lib"}]
        )
        result = playlist_sync.PlaylistSyncService.prefer_library_thumbnail(
            "spotify", integration, "SP1", "dt"
        )
        assert result == "dt"
        assert integration.library_calls == 0

    def test_cached_hit_within_ttl(self, clock):
        integration = _FakeIntegration(
            library=[{"playlistId": "SP1", "thumbnail": "lib"}]
        )
        first = playlist_sync.PlaylistSyncService.prefer_library_thumbnail(
            "youtube_music", integration, "SP1", "dt"
        )
        clock["now"] += 100  # inside the 300 s TTL
        second = playlist_sync.PlaylistSyncService.prefer_library_thumbnail(
            "youtube_music", integration, "SP1", "dt"
        )
        assert first == "lib" and second == "lib"
        assert integration.library_calls == 1

    def test_ttl_expiry_refetches(self, clock):
        integration = _FakeIntegration(
            library=[{"playlistId": "SP1", "thumbnail": "lib"}]
        )
        playlist_sync.PlaylistSyncService.prefer_library_thumbnail(
            "youtube_music", integration, "SP1", "dt"
        )
        clock["now"] += playlist_sync._LIBRARY_LISTING_TTL + 1
        result = playlist_sync.PlaylistSyncService.prefer_library_thumbnail(
            "youtube_music", integration, "SP1", "dt"
        )
        assert result == "lib"
        assert integration.library_calls == 2

    def test_playlist_not_in_listing_uses_details_thumb(self, clock):
        integration = _FakeIntegration(
            library=[{"playlistId": "OTHER", "thumbnail": "lib"}]
        )
        result = playlist_sync.PlaylistSyncService.prefer_library_thumbnail(
            "youtube_music", integration, "SP1", "dt"
        )
        assert result == "dt"

    def test_matched_listing_entry_without_thumb_uses_details(self, clock):
        integration = _FakeIntegration(library=[{"playlistId": "SP1"}])
        result = playlist_sync.PlaylistSyncService.prefer_library_thumbnail(
            "youtube_music", integration, "SP1", "dt"
        )
        assert result == "dt"

    def test_library_fetch_failure_falls_back(self, clock):
        def _boom():
            raise RuntimeError("listing unavailable")

        integration = _FakeIntegration()
        integration.get_library_playlists = _boom
        result = playlist_sync.PlaylistSyncService.prefer_library_thumbnail(
            "youtube_music", integration, "SP1", "dt"
        )
        assert result == "dt"


# ---------------------------------------------------------------------------
# Threaded public API (inline thread, stubbed sync core)
# ---------------------------------------------------------------------------


class TestImportTracksThreaded:
    def test_missing_playlist_id_calls_done_synchronously(
        self, inline_threads, stub_warm_cache
    ):
        done = _Stub()
        service = playlist_sync.PlaylistSyncService(SimpleNamespace())
        service.import_tracks("Dance", "spotify", "", done)
        assert done.calls == [(("Dance", 0, "no_tracks"), {})]
        assert stub_warm_cache.calls == []

    def test_missing_integration_calls_done_synchronously(
        self, inline_threads, stub_warm_cache
    ):
        done = _Stub()
        service = playlist_sync.PlaylistSyncService(SimpleNamespace(get=lambda p: None))
        service.import_tracks("Dance", "spotify", "SP1", done)
        assert done.calls == [(("Dance", 0, "error"), {})]

    def test_success_warms_cache_and_reports(self, inline_threads, stub_warm_cache):
        done = _Stub()
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: object())
        )
        service.import_tracks_sync = _Stub((5, "ok"))
        service.import_tracks("Dance", "spotify", "SP1", done)
        assert done.calls == [(("Dance", 5, "ok"), {})]
        assert stub_warm_cache.calls == [(("Dance", "spotify", "SP1"), {})]

    def test_sync_failure_reports_error(self, inline_threads, stub_warm_cache):
        done = _Stub()
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: object())
        )
        service.import_tracks_sync = _Raise(RuntimeError("boom"))
        service.import_tracks("Dance", "spotify", "SP1", done)
        assert done.calls == [(("Dance", 0, "error"), {})]

    def test_no_playlist_id_never_touches_integrations(self, stub_warm_cache):
        done = _Stub()

        def _unexpected(p):
            raise AssertionError("integrations lookup must not happen")

        service = playlist_sync.PlaylistSyncService(SimpleNamespace(get=_unexpected))
        service.import_tracks("Dance", "spotify", "", done)
        assert done.calls == [(("Dance", 0, "no_tracks"), {})]


class TestReloadDatabaseThreaded:
    def test_missing_playlist_id_calls_done_synchronously(
        self, inline_threads, stub_warm_cache
    ):
        done = _Stub()
        service = playlist_sync.PlaylistSyncService(SimpleNamespace())
        service.reload_database("Dance", "spotify", "", done)
        assert done.calls == [(("Dance", 0, "no_tracks", None), {})]

    def test_missing_integration_calls_done_synchronously(
        self, inline_threads, stub_warm_cache
    ):
        done = _Stub()
        service = playlist_sync.PlaylistSyncService(SimpleNamespace(get=lambda p: None))
        service.reload_database("Dance", "spotify", "SP1", done)
        assert done.calls == [(("Dance", 0, "error", None), {})]

    def test_gate_acquire_timeout_aborts_and_does_not_release(
        self, inline_threads, stub_warm_cache
    ):
        done = _Stub()
        gate = _FakeGate(acquired=False)
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: object())
        )
        service.reload_database("Dance", "spotify", "SP1", done, gate=gate)
        assert done.calls == [(("Dance", 0, "error", None), {})]
        assert gate.acquire_calls == 1
        assert gate.timeout_seen == playlist_sync.GATE_ACQUIRE_TIMEOUT
        assert gate.released == 0
        assert stub_warm_cache.calls == []

    def test_gate_serializes_and_releases_after_done(
        self, inline_threads, stub_warm_cache
    ):
        done = _Stub()
        gate = _FakeGate(acquired=True)
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: object())
        )
        service.reload_database_sync = _Stub((3, "ok", "tb"))
        service.reload_database("Dance", "spotify", "SP1", done, gate=gate)
        assert done.calls == [(("Dance", 3, "ok", "tb"), {})]
        assert gate.acquire_calls == 1 and gate.released == 1
        assert stub_warm_cache.calls == [(("Dance", "spotify", "SP1"), {})]

    def test_sync_failure_still_releases_gate(
        self, inline_threads, stub_warm_cache
    ):
        done = _Stub()
        gate = _FakeGate(acquired=True)
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: object())
        )
        service.reload_database_sync = _Raise(RuntimeError("boom"))
        service.reload_database("Dance", "spotify", "SP1", done, gate=gate)
        assert done.calls == [(("Dance", 0, "error", None), {})]
        assert gate.released == 1

    def test_no_gate_runs_without_acquire_release(
        self, inline_threads, stub_warm_cache
    ):
        done = _Stub()
        service = playlist_sync.PlaylistSyncService(
            SimpleNamespace(get=lambda p: object())
        )
        service.reload_database_sync = _Stub((1, "ok", "tb"))
        service.reload_database("Dance", "spotify", "SP1", done)
        assert done.calls == [(("Dance", 1, "ok", "tb"), {})]