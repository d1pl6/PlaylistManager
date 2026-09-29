"""YouTube Music flow timeout/retry/cache tests (headless).

Covers the failure handling added for the keybind add-flow: ytmusicapi
surfaces a slow network as ``requests.exceptions.ReadTimeout`` (a sibling
of the builtin ``TimeoutError``, NOT a subclass - so ``except
TimeoutError`` never sees it) and ``execute_flow`` must:

* surface a friendly timeout message instead of a raw urllib3 sentence,
* retry the read-only ``get_song`` fetch once on a stalled read,
* report a timed-out library scan as a timeout rather than a misleading
  "playlist not found" (and cache nothing on failure),
* cache resolved artists per video id so a repeated song skips the second
  ``get_song_related`` round trip,
* rewrite the server's "not authorized to edit this playlist" 401 into a
  message pointing at playlist ownership instead of echoing the raw error.

No network is touched - the client is a stub.  The whole module skips
when the youtube_music plugin repo is absent (gitignored plugin dirs are
missing on fresh clones).
"""

from types import SimpleNamespace

import pytest
import requests

URL = "https://music.youtube.com/watch?v=abcdefghijk"


def _plugin_mod():
    try:
        import integrations.youtube_music.flow as flow_mod
    except ImportError:
        pytest.skip("youtube_music plugin not installed in this checkout")
    return flow_mod


class _NoopReceiver:
    def is_running(self) -> bool:
        return False

    def stop(self) -> None:
        pass


class _Recorder:
    """Captures the flow's status/error/success callbacks."""

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


def _make_flow(client):
    flow_mod = _plugin_mod()
    return flow_mod.YouTubeMusicFlow(
        SimpleNamespace(yt_client=client),
        song_manager=object(),
        url_receiver=_NoopReceiver(),
    )


class _ReadTimeoutClient:
    """get_song always stalls; counts calls."""

    def __init__(self):
        self.calls = 0

    def get_song(self, video_id):
        self.calls += 1
        raise requests.exceptions.ReadTimeout(
            "HTTPSConnectionPool(host='music.youtube.com', port=443): "
            "Read timed out. (read timeout=30)"
        )


class _ConnectTimeoutClient:
    """get_song can never connect; counts calls."""

    def __init__(self):
        self.calls = 0

    def get_song(self, video_id):
        self.calls += 1
        raise requests.exceptions.ConnectTimeout("Connection timed out.")


class TestFetchTimeout:
    def test_read_timeout_maps_to_friendly_error_and_retries_once(self):
        client = _ReadTimeoutClient()
        flow = _make_flow(client)
        rec = _Recorder()

        flow.execute_flow(
            "Some Playlist", rec.on_status, rec.on_error, rec.on_success,
            url=URL,
        )

        assert rec.successes == []
        assert len(rec.errors) == 1
        # friendly wording, no urllib3 internals
        assert "Timed out contacting YouTube Music" in rec.errors[0]
        assert "HTTPSConnectionPool" not in rec.errors[0]
        # the read-only fetch was retried exactly once
        assert client.calls == 2

    def test_connect_timeout_maps_to_friendly_error_without_retry(self):
        client = _ConnectTimeoutClient()
        flow = _make_flow(client)
        rec = _Recorder()

        flow.execute_flow(
            "Some Playlist", rec.on_status, rec.on_error, rec.on_success,
            url=URL,
        )

        assert len(rec.errors) == 1
        assert "Timed out contacting YouTube Music" in rec.errors[0]
        # a reachable-host check would just time out again - no retry
        assert client.calls == 1


class _ScanTimeoutClient:
    def get_library_playlists(self, limit=None):
        raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=30)")


