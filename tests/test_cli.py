"""Behavioural tests for the headless CLI: arg parsing + validation.

Covers three layers, none of which needs a display or the network:

1. ``app.main.parse_args`` - the argparse surface for ``-a`` / ``-p`` /
   ``--login`` (and every other flag): value enforcement, aliases,
   ``nargs="+"`` collection, usage-error exit codes.
2. ``app.main.main`` dispatch - which ``run_*`` gets called with what,
   the ``-p`` verb whitelist (add/del/delete/ref/refresh), the
   network-gate ordering (del is local-only), and flag precedence.
3. ``cli`` validation - ``_parse_token`` / ``resolve_targets`` /
   ``resolve_targets_for`` (target numbers/ranges/names/URLs, dedup,
   ambiguity, out-of-range), ``_resolve_platform_arg`` (--install /
   --uninstall), and ``run_login`` / ``run_del`` / ``run_refresh`` /
   ``run_add`` / ``run_add_url`` argument checks.

URL parsing and the store are stubbed (``cli.parse_playlist_url`` and an
in-memory PlaylistStore) so nothing depends on which plugin directories
exist on this machine; the real plugin registry is only ever asked for
platforms that cannot be installed under the test name.
"""

import io
import os
import sys
from types import SimpleNamespace

import pytest

import cli
import main as main_mod


