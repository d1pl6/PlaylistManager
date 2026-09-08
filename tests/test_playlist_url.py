"""Unit tests for services/playlist_url.py - URL parsing and building.

Uses a fake plugin_registry (real manifests need no imports; a stub with
url_hosts / templates suffices) so no real integrations dir is touched.
"""

import pytest

from services import playlist_url


class FakePlugin:
    def __init__(self, pid, hosts, playlist_template="", song_template=""):
        self.id = pid
        self.url_hosts = hosts
        self.playlist_url_template = playlist_template
        self.song_url_template = song_template


class FakeRegistry:
    def __init__(self, plugins):
        self._plugins = {p.id: p for p in plugins}

    def get_all(self):
        return dict(self._plugins)

    def get(self, pid):
        return self._plugins.get(pid)


@pytest.fixture
def registry():
    return FakeRegistry([
        FakePlugin("youtube_music", ["music.youtube.com", "www.youtube.com"],
                   playlist_template="https://{host}/playlist?list={id}",
                   song_template="https://{host}/watch?v={id}"),
        FakePlugin("spotify", ["open.spotify.com"],
                   playlist_template="https://open.spotify.com/playlist/{id}",
                   song_template="https://open.spotify.com/track/{id}"),
        FakePlugin("soundcloud", ["soundcloud.com", "on.soundcloud.com"]),
    ])


class TestParseQueryForm:
    @pytest.mark.parametrize("url", [
        "https://music.youtube.com/playlist?list=PLabc123",
        "https://www.youtube.com/playlist?list=PLabc123",
        "https://music.youtube.com/playlist?list=PLabc123&si=xyz",
    ])
    def test_youtube_family(self, registry, url):
        assert playlist_url.parse_playlist_url(url, registry) == ("youtube_music", "PLabc123")

    def test_query_id_keeps_case(self, registry):
        assert playlist_url.parse_playlist_url(
            "https://music.youtube.com/playlist?list=plABc123", registry
        )[1] == "plABc123"

    def test_missing_list_param_raises(self, registry):
        with pytest.raises(ValueError, match="list"):
            playlist_url.parse_playlist_url(
                "https://music.youtube.com/playlist?foo=bar", registry
            )

    def test_case_insensitive_path_and_scheme(self, registry):
        # urlparse normalizes scheme + host; the code lowercases the path
        # for the token match but keeps the query param name case-sensitive.
        assert playlist_url.parse_playlist_url(
            "HTTPS://MUSIC.YOUTUBE.COM/Playlist?list=PLabc123", registry
        ) == ("youtube_music", "PLabc123")

    def test_uppercase_query_param_name_not_matched(self, registry):
        # parse_qs keeps key case; the code reads lowercase "list" only.
        with pytest.raises(ValueError, match="list"):
            playlist_url.parse_playlist_url(
                "https://music.youtube.com/playlist?List=PLabc123", registry
            )


class TestParsePathForm:
    @pytest.mark.parametrize("url", [
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://open.spotify.com/intl-en-US/playlist/37i9dQZF1DXcBWIGoYBM5M",
    ])
    def test_spotify_playlist_path(self, registry, url):
        assert playlist_url.parse_playlist_url(url, registry) == (
            "spotify", "37i9dQZF1DXcBWIGoYBM5M"
        )

    def test_path_id_keeps_case(self, registry):
        assert playlist_url.parse_playlist_url(
            "https://open.spotify.com/playlist/AbCdEf123", registry
        )[1] == "AbCdEf123"

    def test_bare_playlist_path_hits_query_form(self, registry):
        # /playlist (no trailing segment) reaches the query-param branch
        # first, since path.lower() == "/playlist".
        with pytest.raises(ValueError, match="list"):
            playlist_url.parse_playlist_url("https://open.spotify.com/playlist", registry)

    def test_playlist_token_last_segment_missing_id(self, registry):
        # A "playlist" token that is NOT at the start reaches the path
        # form, where a missing trailing segment raises "No playlist ID".
        with pytest.raises(ValueError, match="No playlist ID"):
            playlist_url.parse_playlist_url(
                "https://open.spotify.com/abc/playlist", registry
            )

    def test_playlist_token_not_found_raises(self, registry):
        with pytest.raises(ValueError, match="Unrecognized URL"):
            playlist_url.parse_playlist_url(
                "https://open.spotify.com/album/abcdef", registry
            )


class TestParseURIToForm:
    def test_spotify_uri(self, registry):
        assert playlist_url.parse_playlist_url(
            "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", registry
        ) == ("spotify", "37i9dQZF1DXcBWIGoYBM5M")

    def test_spotify_uri_with_query_fragment_stripped(self, registry):
        assert playlist_url.parse_playlist_url(
            "spotify:playlist:37i9?si=abc#frag", registry
        ) == ("spotify", "37i9")

    def test_uri_case_insensitive_scheme_and_token(self, registry):
        assert playlist_url.parse_playlist_url(
            "SPOTIFY:Playlist:37i9dQZF1DXcBWIGoYBM5M", registry
        ) == ("spotify", "37i9dQZF1DXcBWIGoYBM5M")

    def test_uri_wrong_type_raises(self, registry):
        with pytest.raises(ValueError):
            playlist_url.parse_playlist_url("spotify:track:abc123", registry)


