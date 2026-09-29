"""Scrobble-target selection (services/scrobble.py).

The core used to take "the first registered ScrobbleCapable integration" as
*the* scrobble backend.  That was arbitrary as soon as a second backend
plugin (ListenBrainz) exists, and wrong in a specific way: a scrobble
written to one service could later be deleted from another.  The replacement
answers a different question - not "which one" but "which ones" - because
two services out of three is a perfectly normal setup.  These tests pin the
contract: a per-platform ``[<platform>] scrobbles`` switch (absent = on), a
deterministic order, capability- and auth-aware candidacy, one backend
reached by name for the remove path, and the switch ignored exactly where
ignoring it is correct.
"""

from __future__ import annotations

import logging

import pytest

from services import scrobble as sc


class FakeIntegration:
    """Minimal duck-typed integration; every ScrobbleCapable method by
    default, so a test only has to *remove* what it does not support."""

    def __init__(self, pid, *, authenticated=True, **capabilities):
        self.id = pid
        self.display_name = pid.title()
        self.supports_playlists = False
        self._authenticated = authenticated
        for name in (
            "scrobble", "delete_scrobble", "love", "unlove", "is_loved",
            "get_loved_set",
        ):
            if capabilities.get(name, True):
                setattr(self, name, lambda *a, **k: None)

    def is_authenticated(self):
        return self._authenticated


class FakeRegistry:
    def __init__(self, *integrations):
        self._integrations = {i.id: i for i in integrations}

    def get_all(self):
        return dict(self._integrations)


@pytest.fixture(autouse=True)
def switches(monkeypatch):
    """Per-platform ``[<platform>] scrobbles`` state, in memory.

    Autouse: the switch defaults to *on*, so without this every test would
    read the shared settings file the rest of the suite writes to, and the
    ones that assert "only this backend" would silently pass or fail
    depending on test order.  ``state["off"]`` is the set of platforms with
    ``scrobbles = false``; anything else reads as enabled.
    """
    state = {"off": set()}
    monkeypatch.setattr(
        sc,
        "get_setting_bool",
        lambda section, option, fallback=False: section not in state["off"],
    )
    monkeypatch.setattr(sc, "_noted_targets", set())
    return state


def ids(backends) -> list[str]:
    return [b.id for b in backends]


# -- the per-platform switch -------------------------------------------------

class TestBackendSwitch:
    def test_an_untouched_platform_is_enabled(self, switches):
        # Absent means ON: installing and logging into a second backend must
        # make it work, and switching one off is always an explicit act.
        assert sc.scrobble_backend_enabled("listenbrainz") is True

    def test_false_switches_a_platform_off(self, switches):
        switches["off"].add("listenbrainz")
        assert sc.scrobble_backend_enabled("listenbrainz") is False
        assert sc.scrobble_backend_enabled("lastfm") is True

    # Every configparser spelling (true/yes/on/1 ...) and the "unreadable
    # value stays enabled" contract are asserted against the REAL reader in
    # test_config.py - here the switch is a bool already.
    def test_a_blank_platform_id_is_not_a_backend(self):
        assert sc.scrobble_backend_enabled("  ") is False


# -- candidacy ---------------------------------------------------------------

class TestCandidates:
    def test_none_registry_yields_nothing(self):
        assert sc.scrobble_backends(None) == []
        assert sc.resolve_scrobble_backend(None) is None
        assert sc.scrobble_backend_for(None, sc.SCROBBLE_DELETE, "lastfm") is None

    def test_a_registry_without_get_all_yields_nothing(self):
        assert sc.scrobble_backends(object()) == []

    def test_non_scrobble_integrations_are_excluded(self):
        class Plain:
            id = "spotify"

        assert sc.scrobble_backends(FakeRegistry(Plain())) == []

    def test_order_is_by_platform_id_not_registration_order(self):
        # The old bug: dict iteration order decided who got the scrobble.
        a, b = FakeIntegration("lastfm"), FakeIntegration("listenbrainz")
        assert ids(sc.scrobble_backends(FakeRegistry(b, a))) == [
            "lastfm",
            "listenbrainz",
        ]
        assert ids(sc.scrobble_backends(FakeRegistry(a, b))) == [
            "lastfm",
            "listenbrainz",
        ]

    def test_unauthenticated_backends_are_skipped(self):
        # A stale credential file must not shadow a working backend: with
        # the old "first registered wins" the scrobble went nowhere and the
        # user saw no error at all.
        dead = FakeIntegration("lastfm", authenticated=False)
        live = FakeIntegration("listenbrainz")
        assert ids(sc.scrobble_backends(FakeRegistry(dead, live))) == [
            "listenbrainz",
        ]

    def test_an_integration_without_the_capability_is_skipped(self):
        # ListenBrainz: scrobbles, but has no love concept.
        lb = FakeIntegration("listenbrainz", love=False, unlove=False, is_loved=False)
        lastfm = FakeIntegration("lastfm")
        registry = FakeRegistry(lb, lastfm)
        assert ids(sc.scrobble_backends(registry)) == ["lastfm", "listenbrainz"]
        assert sc.resolve_scrobble_backend(registry, sc.SCROBBLE_LIKE) is lastfm

    def test_a_duck_typed_stub_without_is_authenticated_is_eligible(self):
        class Stub:
            id = "stub"
            scrobble = lambda self, song: None

        stub = Stub()
        assert sc.scrobble_backends(FakeRegistry(stub)) == [stub]

    def test_an_is_authenticated_that_raises_is_skipped(self):
        class Broken(FakeIntegration):
            def is_authenticated(self):
                raise RuntimeError("boom")

        live = FakeIntegration("listenbrainz")
        assert ids(sc.scrobble_backends(FakeRegistry(Broken("lastfm"), live))) == [
            "listenbrainz",
        ]