class TestPlaylistIdTimeout:
    def test_scan_timeout_raises_timeout_message_not_not_found(self):
        flow = _make_flow(_ScanTimeoutClient())

        with pytest.raises(RuntimeError) as excinfo:
            flow._get_playlist_id("Some Playlist")
        assert "Timed out scanning" in str(excinfo.value)
        assert "check the network" in str(excinfo.value)

        # Nothing was cached: the next resolution gets a fresh scan (and
        # fails again rather than returning a stale "not found").
        with pytest.raises(RuntimeError):
            flow._get_playlist_id("Some Playlist")


class TestArtistCache:
    def test_artists_cached_per_video_id_skips_second_related_call(self):
        class _Client:
            def __init__(self):
                self.related_calls = 0

            def get_song(self, video_id):
                return {
                    "videoDetails": {
                        "title": "Some Song",
                        "author": "Piano Music - Topic",
                        "lengthSeconds": 200,
                        "thumbnail": {"thumbnails": []},
                    },
                    "related": [],
                }

            def get_song_related(self, video_id):
                self.related_calls += 1
                raise RuntimeError("network down")

        client = _Client()
        flow = _make_flow(client)

        # Priority 1 (get_song_related) fails -> falls back to the
        # channel-suffix stripping of videoDetails.author.
        first = flow._resolve_artists(
            "abcdefghijk", {"videoDetails": {}}, {"author": "Piano Music - Topic"}
        )
        assert first == ["Piano Music"]

        # Same video again: cached, no second round trip even though the
        # fallback inputs differ.
        second = flow._resolve_artists(
            "abcdefghijk", {"videoDetails": {}}, {"author": "Something Else"}
        )
        assert second == ["Piano Music"]
        assert client.related_calls == 1


class _UnauthorizedAddClient:
    """add_playlist_items always rejects with the server's edit-denied 401."""

    def add_playlist_items(self, playlist_id, video_ids):
        from ytmusicapi.exceptions import YTMusicServerError

        raise YTMusicServerError(
            "Server returned HTTP 401: Unauthorized.\n"
            "You are not authorized to edit this playlist."
        )


class _MakingFlow:
    """Builds a flow that runs execute_flow all the way to the add step."""

    def __init__(self, client):
        flow_mod = _plugin_mod()
        self.flow = flow_mod.YouTubeMusicFlow(
            SimpleNamespace(yt_client=client),
            song_manager=SimpleNamespace(
                song_exists=lambda *a, **k: False,
                get_all_songs=lambda *a, **k: [],
            ),
            url_receiver=_NoopReceiver(),
        )

    def run(self, monkeypatch, **kwargs):
        flow_mod = _plugin_mod()
        monkeypatch.setattr(
            flow_mod.PlaylistStore,
            "playlist_still_registered",
            staticmethod(lambda name, platform, pid: True),
        )
        rec = _Recorder()
        self.flow.execute_flow(
            "Some Playlist",
            rec.on_status,
            rec.on_error,
            rec.on_success,
            url=URL,
            song_data={"title": "Some Song", "artists": []},
            playlist_id="PL123",
            skip_duplicate_check=True,
            **kwargs,
        )
        return rec


class TestAddNotAuthorized:
    def test_edit_denied_401_shows_friendly_ownership_message(self, monkeypatch):
        pytest.importorskip("ytmusicapi")
        rec = _MakingFlow(_UnauthorizedAddClient()).run(monkeypatch)

        assert rec.successes == []
        assert len(rec.errors) == 1
        # the raw "Server returned HTTP 401..." sentence is replaced by
        # guidance about the playlist not being editable from this account
        assert "not editable from your account" in rec.errors[0]
        assert "Server returned HTTP 401" not in rec.errors[0]
        assert "Error:" not in rec.errors[0]

    def test_unrelated_errors_keep_raw_text(self, monkeypatch):
        class _BoomClient:
            def add_playlist_items(self, playlist_id, video_ids):
                raise RuntimeError("boom")

        rec = _MakingFlow(_BoomClient()).run(monkeypatch)

        assert len(rec.errors) == 1
        assert rec.errors[0] == "Error: boom"