@pytest.fixture(autouse=True)
def _no_credential_env(monkeypatch):
    """No ``PLAYLISTMANAGER_*`` credential leaks in from the outside.

    ``--login`` resolves each credential as flag > env > prompt (see
    CLI.MD "Authentication"), so an export in the developer's shell - or
    a CI job's secret store - would silently satisfy a test that expects
    the prompt, or the "credentials required" exit.  Same hermetic
    precedent as conftest.py's keyring fixtures.
    """
    for name in [k for k in os.environ if k.startswith("PLAYLISTMANAGER_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def registry(monkeypatch):
    """Stub plugin registry: the three CLI-login platforms + the two
    branches that must stay reachable (``lastfm`` is GUI-only,
    ``youtube_music`` opens a terminal).

    Without this the login tests depend on which plugin directories
    happen to be installed under ``integrations/`` on the machine -
    they are separate repos and are gitignored, so a fresh CI checkout
    has none.
    """
    plugins = {
        pid: SimpleNamespace(auth_path=None)
        for pid in ("spotify", "soundcloud", "deezer", "lastfm", "youtube_music")
    }
    reg = SimpleNamespace(
        get_platform_ids=lambda: list(plugins),
        get=lambda pid: plugins.get(pid),
    )
    monkeypatch.setattr(cli, "get_default_registry", lambda: reg)
    return reg


@pytest.fixture
def _clean_secret_registry():
    """Undo any register_secret() the test performed.

    The redaction registry is process-global; a leaked test secret would
    mask that same string in an unrelated later assertion.
    """
    from utils import logging_config

    before = set(logging_config._REGISTERED_SECRETS)
    yield
    logging_config._REGISTERED_SECRETS.difference_update(
        set(logging_config._REGISTERED_SECRETS) - before
    )


# ---------------------------------------------------------------------------
# parse_args: the argparse surface
# ---------------------------------------------------------------------------


def _parse(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["playlistmanager", *argv])
    return main_mod.parse_args()


class TestParseArgs:
    def test_no_args_defaults(self, monkeypatch):
        args = _parse(monkeypatch)
        assert args.add_song_targets is None
        assert args.scrobble_only is False
        assert args.playlist_args is None
        assert args.login_platform is None
        assert args.logout_platform is None
        assert args.install_platform is None
        assert args.uninstall_platform is None
        assert args.list_only is False
        assert args.client_id is None
        assert args.client_secret is None
        assert args.refresh_token is None
        assert args.data_saver is False
        assert args.fullscreen is False
        assert args.start_in_tray is False
        assert args.verbose == 0

    def test_add_song_short_and_long(self, monkeypatch):
        assert _parse(monkeypatch, "-a", "1,2,3").add_song_targets == "1,2,3"
        assert _parse(monkeypatch, "--add-song", "Chill Mix").add_song_targets == "Chill Mix"

    @pytest.mark.parametrize("arg", ["-a", "--add-song"])
    def test_add_song_requires_value(self, monkeypatch, arg):
        with pytest.raises(SystemExit) as e:
            _parse(monkeypatch, arg)
        assert e.value.code == 2

    def test_scrobble_flag(self, monkeypatch):
        assert _parse(monkeypatch, "-s").scrobble_only is True
        assert _parse(monkeypatch, "--scrobble").scrobble_only is True

    def test_playlist_action_collects_values(self, monkeypatch):
        args = _parse(
            monkeypatch, "-p", "add", "https://music.youtube.com/playlist?list=PL1"
        )
        assert args.playlist_args == [
            "add", "https://music.youtube.com/playlist?list=PL1"
        ]

    def test_playlist_action_collects_many_values(self, monkeypatch):
        args = _parse(monkeypatch, "-p", "del", "1", "Chill Mix")
        assert args.playlist_args == ["del", "1", "Chill Mix"]

    def test_playlist_stops_at_next_option(self, monkeypatch):
        args = _parse(monkeypatch, "-p", "del", "1", "-s")
        assert args.playlist_args == ["del", "1"]
        assert args.scrobble_only is True

    def test_playlist_requires_at_least_one_value(self, monkeypatch):
        with pytest.raises(SystemExit) as e:
            _parse(monkeypatch, "-p")
        assert e.value.code == 2

    def test_login_platform(self, monkeypatch):
        assert _parse(monkeypatch, "--login", "spotify").login_platform == "spotify"

    def test_login_requires_value(self, monkeypatch):
        with pytest.raises(SystemExit) as e:
            _parse(monkeypatch, "--login")
        assert e.value.code == 2

    def test_login_flag_does_not_swallow_next_option(self, monkeypatch):
        # "--login --list": the value must be a platform, not the next flag.
        with pytest.raises(SystemExit) as e:
            _parse(monkeypatch, "--login", "--list")
        assert e.value.code == 2

    def test_login_spotify_flags(self, monkeypatch):
        args = _parse(
            monkeypatch,
            "--login", "spotify",
            "--client-id", "CID",
            "--client-secret", "SEC",
            "--refresh-token", "TOK",
        )
        assert args.login_platform == "spotify"
        assert args.client_id == "CID"
        assert args.client_secret == "SEC"
        assert args.refresh_token == "TOK"

    @pytest.mark.parametrize(
        "argv, attr",
        [
            (["--logout", "spotify"], "logout_platform"),
            (["--install", "all"], "install_platform"),
            (["--uninstall", "youtube_music"], "uninstall_platform"),
        ],
    )
    def test_single_value_platform_args(self, monkeypatch, argv, attr):
        assert getattr(_parse(monkeypatch, *argv), attr) == argv[-1]

    @pytest.mark.parametrize("flag", ["--logout", "--install", "--uninstall"])
    def test_single_value_platform_args_require_value(self, monkeypatch, flag):
        with pytest.raises(SystemExit) as e:
            _parse(monkeypatch, flag)
        assert e.value.code == 2

    def test_list_flag(self, monkeypatch):
        assert _parse(monkeypatch, "--list").list_only is True
        assert _parse(monkeypatch, "-l").list_only is True

    def test_verbosity_flags(self, monkeypatch):
        assert _parse(monkeypatch, "-v").verbose == 1
        assert _parse(monkeypatch, "-vvv").verbose == 3
        assert _parse(monkeypatch, "--debug").debug is True
        assert _parse(monkeypatch, "--trace").trace is True

    def test_ui_override_flags(self, monkeypatch):
        args = _parse(monkeypatch, "--data-saver", "--fullscreen", "--start-in-tray")
        assert args.data_saver is True
        assert args.fullscreen is True
        assert args.start_in_tray is True

    def test_unknown_option_is_usage_error(self, monkeypatch):
        with pytest.raises(SystemExit) as e:
            _parse(monkeypatch, "--no-such-flag")
        assert e.value.code == 2


# ---------------------------------------------------------------------------
# main(): dispatch, -p verb whitelist, network gate, precedence
# ---------------------------------------------------------------------------


class _Recorder:
    """Callable stand-in for a run_* function: records calls, returns code."""

    def __init__(self, code=0):
        self.code = code
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.code


class _FakeApp:
    """Captures the args a GUI launch would receive."""

    last_args = None

    def __init__(self, args):
        _FakeApp.last_args = args

    def run(self):
        pass


class TestMainDispatch:
    @pytest.fixture(autouse=True)
    def _stub_runners(self, monkeypatch):
        self.calls = {}
        for name in (
            "run_list", "run_add", "run_scrobble", "run_login",
            "run_logout", "run_install", "run_uninstall",
            "run_add_url", "run_del", "run_refresh",
        ):
            rec = _Recorder()
            self.calls[name] = rec
            monkeypatch.setattr(main_mod, name, rec)
        self.network = _Recorder()
        monkeypatch.setattr(main_mod, "_require_network", self.network)
        self.verbosity = _Recorder()
        monkeypatch.setattr(main_mod, "configure_logging", self.verbosity)
        monkeypatch.setattr(main_mod, "set_language", lambda lang: None)

    def _run(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["playlistmanager", *argv])
        with pytest.raises(SystemExit) as e:
            main_mod.main()
        return e.value.code

    def _expect(self, name, *args):
        rec = self.calls[name]
        assert rec.calls == [((*args,), {})]

    def test_no_args_launches_gui(self, monkeypatch):
        monkeypatch.setattr(main_mod, "_import_app", lambda: _FakeApp)
        code = self._run(monkeypatch)
        assert code == 0
        assert _FakeApp.last_args is not None
        assert _FakeApp.last_args.add_song_targets is None

    def test_list_dispatches(self, monkeypatch):
        assert self._run(monkeypatch, "--list") == 0
        self._expect("run_list")
        assert self.network.calls == []

    def test_login_dispatches_with_flags(self, monkeypatch):
        code = self._run(
            monkeypatch,
            "--login", "spotify",
            "--client-id", "CID", "--client-secret", "SEC", "--refresh-token", "TOK",
        )
        assert code == 0
        self._expect("run_login", "spotify", "CID", "SEC", "TOK")

    def test_logout_dispatches(self, monkeypatch):
        assert self._run(monkeypatch, "--logout", "spotify") == 0
        self._expect("run_logout", "spotify")

    def test_install_dispatches(self, monkeypatch):
        assert self._run(monkeypatch, "--install", "all") == 0
        self._expect("run_install", "all")

    def test_uninstall_dispatches(self, monkeypatch):
        assert self._run(monkeypatch, "--uninstall", "spotify") == 0
        self._expect("run_uninstall", "spotify")

    def test_add_gated_on_network(self, monkeypatch):
        assert self._run(monkeypatch, "-a", "1,2,3") == 0
        self._expect("run_add", "1,2,3")
        assert len(self.network.calls) == 1

    def test_scrobble_gated_on_network(self, monkeypatch):
        assert self._run(monkeypatch, "-s") == 0
        self._expect("run_scrobble")
        assert len(self.network.calls) == 1

    def test_playlist_add_gated_on_network(self, monkeypatch):
        assert self._run(monkeypatch, "-p", "add", "https://x/playlist?list=1") == 0
        self._expect("run_add_url", "https://x/playlist?list=1")
        assert len(self.network.calls) == 1

    def test_playlist_del_is_local_only(self, monkeypatch):
        # del/delete only touch the local registry + DBs - no network gate.
        assert self._run(monkeypatch, "-p", "del", "1", "2") == 0
        self._expect("run_del", "1,2")
        assert self.network.calls == []

    def test_playlist_delete_alias(self, monkeypatch):
        assert self._run(monkeypatch, "-p", "delete", "3") == 0
        self._expect("run_del", "3")

    def test_playlist_refresh_gated_on_network(self, monkeypatch):
        assert self._run(monkeypatch, "-p", "ref", "1") == 0
        self._expect("run_refresh", "1")
        assert len(self.network.calls) == 1

    def test_playlist_refresh_alias(self, monkeypatch):
        assert self._run(monkeypatch, "-p", "refresh", "2,3") == 0
        self._expect("run_refresh", "2,3")

    def test_playlist_add_without_url_passes_empty(self, monkeypatch):
        assert self._run(monkeypatch, "-p", "add") == 0
        self._expect("run_add_url", "")

    def test_unknown_playlist_action(self, monkeypatch, capsys):
        code = self._run(monkeypatch, "-p", "bogus", "x")
        assert code == 2
        assert "unknown playlist action 'bogus'" in capsys.readouterr().err
        assert all(rec.calls == [] for rec in self.calls.values())

    def test_login_precedence_over_add(self, monkeypatch):
        assert self._run(monkeypatch, "--login", "spotify", "-a", "1") == 0
        self._expect("run_login", "spotify", None, None, None)
        assert self.calls["run_add"].calls == []
        assert self.network.calls == []

    def test_verbosity_levels(self, monkeypatch):
        self._run(monkeypatch, "-a", "1")
        assert self.verbosity.calls[-1] == ((0,), {})
        self._run(monkeypatch, "-v", "--list")
        assert self.verbosity.calls[-1] == ((1,), {})
        self._run(monkeypatch, "--debug", "-a", "1")
        assert self.verbosity.calls[-1] == ((2,), {})
        self._run(monkeypatch, "-vvv", "--list")
        assert self.verbosity.calls[-1] == ((3,), {})


# ---------------------------------------------------------------------------
# cli target parsing: _parse_token
# ---------------------------------------------------------------------------


class TestParseToken:
    def test_digits_are_order_numbers(self):
        assert cli._parse_token("7") == ("number", 7)

    def test_whitespace_stripped(self):
        assert cli._parse_token("  7  ") == ("number", 7)

    def test_range(self):
        assert cli._parse_token("2-4") == ("range", (2, 4))

    def test_range_with_spaces(self):
        assert cli._parse_token("2 - 4") == ("range", (2, 4))

    def test_inverted_range_raises(self):
        with pytest.raises(cli.UsageError):
            cli._parse_token("4-2")

    def test_hanging_dash_is_a_name(self):
        assert cli._parse_token("1-")[0] == "name"

    def test_multi_dash_is_a_name(self):
        assert cli._parse_token("1-2-3")[0] == "name"

    def test_name_keeps_case_and_spaces(self):
        assert cli._parse_token("Chill Mix") == ("name", "Chill Mix")

    def test_url_without_allow_urls_is_a_name(self):
        assert cli._parse_token("https://x/y")[0] == "name"

    def test_url_with_allow_urls(self, monkeypatch):
        monkeypatch.setattr(
            cli, "parse_playlist_url", lambda url: ("spotify", "abc123")
        )
        kind, value = cli._parse_token(
            "https://open.spotify.com/playlist/abc123", allow_urls=True
        )
        assert kind == "url"
        assert value == ("spotify", "abc123")

    def test_spotify_uri_scheme(self, monkeypatch):
        monkeypatch.setattr(
            cli, "parse_playlist_url", lambda url: ("spotify", "abc123")
        )
        assert cli._parse_token("spotify:playlist:abc123", allow_urls=True)[0] == "url"

    def test_bad_url_raises_usage_error(self, monkeypatch):
        def _reject(url):
            raise ValueError("Unrecognized playlist URL")

        monkeypatch.setattr(cli, "parse_playlist_url", _reject)
        with pytest.raises(cli.UsageError):
            cli._parse_token("https://bad.example/nope", allow_urls=True)


def _playlists():
    """Four entries: #1/#3 share a name on different platforms (#3 the
    ambiguity demo), #2 is distinct, #4 is a legacy entry with no id."""
    return [
        {"name": "Chill Mix", "platform": "youtube_music", "playlist_id": "PL1"},
        {"name": "Workout", "platform": "spotify", "playlist_id": "SP1"},
        {"name": "Chill Mix", "platform": "spotify", "playlist_id": "SP2"},
        {"name": "old", "platform": "youtube_music"},
    ]


class TestResolveTargets:
    def test_single_number(self):
        assert cli.resolve_targets("1", _playlists()) == [(1, _playlists()[0])]

    def test_number_list(self):
        resolved = cli.resolve_targets("1,3", _playlists())
        assert [n for n, _ in resolved] == [1, 3]

    def test_spaces_around_commas(self):
        assert [n for n, _ in cli.resolve_targets("1, 2, 3", _playlists())] == [1, 2, 3]

    def test_lower_bound_is_one(self):
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("0", _playlists())

    def test_out_of_range_high(self):
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("5", _playlists())

    def test_range_expands_inclusive(self):
        resolved = cli.resolve_targets("2-3", _playlists())
        assert [n for n, _ in resolved] == [2, 3]
        assert resolved[0][1]["playlist_id"] == "SP1"

    def test_range_out_of_bounds(self):
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("3-5", _playlists())

    def test_range_crosses_lower_bound(self):
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("0-2", _playlists())

    def test_duplicate_numbers_deduped(self):
        assert [n for n, _ in cli.resolve_targets("1,1,2", _playlists())] == [1, 2]

    def test_overlapping_number_and_range_deduped(self):
        assert [n for n, _ in cli.resolve_targets("1,1-2", _playlists())] == [1, 2]

    def test_empty_spec_raises(self):
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("", _playlists())

    def test_blank_spec_raises(self):
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("   ", _playlists())

    def test_name_exact_match(self):
        resolved = cli.resolve_targets("Workout", _playlists())
        assert resolved == [(2, _playlists()[1])]

    def test_name_case_insensitive_fallback(self):
        assert cli.resolve_targets("workout", _playlists())[0][0] == 2

    def test_name_not_found(self):
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("Nope", _playlists())

    def test_ambiguous_name(self):
        with pytest.raises(cli.UsageError) as e:
            cli.resolve_targets("Chill Mix", _playlists())
        assert "ambiguous" in str(e.value)

    def test_exact_match_beats_case_variant(self):
        playlists = [
            {"name": "Chip Tunes", "platform": "youtube_music", "playlist_id": "A"},
            {"name": "chip tunes", "platform": "spotify", "playlist_id": "B"},
        ]
        resolved = cli.resolve_targets("Chip Tunes", playlists)
        assert resolved == [(1, playlists[0])]

    def test_numeric_looking_name_is_an_order_number(self):
        # 123 > registry size -> out of range, NOT a "not found" name error.
        with pytest.raises(cli.UsageError) as e:
            cli.resolve_targets("123", _playlists())
        assert "out of range" in str(e.value)

    def test_mixed_numbers_and_names(self):
        resolved = cli.resolve_targets("1,Workout", _playlists())
        assert [n for n, _ in resolved] == [1, 2]

    def test_number_then_same_name_is_deduped(self):
        # #2 IS "Workout": resolving "2,Workout" must not duplicate it.
        playlists = [
            {"name": "A", "platform": "youtube_music", "playlist_id": "1"},
            {"name": "Workout", "platform": "spotify", "playlist_id": "2"},
        ]
        resolved = cli.resolve_targets("2,Workout", playlists)
        assert resolved == [(2, playlists[1])]

    def test_ambiguous_name_after_number_still_ambiguous(self):
        # Name resolution scans the whole registry, not just the remainder.
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("1,Chill Mix", _playlists())

    def test_url_token_requires_allow_urls(self):
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("https://open.spotify.com/playlist/SP1", _playlists())

    def test_registered_url_resolves(self, monkeypatch):
        monkeypatch.setattr(
            cli, "parse_playlist_url", lambda url: ("spotify", "SP1")
        )
        resolved = cli.resolve_targets(
            "https://open.spotify.com/playlist/SP1", _playlists(), allow_urls=True
        )
        assert resolved == [(2, _playlists()[1])]

    def test_unregistered_url_raises(self, monkeypatch):
        monkeypatch.setattr(
            cli, "parse_playlist_url", lambda url: ("spotify", "SP999")
        )
        with pytest.raises(cli.UsageError):
            cli.resolve_targets(
                "https://open.spotify.com/playlist/SP999",
                _playlists(),
                allow_urls=True,
            )

    def test_empty_registry_any_number_out_of_range(self):
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("1", [])


class TestResolveTargetsFor:
    def test_all_keyword(self):
        resolved = cli.resolve_targets_for("all", _playlists())
        assert [n for n, _ in resolved] == [1, 2, 3, 4]

    def test_all_case_and_space_insensitive(self):
        for spec in ("ALL", " all "):
            assert [n for n, _ in cli.resolve_targets_for(spec, _playlists())] == [1, 2, 3, 4]

    def test_all_only_valid_via_resolve_targets_for(self):
        # "all" is a name to resolve_targets; only the del/ref wrapper
        # understands it as a keyword.
        with pytest.raises(cli.UsageError):
            cli.resolve_targets("all", _playlists())

    def test_urls_allowed_by_default(self, monkeypatch):
        monkeypatch.setattr(
            cli, "parse_playlist_url", lambda url: ("spotify", "SP1")
        )
        resolved = cli.resolve_targets_for(
            "https://open.spotify.com/playlist/SP1", _playlists()
        )
        assert resolved == [(2, _playlists()[1])]


# ---------------------------------------------------------------------------
# run_* argument validation (in-memory store, no network)
# ---------------------------------------------------------------------------


class _FakeStore:
    """In-memory PlaylistStore stand-in (test-only)."""

    _items = []

    @classmethod
    def reset(cls, items):
        cls._items = [dict(i) for i in items]

    @classmethod
    def load_playlists(cls):
        return [dict(i) for i in cls._items]

    @classmethod
    def delete_playlist(cls, name, platform="youtube_music", playlist_id=""):
        cls._items = [
            p
            for p in cls._items
            if not (
                p.get("name") == name
                and (p.get("platform") or "youtube_music") == platform
                and (p.get("playlist_id") or "") == playlist_id
            )
        ]


def _use_fake_store(monkeypatch, items):
    _FakeStore.reset(items)
    monkeypatch.setattr(cli, "PlaylistStore", _FakeStore)


class TestRunCommands:
    def test_run_list_empty(self, monkeypatch, capsys):
        _use_fake_store(monkeypatch, [])
        assert cli.run_list() == 2
        assert capsys.readouterr().err

    def test_run_list_prints_numbered_entries(self, monkeypatch, capsys):
        _use_fake_store(monkeypatch, _playlists())
        assert cli.run_list() == 0
        out = capsys.readouterr().out
        assert '1. "Chill Mix"' in out and "Workout" in out
        assert '4. "old"' in out

    def test_run_del_empty_registry(self, monkeypatch):
        _use_fake_store(monkeypatch, [])
        assert cli.run_del("1") == 2

    def test_run_del_deletes_target(self, monkeypatch):
        _use_fake_store(monkeypatch, _playlists())
        assert cli.run_del("1") == 0
        assert len(_FakeStore._items) == 3
        assert all(p.get("playlist_id") != "PL1" for p in _FakeStore._items)

    def test_run_del_out_of_range(self, monkeypatch, capsys):
        _use_fake_store(monkeypatch, _playlists())
        assert cli.run_del("99") == 2
        assert "out of range" in capsys.readouterr().err

    def test_run_del_unknown_name(self, monkeypatch, capsys):
        _use_fake_store(monkeypatch, _playlists())
        assert cli.run_del("Nope") == 2
        assert "not found" in capsys.readouterr().err

    def test_run_del_empty_spec(self, monkeypatch, capsys):
        _use_fake_store(monkeypatch, _playlists())
        assert cli.run_del("") == 2
        assert "No playlists given" in capsys.readouterr().err

    def test_run_del_all_keyword(self, monkeypatch):
        _use_fake_store(monkeypatch, _playlists())
        assert cli.run_del("all") == 0
        assert _FakeStore._items == []

    def test_run_del_legacy_entry_without_id(self, monkeypatch):
        _use_fake_store(monkeypatch, _playlists())
        assert cli.run_del("4") == 0
        assert len(_FakeStore._items) == 3

    def test_run_del_survives_an_unwritable_extra_json(self, monkeypatch, capsys):
        # db/extra.json can fail to persist (full disk, permissions).  The
        # registry entry and the song DB are already gone, so the delete
        # must still report success and must not skip the remaining targets.
        _use_fake_store(monkeypatch, _playlists())

        def _boom(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(cli.duplicate_queue, "purge_playlist", _boom)
        assert cli.run_del("1,2") == 0
        captured = capsys.readouterr()
        # Both targets were still deleted and reported; only the legacy
        # entry #4 is left in the registry.
        assert len(_FakeStore._items) == 2
        assert all(p.get("playlist_id") != "PL1" for p in _FakeStore._items)
        assert all(p.get("playlist_id") != "PL2" for p in _FakeStore._items)
        # The purge failure is surfaced, not swallowed.
        assert "No space left" in captured.err
        assert captured.out.count("\n") >= 2

    def test_run_logout_survives_an_unwritable_extra_json(
        self, monkeypatch, capsys, registry
    ):
        # Credentials, registry entries and DBs are already deleted by the
        # time the duplicate-queue purge runs, so a failed purge must be a
        # warning - the logout itself succeeded, and the scrobble-ledger
        # cleanup that follows must still run.
        purged_ledger = []
        monkeypatch.setattr(
            cli.auth_setup,
            "delete_platform_credentials",
            lambda platform, **kw: (["token"], []),
        )

        def _boom(platform):
            raise OSError("No space left on device")

        monkeypatch.setattr(cli.duplicate_queue, "purge_platform", _boom)
        monkeypatch.setattr(
            cli.scrobble_log,
            "remove_platform_entries",
            lambda platform: purged_ledger.append(platform),
        )

        assert cli.run_logout("spotify") == 0
        captured = capsys.readouterr()
        assert "No space left" in captured.err
        assert purged_ledger == ["spotify"]

    def test_run_refresh_empty_registry(self, monkeypatch):
        _use_fake_store(monkeypatch, [])
        assert cli.run_refresh("1") == 2

    def test_run_refresh_legacy_entry_without_id(self, monkeypatch):
        # A legacy entry with no playlist_id cannot be re-imported - the
        # per-target failure path must fire before any network work.
        _use_fake_store(monkeypatch, _playlists())
        monkeypatch.setattr(cli, "_build_integrations", lambda **kw: SimpleNamespace())
        assert cli.run_refresh("4") == 1

    def test_run_add_empty_registry(self, monkeypatch, capsys):
        _use_fake_store(monkeypatch, [])
        assert cli.run_add("1") == 2
        assert capsys.readouterr().err

    def test_run_add_empty_spec(self, monkeypatch, capsys):
        _use_fake_store(monkeypatch, _playlists())
        assert cli.run_add("") == 2
        assert "No playlists given" in capsys.readouterr().err

    def test_run_add_out_of_range(self, monkeypatch, capsys):
        _use_fake_store(monkeypatch, _playlists())
        assert cli.run_add("8") == 2
        assert "out of range" in capsys.readouterr().err

    def test_run_add_url_unparseable(self, monkeypatch, capsys):
        def _reject(url):
            raise ValueError("Unrecognized playlist URL")

        monkeypatch.setattr(cli, "parse_playlist_url", _reject)
        assert cli.run_add_url("https://bad.example/nope") == 2
        assert "Unrecognized playlist URL" in capsys.readouterr().err

    def test_run_add_url_unknown_platform_errors_before_network(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli, "parse_playlist_url", lambda url: ("spotify", "abc123")
        )
        monkeypatch.setattr(
            cli, "_build_integrations",
            lambda **kw: SimpleNamespace(get=lambda pid: None),
        )
        assert cli.run_add_url("https://open.spotify.com/playlist/abc123") == 1
        assert capsys.readouterr().err


# ---------------------------------------------------------------------------
# run_login argument validation (prompts stubbed, verify stubbed)
# ---------------------------------------------------------------------------


class TestRunLoginValidation:
    def test_unknown_platform(self, monkeypatch, capsys):
        # "snarfle" can never be an installed platform id.
        assert cli.run_login("snarfle") == 2
        assert "snarfle" in capsys.readouterr().err

    def test_lastfm_is_gui_only(self, monkeypatch, capsys, registry):
        assert cli.run_login("lastfm") == 2
        assert capsys.readouterr().err

    def test_spotify_missing_credentials(self, monkeypatch, capsys, registry):
        monkeypatch.setattr(cli, "_prompt", lambda *a, **k: "")
        monkeypatch.setattr(cli, "_stored_refresh_token", lambda: None)
        assert cli.run_login("spotify") == 2
        assert capsys.readouterr().err

    def test_spotify_falls_back_to_stored_refresh_token(self, monkeypatch, capsys, registry):
        # id/secret come from flags; the refresh-token prompt is left
        # empty and must fall back to the stored token.  A failed verify
        # must be reported without touching the network.
        verify_calls = []

        def _verify(cid, sec, tok):
            verify_calls.append((cid, sec, tok))
            return {"ok": False, "error": "verify boom"}

        monkeypatch.setattr(cli, "_prompt", lambda *a, **k: "")
        monkeypatch.setattr(cli, "_stored_refresh_token", lambda: "STORED")
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_spotify_credentials", _verify
        )
        assert cli.run_login("spotify", "CID", "SEC") == 1
        assert verify_calls == [("CID", "SEC", "STORED")]
        assert "verify boom" in capsys.readouterr().err

    def test_spotify_flags_skip_prompts(self, monkeypatch, registry):
        def _no_prompt(*a, **k):
            raise AssertionError("flag-supplied credentials must not prompt")

        monkeypatch.setattr(cli, "_prompt", _no_prompt)
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_spotify_credentials",
            lambda cid, sec, tok: {"ok": True, "display_name": "d1pl"},
        )
        assert cli.run_login("spotify", "A", "B", "C") == 0

    def test_deezer_requires_arl(self, monkeypatch, capsys, registry):
        monkeypatch.setattr(cli, "_prompt", lambda *a, **k: "")
        assert cli.run_login("deezer") == 2
        assert capsys.readouterr().err

    def test_soundcloud_requires_all_credentials(self, monkeypatch, capsys, registry):
        monkeypatch.setattr(cli, "_prompt", lambda *a, **k: "")
        assert cli.run_login("soundcloud") == 2
        assert capsys.readouterr().err


# ---------------------------------------------------------------------------
# --login credential sources: flag > env > hidden prompt
# ---------------------------------------------------------------------------


class TestLoginCredentialSources:
    """The three ways a credential can reach ``--login``.

    argv is the worst of them (``/proc/<pid>/cmdline`` is world-readable
    and the value is kept in the shell history), so the environment tier
    exists to keep a real secret off the command line while staying
    scriptable.  Precedence is flag > env > prompt: an explicit flag is a
    deliberate per-run override of an exported variable.
    """

    def test_env_credential_name_and_blank_handling(self, monkeypatch):
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET", "  s3cret  ")
        assert cli.env_credential("spotify", "client_secret") == "s3cret"
        # Blank / whitespace-only counts as unset -> falls through to the
        # prompt instead of verifying with an empty secret.
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_REFRESH_TOKEN", "   ")
        assert cli.env_credential("spotify", "refresh_token") is None
        assert cli.env_credential("spotify", "client_id") is None

    def test_spotify_env_supplies_all_three_without_prompting(
        self, monkeypatch, registry
    ):
        def _no_prompt(*a, **k):
            raise AssertionError("env-supplied credentials must not prompt")

        monkeypatch.setattr(cli, "_prompt", _no_prompt)
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_CLIENT_ID", "EnvCID")
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET", "EnvSecret1")
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_REFRESH_TOKEN", "EnvToken1")
        seen = []
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_spotify_credentials",
            lambda cid, sec, tok: seen.append((cid, sec, tok))
            or {"ok": True, "display_name": "d1pl"},
        )
        assert cli.run_login("spotify") == 0
        assert seen == [("EnvCID", "EnvSecret1", "EnvToken1")]

    def test_flag_overrides_env(self, monkeypatch, registry):
        def _no_prompt(*a, **k):
            raise AssertionError("nothing left to prompt for")

        monkeypatch.setattr(cli, "_prompt", _no_prompt)
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET", "EnvSecret1")
        seen = []
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_spotify_credentials",
            lambda cid, sec, tok: seen.append((cid, sec, tok))
            or {"ok": True},
        )
        assert cli.run_login("spotify", "FlagCID", "FlagSecret1", "FlagToken1") == 0
        assert seen == [("FlagCID", "FlagSecret1", "FlagToken1")]

    def test_env_token_beats_stored_token(self, monkeypatch, registry):
        # The stored token is the LAST resort: an env-supplied token wins,
        # because the user just handed us a newer one.
        monkeypatch.setattr(cli, "_prompt", lambda *a, **k: "")
        monkeypatch.setattr(cli, "_stored_refresh_token", lambda: "STORED")
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_REFRESH_TOKEN", "EnvToken1")
        seen = []
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_spotify_credentials",
            lambda cid, sec, tok: seen.append(tok) or {"ok": True},
        )
        assert cli.run_login("spotify", "CID", "SEC") == 0
        assert seen == ["EnvToken1"]

    def test_partial_env_still_prompts_for_the_rest(self, monkeypatch, registry):
        asked = []

        def _prompt(label, hidden=False):
            asked.append(label)
            return "PromptCID" if "id" in label.lower() else "Prompted"

        monkeypatch.setattr(cli, "_prompt", _prompt)
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET", "EnvSecret1")
        seen = []
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_spotify_credentials",
            lambda cid, sec, tok: seen.append((cid, sec, tok)) or {"ok": True},
        )
        assert cli.run_login("spotify") == 0
        # The secret came from the env, so its label is never asked; the
        # client id and the token still are.
        assert asked == ["Client id: ", "Refresh token (leave empty to reuse stored): "]
        assert seen == [("PromptCID", "EnvSecret1", "Prompted")]

    def test_soundcloud_env_is_platform_scoped(self, monkeypatch, registry):
        # The Spotify export must not satisfy --login soundcloud: a
        # wrong-platform secret is a confusing verify failure.
        def _no_prompt(*a, **k):
            raise AssertionError("only soundcloud env vars are valid here")

        monkeypatch.setattr(cli, "_prompt", _no_prompt)
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_CLIENT_ID", "SpotifyCID")
        monkeypatch.setenv("PLAYLISTMANAGER_SOUNDCLOUD_CLIENT_ID", "ScCID")
        monkeypatch.setenv("PLAYLISTMANAGER_SOUNDCLOUD_CLIENT_SECRET", "ScSecret1")
        monkeypatch.setenv("PLAYLISTMANAGER_SOUNDCLOUD_REFRESH_TOKEN", "ScToken1")
        seen = []
        monkeypatch.setattr(
            cli, "_soundcloud_save_and_verify",
            lambda cid, sec, tok: seen.append((cid, sec, tok))
            or {"ok": True, "display_name": "sc"},
        )
        assert cli.run_login("soundcloud") == 0
        assert seen == [("ScCID", "ScSecret1", "ScToken1")]

    def test_deezer_env_arl_skips_the_prompt(self, monkeypatch, registry):
        def _no_prompt(*a, **k):
            raise AssertionError("PLAYLISTMANAGER_DEEZER_ARL must skip the prompt")

        monkeypatch.setattr(cli, "_prompt", _no_prompt)
        monkeypatch.setenv("PLAYLISTMANAGER_DEEZER_ARL", "ArlFromEnv123")
        seen = []
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_deezer_credentials",
            lambda arl: seen.append(arl) or {"ok": True, "display_name": "dz"},
        )
        assert cli.run_login("deezer") == 0
        assert seen == ["ArlFromEnv123"]

    def test_deezer_still_prompts_without_env(self, monkeypatch, registry):
        monkeypatch.setattr(cli, "_prompt", lambda *a, **k: "ArlFromPrompt1")
        seen = []
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_deezer_credentials",
            lambda arl: seen.append(arl) or {"ok": True, "display_name": "dz"},
        )
        assert cli.run_login("deezer") == 0
        assert seen == ["ArlFromPrompt1"]