# -- the selection ------------------------------------------------------------

class TestSelection:
    def test_single_backend_needs_no_configuration(self):
        only = FakeIntegration("lastfm")
        assert sc.scrobble_backends(FakeRegistry(only)) == [only]

    def test_every_enabled_backend_receives_the_listen(self):
        # The whole point of the feature: two services, both logged in, both
        # left at the default - the scrobble goes to BOTH, not to whichever
        # the registry happened to yield first.
        registry = FakeRegistry(
            FakeIntegration("lastfm"), FakeIntegration("listenbrainz")
        )
        assert ids(sc.scrobble_backends(registry)) == ["lastfm", "listenbrainz"]

    def test_one_switched_off_leaves_the_other(self, switches):
        switches["off"].add("listenbrainz")
        registry = FakeRegistry(
            FakeIntegration("lastfm"), FakeIntegration("listenbrainz")
        )
        assert ids(sc.scrobble_backends(registry)) == ["lastfm"]

    def test_two_of_three_enabled_is_expressible(self, switches):
        # The reason this is a per-platform switch and not one "the backend"
        # setting.
        switches["off"].add("spotify_scrobbler")
        registry = FakeRegistry(
            FakeIntegration("lastfm"),
            FakeIntegration("listenbrainz"),
            FakeIntegration("spotify_scrobbler"),
        )
        assert ids(sc.scrobble_backends(registry)) == [
            "lastfm",
            "listenbrainz",
        ]

    def test_switching_everything_off_selects_nothing(self, switches):
        switches["off"].update({"lastfm", "listenbrainz"})
        registry = FakeRegistry(
            FakeIntegration("lastfm"), FakeIntegration("listenbrainz")
        )
        assert sc.scrobble_backends(registry) == []

    def test_selection_is_deterministic(self):
        registry = FakeRegistry(
            FakeIntegration("listenbrainz"), FakeIntegration("lastfm")
        )
        picks = {tuple(ids(sc.scrobble_backends(registry))) for _ in range(20)}
        assert picks == {("lastfm", "listenbrainz")}

    def test_a_switched_off_backend_is_still_visible_when_asked(self, switches):
        # Settings has to be able to list the OFF ones, or the user could
        # never switch one back on.
        switches["off"].add("listenbrainz")
        registry = FakeRegistry(
            FakeIntegration("lastfm"), FakeIntegration("listenbrainz")
        )
        assert ids(sc.scrobble_backends(registry, only_enabled=False)) == [
            "lastfm",
            "listenbrainz",
        ]

    def test_the_capability_is_part_of_the_query(self):
        # A backend implementing only the love half is not a scrobble
        # target, and vice versa.
        only_love = FakeIntegration(
            "lastfm", scrobble=False, delete_scrobble=False
        )
        registry = FakeRegistry(only_love)
        assert sc.scrobble_backends(registry) == []
        assert sc.scrobble_backends(registry, sc.SCROBBLE_LOVED_SET) == [only_love]


class TestMultiTargetNote:
    def test_the_targets_are_named_once_per_set(self, switches, caplog):
        registry = FakeRegistry(
            FakeIntegration("lastfm"), FakeIntegration("listenbrainz")
        )
        with caplog.at_level(logging.INFO, logger="services.scrobble"):
            for _ in range(5):
                sc.scrobble_backends(registry)
        lines = [r for r in caplog.records if "lastfm" in r.getMessage()]
        assert len(lines) == 1
        assert "listenbrainz" in lines[0].getMessage()

    def test_nothing_is_said_about_a_single_backend(self, caplog):
        registry = FakeRegistry(FakeIntegration("lastfm"))
        with caplog.at_level(logging.INFO, logger="services.scrobble"):
            sc.scrobble_backends(registry)
        assert not [r for r in caplog.records if "Scrobbling to" in r.getMessage()]

    def test_nothing_is_said_when_the_ignore_flag_path_runs(self, switches, caplog):
        # only_enabled=False is the remove/love path: it does not scrobble,
        # so "you are scrobbling to two services" would be a lie there.
        registry = FakeRegistry(
            FakeIntegration("lastfm"), FakeIntegration("listenbrainz")
        )
        with caplog.at_level(logging.INFO, logger="services.scrobble"):
            sc.scrobble_backends(registry, only_enabled=False)
        assert not caplog.records

    def test_a_changed_set_is_noted_again(self, switches, caplog):
        registry = FakeRegistry(
            FakeIntegration("lastfm"), FakeIntegration("listenbrainz")
        )
        with caplog.at_level(logging.INFO, logger="services.scrobble"):
            sc.scrobble_backends(registry)
            switches["off"].add("listenbrainz")
            sc.scrobble_backends(registry)
        assert len([r for r in caplog.records if "Scrobbling to" in r.getMessage()]) == 1


