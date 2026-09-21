"""Unit tests for utils/updater.py - the [update_check] interval policy.

Pure-function tests need no network; the check()-flow tests stub the
request plus the background thread (running inline) and record the
callback payload.
"""

import time

import pytest

from utils import updater


class _SyncThread:
    """Runs the target inline - turns check() into a synchronous call."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class TestReadInterval:
    def test_known_value_passthrough(self, monkeypatch):
        monkeypatch.setattr(
            updater, "get_setting_value", lambda *a, **k: "14d"
        )
        assert updater.read_interval() == "14d"

    def test_unknown_value_falls_back_to_launch(self, monkeypatch):
        monkeypatch.setattr(
            updater, "get_setting_value", lambda *a, **k: "garbage"
        )
        assert updater.read_interval() == updater.UPDATE_CHECK_DEFAULT_INTERVAL


class TestIntervalDue:
    def test_off_never_checks(self):
        assert updater.interval_due("off", 0.0, time.time()) is False
        assert updater.interval_due("off", time.time(), time.time()) is False

    def test_launch_always_checks(self):
        assert updater.interval_due("launch", time.time(), time.time()) is True

    def test_missing_stamp_counts_as_due(self):
        assert updater.interval_due("24h", 0.0, time.time()) is True

    def test_not_due_inside_interval(self):
        now = time.time()
        assert updater.interval_due("24h", now - 23 * 3600, now) is False

    def test_due_at_exact_interval(self):
        now = time.time()
        assert updater.interval_due("24h", now - 24 * 3600, now) is True

    def test_due_beyond_interval(self):
        now = time.time()
        assert updater.interval_due("12h", now - 48 * 3600, now) is True

    def test_seconds_map_covers_all_timed_intervals(self):
        from utils.config import UPDATE_CHECK_INTERVAL_SECONDS

        timed = set(updater.UPDATE_CHECK_INTERVALS) - {"off", "launch"}
        assert timed == set(UPDATE_CHECK_INTERVAL_SECONDS)
        assert all(v > 0 for v in UPDATE_CHECK_INTERVAL_SECONDS.values())

    def test_unknown_interval_defensively_due(self):
        assert updater.interval_due("garbage", time.time(), time.time()) is True


class TestLastCheckStamp:
    def test_parses_numeric(self, monkeypatch):
        monkeypatch.setattr(
            updater, "get_setting_value", lambda *a, **k: "1234567890"
        )
        assert updater._last_check() == 1234567890.0

    def test_garbage_parses_to_zero(self, monkeypatch):
        monkeypatch.setattr(
            updater, "get_setting_value", lambda *a, **k: "abc"
        )
        assert updater._last_check() == 0.0

    def test_stamp_writes_current_epoch(self, monkeypatch):
        writes = []

        def fake_set(section, option, value):
            writes.append((section, option, value))

        monkeypatch.setattr(updater, "set_setting_value", fake_set)
        before = int(time.time())
        updater._stamp_last_check()
        after = int(time.time())
        assert len(writes) == 1
        section, option, value = writes[0]
        assert section == "update_check"
        assert option == "last_check"
        assert before <= int(value) <= after


class TestParseVersion:
    def test_plain_versions(self):
        cases = [
            ("1.0.1", (1, 0, 1, 0, 0, 0)),
            ("1.0", (1, 0, 0, 0, 0, 0)),
            ("1.0.1.2", (1, 0, 1, 2, 0, 0)),
            ("2", (2, 0, 0, 0, 0, 0)),
        ]
        for raw, expected in cases:
            assert updater.parse_version(raw) == expected, raw

    def test_strips_prefix_and_whitespace(self):
        assert updater.parse_version("v1.2.3") == updater.parse_version("1.2.3")
        assert updater.parse_version("V1.2.3-beta1") == updater.parse_version(
            "1.2.3-beta1"
        )
        assert updater.parse_version(" 1.2.3 ") == updater.parse_version("1.2.3")

    def test_pre_release_suffix_number(self):
        assert updater.parse_version("1.0.1-beta0") == (1, 0, 1, 0, -1, 0)
        assert updater.parse_version("1.0.1-beta1") == (1, 0, 1, 0, -1, 1)
        # A bare suffix (no number) counts as zero.
        assert updater.parse_version("1.0.1-beta") == (1, 0, 1, 0, -1, 0)

    def test_attached_and_dotted_suffix_compare_equal(self):
        assert updater.parse_version("1.0.1-beta.2") == updater.parse_version(
            "1.0.1-beta2"
        )
        # Only the suffix NUMBER participates in ordering, not its label.
        assert updater.parse_version("1.0.1-rc1") == updater.parse_version(
            "1.0.1-beta1"
        )

    def test_ordering_table(self):
        cases = [
            ("1.0.1", "1.0"),
            ("1.0.1.2", "1.0.1"),
            ("1.0.2", "1.0.1-rc9"),
            ("1.0.1-beta1", "1.0.1-beta0"),
            ("1.0.2-beta1", "1.0.1-beta1"),
        ]
        for newer, older in cases:
            assert updater.parse_version(newer) > updater.parse_version(older), (
                f"{newer} should sort above {older}"
            )
        # Every pre-release sorts below its stable release.
        assert updater.parse_version("1.0.1-beta1") < updater.parse_version("1.0.1")
        assert updater.parse_version("1.0.1-rc9") < updater.parse_version("1.0.1")

    def test_garbage_does_not_raise(self):
        # Non-version input must never crash the update check thread.
        assert updater.parse_version("") is not None
        assert updater.parse_version("abc") is not None


class TestCheckFlow:
    @pytest.fixture(autouse=True)
    def _sync_threads(self, monkeypatch):
        monkeypatch.setattr(updater.threading, "Thread", _SyncThread)

    def test_skips_when_not_due_without_network(self, monkeypatch):
        calls = []
        monkeypatch.setattr(updater, "read_interval", lambda: "24h")
        monkeypatch.setattr(updater, "_last_check", time.time)

        def _fail(*a, **k):
            raise AssertionError("network must not be touched")

        monkeypatch.setattr(updater, "_stamp_last_check", _fail)
        monkeypatch.setattr(updater.requests, "get", _fail)

        updater.check(lambda *args: calls.append(args))
        assert calls == [(False, None, None, None, None)]

    def test_runs_when_due_and_stamps(self, monkeypatch):
        calls = []
        stamps = []
        monkeypatch.setattr(updater, "read_interval", lambda: "24h")
        monkeypatch.setattr(updater, "_last_check", lambda: 0.0)
        monkeypatch.setattr(
            updater, "_stamp_last_check", lambda: stamps.append(True)
        )

        def _boom(*a, **k):
            raise updater.requests.RequestException("boom")

        monkeypatch.setattr(updater.requests, "get", _boom)

        updater.check(lambda *args: calls.append(args))
        assert stamps == [True]
        assert len(calls) == 1
        available, _, _, _, error = calls[0]
        assert available is False
        assert error is not None

    def test_force_bypasses_interval_entirely(self, monkeypatch):
        calls = []

        def _forbidden():
            raise AssertionError("interval must not be read when forcing")

        monkeypatch.setattr(updater, "read_interval", _forbidden)
        monkeypatch.setattr(updater, "_stamp_last_check", lambda: None)

        def _boom(*a, **k):
            raise updater.requests.RequestException("boom")

        monkeypatch.setattr(updater.requests, "get", _boom)

        updater.check(lambda *args: calls.append(args), force=True)
        assert len(calls) == 1
        assert calls[0][4] is not None