class TestLoginSecretHygiene:
    def test_resolved_secret_is_registered_for_masking(
        self, monkeypatch, capsys, registry, _clean_secret_registry
    ):
        # An env-supplied secret reaches the verify call, but the failure
        # reason is printed to the terminal - which redact() does not
        # touch.  Registering at resolution time is what keeps it out of
        # both the log and that message.
        secret = "EnvSecretLeak1"
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_CLIENT_ID", "CID")
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET", secret)
        monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_REFRESH_TOKEN", "TokenLeak1")
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_spotify_credentials",
            lambda cid, sec, tok: {"ok": False, "error": f"401 invalid {sec}"},
        )
        assert cli.run_login("spotify") == 1
        err = capsys.readouterr().err
        assert secret not in err
        assert "401 invalid" in err  # the message survives, the value does not

    def test_secret_value_is_never_logged(self, monkeypatch, registry, caplog, _clean_secret_registry):
        # Source reporting is for troubleshooting ("why was my env var
        # ignored?") and must name the source only.
        import logging

        secret = "EnvSecretLeak2"
        with caplog.at_level(logging.DEBUG, logger="cli"):
            monkeypatch.setenv("PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET", secret)
            monkeypatch.setattr(
                cli.auth_setup, "save_and_verify_spotify_credentials",
                lambda cid, sec, tok: {"ok": True},
            )
            assert cli.run_login("spotify", "CID", None, "TOK") == 0
        assert secret not in caplog.text
        assert "PLAYLISTMANAGER_SPOTIFY_CLIENT_SECRET" in caplog.text