class TestParseSoundCloud:
    @pytest.mark.parametrize("url", [
        "https://soundcloud.com/user/sets/slug",
        "https://soundcloud.com/artist/slug",
        "https://on.soundcloud.com/token123",
    ])
    def test_path_form_returns_full_path(self, registry, url):
        platform, pid = playlist_url.parse_playlist_url(url, registry)
        assert platform == "soundcloud"
        assert pid  # the full path is stored as the id

    def test_user_named_playlist_not_misparsed(self, registry):
        # A SoundCloud user legitimately called "playlist" must not be
        # treated as the /playlist/ token.
        assert playlist_url.parse_playlist_url(
            "https://soundcloud.com/playlist/something", registry
        )[1] == "playlist/something"


class TestRejections:
    def test_empty_url(self, registry):
        with pytest.raises(ValueError, match="Empty"):
            playlist_url.parse_playlist_url("   ", registry)

    def test_youtu_be_is_song_hint(self, registry):
        with pytest.raises(ValueError, match="song URL"):
            playlist_url.parse_playlist_url("https://youtu.be/abc123", registry)

    def test_watch_url_is_song_hint(self, registry):
        with pytest.raises(ValueError, match="song URL"):
            playlist_url.parse_playlist_url(
                "https://music.youtube.com/watch?v=abc123", registry
            )

    def test_unrecognized_host(self, registry):
        with pytest.raises(ValueError):
            playlist_url.parse_playlist_url("https://example.com/playlist/abc", registry)

    def test_non_http_scheme(self, registry):
        with pytest.raises(ValueError):
            playlist_url.parse_playlist_url("ftp://open.spotify.com/playlist/abc", registry)


class TestBuildPlaylistUrl:
    def test_uses_template(self, registry):
        assert playlist_url.build_playlist_url(
            "spotify", "37i9dQZF1DXcBWIGoYBM5M", registry
        ) == "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"

    def test_template_placeholder_substitution(self, registry):
        assert playlist_url.build_playlist_url(
            "youtube_music", "PLabc", registry
        ) == "https://music.youtube.com/playlist?list=PLabc"

    def test_soundcloud_path_id_falls_through_to_template(self, registry):
        assert playlist_url.build_playlist_url(
            "soundcloud", "user/sets/slug", registry
        ) == "https://soundcloud.com/user/sets/slug"

    def test_soundcloud_urn_numeric_playlist(self, registry):
        assert playlist_url.build_playlist_url(
            "soundcloud", "soundcloud:playlists:123", registry
        ) == "https://soundcloud.com/sets/123"

    def test_soundcloud_unknown_format(self, registry):
        # A numeric-looking track URN or bare id has no browseable page.
        assert playlist_url.build_playlist_url(
            "soundcloud", "soundcloud:tracks:123", registry
        ) is None

    def test_empty_id_returns_none(self, registry):
        assert playlist_url.build_playlist_url("spotify", "", registry) is None

    def test_unknown_platform_returns_none(self, registry):
        assert playlist_url.build_playlist_url("nonexistent", "abc", registry) is None


class TestBuildSongUrl:
    def test_uses_template(self, registry):
        assert playlist_url.build_song_url(
            "spotify", "12345abc", registry
        ) == "https://open.spotify.com/track/12345abc"

    def test_empty_id_returns_none(self, registry):
        assert playlist_url.build_song_url("spotify", "", registry) is None

    def test_unknown_platform_returns_none(self, registry):
        assert playlist_url.build_song_url("nonexistent", "abc", registry) is None

    def test_soundcloud_track_urn_no_numeric_page(self, registry):
        assert playlist_url.build_song_url(
            "soundcloud", "soundcloud:tracks:123", registry
        ) is None

    def test_soundcloud_path_id_builds(self, registry):
        assert playlist_url.build_song_url(
            "soundcloud", "user/track-slug", registry
        ) == "https://soundcloud.com/user/track-slug"


class TestHelperInternals:
    def test_soundcloud_urn_playlist_path(self):
        assert playlist_url._soundcloud_url("soundcloud:playlists:42", "playlist") == (
            "https://soundcloud.com/sets/42"
        )

    def test_soundcloud_urn_track_returns_none(self):
        assert playlist_url._soundcloud_url("soundcloud:tracks:42", "song") is None

    def test_resolve_host_returns_first_declared(self, registry):
        assert playlist_url._resolve_host("youtube_music", registry) == "music.youtube.com"

    def test_resolve_host_unknown(self, registry):
        assert playlist_url._resolve_host("ghost", registry) is None