# -- addressing one recorded backend (the remove path) -----------------------

class TestBackendFor:
    def test_the_named_backend_is_found(self):
        lb = FakeIntegration("listenbrainz")
        registry = FakeRegistry(FakeIntegration("lastfm"), lb)
        assert sc.scrobble_backend_for(registry, sc.SCROBBLE_DELETE, "listenbrainz") is lb

    def test_the_name_is_matched_case_insensitively(self):
        lb = FakeIntegration("listenbrainz")
        registry = FakeRegistry(lb)
        assert sc.scrobble_backend_for(registry, sc.SCROBBLE_DELETE, "ListenBrainz") is lb

    def test_the_switch_is_ignored(self, switches):
        # The flag governs NEW scrobbles.  A listen recorded while the
        # backend was on is still sitting there after the user switches it
        # off, and the remove path has to be able to reach it - otherwise
        # removing a song orphans a scrobble the app can never clean up.
        switches["off"].add("listenbrainz")
        lb = FakeIntegration("listenbrainz")
        registry = FakeRegistry(FakeIntegration("lastfm"), lb)
        assert sc.scrobble_backend_for(registry, sc.SCROBBLE_DELETE, "listenbrainz") is lb

    def test_an_unknown_name_does_not_fall_back(self):
        # The backend that accepted the scrobble is gone (uninstalled).  The
        # delete must NOT be handed to whatever is enabled now: that service
        # never received the listen.
        registry = FakeRegistry(FakeIntegration("lastfm"))
        assert sc.scrobble_backend_for(
            registry, sc.SCROBBLE_DELETE, "uninstalled"
        ) is None

    def test_a_backend_without_the_capability_is_not_used(self):
        lb = FakeIntegration("listenbrainz", delete_scrobble=False)
        registry = FakeRegistry(lb)
        assert sc.scrobble_backend_for(registry, sc.SCROBBLE_DELETE, "listenbrainz") is None

    def test_a_blank_name_is_not_a_backend(self):
        registry = FakeRegistry(FakeIntegration("lastfm"))
        assert sc.scrobble_backend_for(registry, sc.SCROBBLE_DELETE, "  ") is None

    def test_an_integration_without_an_id_is_unreachable(self):
        # A plugin that forgets its id cannot be named in a ledger record.
        nameless = FakeIntegration("lastfm")
        nameless.id = ""
        assert sc.scrobble_backend_for(
            FakeRegistry(nameless), sc.SCROBBLE_DELETE, "lastfm"
        ) is None


# -- the love paths (one backend) --------------------------------------------

class TestResolve:
    def test_no_backend_means_none(self):
        assert sc.resolve_scrobble_backend(FakeRegistry()) is None

    def test_the_only_love_capable_backend_wins(self):
        # Not affected by the scrobble switch: a user who sends listens to
        # ListenBrainz only still wants the heart to work on Last.fm.
        lastfm = FakeIntegration("lastfm")
        lb = FakeIntegration("listenbrainz", love=False, unlove=False, is_loved=False)
        registry = FakeRegistry(lastfm, lb)
        assert sc.resolve_scrobble_backend(registry, sc.SCROBBLE_LIKE) is lastfm

    def test_a_switched_off_backend_still_serves_the_heart(self, switches):
        switches["off"].add("lastfm")
        lastfm = FakeIntegration("lastfm")
        registry = FakeRegistry(lastfm)
        assert sc.resolve_scrobble_backend(registry, sc.SCROBBLE_LOVED_SET) is lastfm


# -- capture platforms --------------------------------------------------------

class TestIsCapturePlatform:
    def test_a_platform_with_a_flow_can_capture(self):
        class Plugin:
            flow_class = "DemoFlow"

        class Registry:
            def get(self, pid):
                return Plugin() if pid == "spotify" else None

        assert sc.is_capture_platform(Registry(), "spotify") is True

    def test_a_service_backend_is_not_a_capture_source(self):
        # ListenBrainz must never be listed as something to scrobble FROM;
        # the Settings scrobble-source list is built from this.
        class Plugin:
            flow_class = ""

        class Registry:
            def get(self, pid):
                return Plugin()

        assert sc.is_capture_platform(Registry(), "listenbrainz") is False

    def test_no_registry_means_nothing_is_confirmed(self):
        assert sc.is_capture_platform(None, "spotify") is False

    def test_an_unknown_platform_is_not_a_capture_source(self):
        class Registry:
            def get(self, pid):
                return None

        assert sc.is_capture_platform(Registry(), "nope") is False