class TestPromptInputPaths:
    """``getpass`` only on a TTY; a plain read otherwise.

    ``getpass`` opens ``/dev/tty`` itself and, when that fails, falls back
    to reading stdin *with a warning that the input may be echoed*.  Under
    a compositor keybind, a pipe or CI there is no terminal to echo to, and
    that fallback would be the only warning the user ever sees.
    """

    def test_tty_uses_getpass(self, monkeypatch):
        class _Tty(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stdin", _Tty(""))
        calls = []
        monkeypatch.setattr(
            cli.getpass, "getpass", lambda label: calls.append(label) or "TypedSecret"
        )
        assert cli._prompt("Client secret: ", hidden=True) == "TypedSecret"
        assert calls == ["Client secret: "]

    def test_non_tty_never_touches_getpass(self, monkeypatch):
        def _explode(*a, **k):
            raise AssertionError("getpass must not run without a TTY")

        monkeypatch.setattr(sys, "stdin", io.StringIO("PipedSecret1\n"))
        monkeypatch.setattr(cli.getpass, "getpass", _explode)
        assert cli._prompt("Client secret: ", hidden=True) == "PipedSecret1"

    def test_piped_secret_from_stdin_keeps_it_off_argv(self, monkeypatch, registry):
        # The scripting route: `... --login spotify --client-id ID < secret.txt`
        monkeypatch.setattr(sys, "stdin", io.StringIO("PipedSecret1\n"))
        seen = []
        monkeypatch.setattr(
            cli.auth_setup, "save_and_verify_spotify_credentials",
            lambda cid, sec, tok: seen.append((cid, sec, tok)) or {"ok": True},
        )
        assert cli.run_login("spotify", "CID", None, "TOK") == 0
        assert seen == [("CID", "PipedSecret1", "TOK")]

    def test_prompt_label_goes_to_stderr_keeping_stdout_parseable(
        self, monkeypatch, capsys
    ):
        # The login command prints exactly one result line on stdout; a
        # prompt label there would break `out=$(playlistmanager --login ...)`.
        monkeypatch.setattr(sys, "stdin", io.StringIO("PipedSecret1\n"))
        cli._prompt("Client secret: ", hidden=True)
        out, err = capsys.readouterr()
        assert out == ""
        assert "Client secret: " in err

    def test_eof_returns_none(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setattr(
            cli.getpass, "getpass",
            lambda label: (_ for _ in ()).throw(AssertionError("no TTY here")),
        )
        assert cli._prompt("Client secret: ", hidden=True) is None

    def test_closed_stdin_returns_none(self, monkeypatch):
        closed = io.StringIO("x")
        closed.close()
        monkeypatch.setattr(sys, "stdin", closed)
        assert cli._prompt("Client secret: ", hidden=True) is None

    def test_no_stdin_stream_returns_none(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", None)
        assert cli._prompt("Client secret: ", hidden=True) is None
        assert cli._stdin_is_tty() is False


# ---------------------------------------------------------------------------
# --install / --uninstall argument validation
# ---------------------------------------------------------------------------


class TestResolvePlatformArg:
    @pytest.fixture(autouse=True)
    def _catalog(self, monkeypatch):
        monkeypatch.setattr(
            cli.integration_manager, "installable_ids",
            lambda: ["spotify", "youtube_music"],
        )

    def test_all_expands_to_catalog(self):
        assert cli._resolve_platform_arg("all") == ["spotify", "youtube_music"]

    def test_known_platform(self):
        assert cli._resolve_platform_arg("spotify") == ["spotify"]

    def test_unknown_platform_raises(self):
        with pytest.raises(cli.UsageError):
            cli._resolve_platform_arg("nope")

    def test_run_install_unknown_platform(self, capsys):
        assert cli.run_install("nope") == 2
        assert "unknown platform 'nope'" in capsys.readouterr().err

    def test_run_uninstall_unknown_platform(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli, "get_default_registry", lambda: SimpleNamespace(get=lambda pid: None)
        )
        assert cli.run_uninstall("nope") == 2
        assert "nope" in capsys.readouterr().err