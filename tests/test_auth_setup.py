"""Unit tests for services/auth_setup.py - credential path handling.

The credential-file lifecycle (save/load/delete/verify) needs live
plugins and is exercised through the plugin repos; this file covers the
pure, headless-safe parts: the path/status inspection used by the
Settings credentials panel.

File-permission semantics are POSIX; the 0600/0700 policy the panel
surfaces makes no sense on Windows, so the mode-dependent cases are
skipped there.
"""

import os
import sys
from pathlib import Path

import pytest

from services import auth_setup

_nt = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


@pytest.fixture(autouse=True)
def _isolated_auth_dir():
    """Fresh auth dir before each test (the conftest sandbox is process-wide)."""
    auth_dir = auth_setup.AUTH_DIR
    if auth_dir.exists():
        for p in auth_dir.iterdir():
            p.unlink()
    yield


def test_missing_path_reports_not_exists(tmp_path):
    status = auth_setup.credential_path_status(tmp_path / "nope.json")
    assert status == {"exists": False, "mode": None, "private": False}


@_nt
def test_private_file_0600(tmp_path):
    p = tmp_path / "spotify.json"
    p.write_text("{}")
    os.chmod(p, 0o600)
    status = auth_setup.credential_path_status(p)
    assert status["exists"] is True
    assert status["mode"] == "0600"
    assert status["private"] is True


@_nt
def test_loose_file_flagged(tmp_path):
    p = tmp_path / "browser.json"
    p.write_text("{}")
    os.chmod(p, 0o644)
    status = auth_setup.credential_path_status(p)
    assert status["exists"] is True
    assert status["mode"] == "0644"
    assert status["private"] is False


@_nt
def test_private_dir_0700(tmp_path):
    d = tmp_path / "auth"
    d.mkdir(mode=0o700)
    status = auth_setup.credential_path_status(d)
    assert status["exists"] is True
    assert status["mode"] == "0700"
    assert status["private"] is True


@_nt
def test_loose_dir_flagged(tmp_path):
    d = tmp_path / "auth"
    d.mkdir(mode=0o755)
    status = auth_setup.credential_path_status(d)
    assert status["mode"] == "0755"
    assert status["private"] is False


def test_unreadable_path_reports_not_exists(monkeypatch, tmp_path):
    # Any OSError from stat() (permission denied, broken symlink) must
    # degrade to "missing" - the panel helper never raises.
    def _boom(_self):
        raise OSError("access denied")

    monkeypatch.setattr(Path, "stat", _boom)
    status = auth_setup.credential_path_status(tmp_path / "x.json")
    assert status == {"exists": False, "mode": None, "private": False}


def test_platform_credential_map_has_all_known_platforms():
    # The hardcoded map drives the registry-less fallback listing in the
    # Settings credentials panel; every platform that carries credentials
    # must appear, or its file would be invisible without a live registry.
    for pid in ("youtube_music", "spotify", "lastfm", "soundcloud", "deezer"):
        assert pid in auth_setup.PLATFORM_CREDENTIAL_FILES
        assert auth_setup.PLATFORM_CREDENTIAL_FILES[pid]


# ---------------------------------------------------------------------------
# OS-keyring mirror wiring (services/credential_store.py)
# ---------------------------------------------------------------------------

_SPOTIFY_PAYLOAD = (
    '{"client_id": "id", "client_secret": "s3cret", "refresh_token": "r3fr3sh"}'
)


def _seed_keyring(fake, name: str, payload: str) -> None:
    from services import credential_store

    fake.set_password(
        credential_store.KEYRING_SERVICE,
        credential_store._account(name),
        payload,
    )


def _peek_keyring(fake, name: str):
    from services import credential_store

    return fake.get_password(
        credential_store.KEYRING_SERVICE,
        credential_store._account(name),
    )


def test_delete_platform_credentials_clears_keyring(fake_keyring_backend):
    auth_setup.SPOTIFY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth_setup.SPOTIFY_FILE.write_text(_SPOTIFY_PAYLOAD)
    _seed_keyring(fake_keyring_backend, "spotify.json", _SPOTIFY_PAYLOAD)

    deleted, missing = auth_setup.delete_platform_credentials("spotify")

    assert auth_setup.SPOTIFY_FILE in deleted
    assert missing == []
    assert not auth_setup.SPOTIFY_FILE.exists()
    assert _peek_keyring(fake_keyring_backend, "spotify.json") is None


def test_delete_spotify_credentials_clears_keyring(fake_keyring_backend):
    auth_setup.SPOTIFY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth_setup.SPOTIFY_FILE.write_text(_SPOTIFY_PAYLOAD)
    _seed_keyring(fake_keyring_backend, "spotify.json", _SPOTIFY_PAYLOAD)

    assert auth_setup.delete_spotify_credentials() is True

    assert not auth_setup.SPOTIFY_FILE.exists()
    assert _peek_keyring(fake_keyring_backend, "spotify.json") is None


def test_load_spotify_credentials_restores_from_keyring(fake_keyring_backend):
    # Auth-dir loss: the file is gone but the keyring mirror survives -
    # load re-materializes it instead of returning empty.
    auth_setup.SPOTIFY_FILE.unlink(missing_ok=True)
    _seed_keyring(fake_keyring_backend, "spotify.json", _SPOTIFY_PAYLOAD)

    creds = auth_setup.load_spotify_credentials()

    assert creds.get("client_id") == "id"
    assert auth_setup.SPOTIFY_FILE.exists()
    assert auth_setup.SPOTIFY_FILE.read_text(encoding="utf-8") == _SPOTIFY_PAYLOAD


def test_load_spotify_credentials_without_mirror_stays_empty(fake_keyring_backend):
    auth_setup.SPOTIFY_FILE.unlink(missing_ok=True)
    assert auth_setup.load_spotify_credentials() == {}


def test_complete_browser_file_mirrors_on_observation(fake_keyring_backend):
    # The ytmusicapi terminal flow writes browser.json outside the app -
    # first observation of a complete file is the mirror point.
    payload = '{"cookie": "c", "access_token": "t", "X-Goog-AuthUser": "0"}'
    auth_setup.BROWSER_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth_setup.BROWSER_FILE.write_text(payload)

    assert auth_setup.complete_browser_file(auth_setup.BROWSER_FILE) is True

    assert _peek_keyring(fake_keyring_backend, "browser.json") == payload


def test_incomplete_browser_file_not_mirrored(fake_keyring_backend):
    # A headers file without the auth cookie is not consumable and must
    # not be mirrored (mirrors ytmusicapi's own refusal to write it).
    auth_setup.BROWSER_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth_setup.BROWSER_FILE.write_text('{"X-Goog-AuthUser": "0"}')

    assert auth_setup.complete_browser_file(auth_setup.BROWSER_FILE) is False

    assert _peek_keyring(fake_keyring_backend, "browser.json") is None


# ---------------------------------------------------------------------------
# Credential-permission audit (startup check + Settings panel warning)
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Minimal duck-typed PluginRegistry for the audit's two calls."""

    def __init__(self, plugins=None):
        self._plugins = dict(plugins or {})

    def get(self, pid):
        return self._plugins.get(pid)

    def get_all(self):
        return dict(self._plugins)


class _FakePlugin:
    def __init__(self, auth_paths=()):
        self.auth_paths = list(auth_paths)


@pytest.fixture
def _restore_auth_dir_mode():
    """The tests below loosen the shared sandbox auth dir; put it back."""
    auth_dir = auth_setup.AUTH_DIR
    auth_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(auth_dir, 0o700)
    yield auth_dir
    os.chmod(auth_dir, 0o700)


def _write(path, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    os.chmod(path, mode)
    return path


@_nt
def test_audit_flags_loose_auth_dir_and_file(_restore_auth_dir_mode):
    auth_dir = _restore_auth_dir_mode
    os.chmod(auth_dir, 0o755)
    _write(auth_setup.SPOTIFY_FILE, 0o644)

    report = auth_setup.audit_credential_permissions()

    found = {f.path: (f.actual, f.expected) for f in report.findings}
    assert found == {
        auth_dir: (0o755, 0o700),
        auth_setup.SPOTIFY_FILE: (0o644, 0o600),
    }
    assert report.ok is False


@_nt
def test_audit_clean_tree_reports_ok(_restore_auth_dir_mode):
    _write(auth_setup.SPOTIFY_FILE, 0o600)

    report = auth_setup.audit_credential_permissions()

    assert report.ok is True
    assert report.checked >= 1  # the auth dir + spotify.json
    assert report.first_path is None


@_nt
def test_audit_ignores_missing_credential_files(_restore_auth_dir_mode):
    # No credentials at all (a fresh install) is not a regression.
    report = auth_setup.audit_credential_permissions()
    assert report.ok is True
    assert report.findings == ()


@_nt
def test_audit_covers_manifest_fallback_paths(_restore_auth_dir_mode, tmp_path):
    # A legacy repo-root copy (auth_file_fallbacks) is still a live
    # credential - logout deletes it, so the audit must inspect it.
    fallback = _write(tmp_path / "browser.json", 0o666)
    registry = _FakeRegistry({"youtube_music": _FakePlugin([fallback])})

    report = auth_setup.audit_credential_permissions(registry)

    assert [(f.path, f.actual) for f in report.findings] == [(fallback, 0o666)]


@_nt
def test_audit_covers_third_party_platform_paths(_restore_auth_dir_mode, tmp_path):
    # A platform that is not in the hardcoded map is audited from its
    # manifest alone.
    other = _write(tmp_path / "listenbrainz.json", 0o604)
    registry = _FakeRegistry({"listenbrainz": _FakePlugin([other])})

    report = auth_setup.audit_credential_permissions(registry)

    assert [f.path for f in report.findings] == [other]


@_nt
def test_audit_does_not_double_count_shared_paths(
    _restore_auth_dir_mode, tmp_path
):
    # A path both in the hardcoded map and in the manifest is one path.
    _write(auth_setup.SPOTIFY_FILE, 0o644)
    registry = _FakeRegistry({"spotify": _FakePlugin([auth_setup.SPOTIFY_FILE])})

    report = auth_setup.audit_credential_permissions(registry)

    assert [f.path for f in report.findings] == [auth_setup.SPOTIFY_FILE]


@_nt
def test_audit_survives_a_broken_registry(_restore_auth_dir_mode, tmp_path):
    # A plugin whose manifest cannot be read must not void the audit of
    # everything else - the auth dir is still checked.
    os.chmod(_restore_auth_dir_mode, 0o755)

    class _Broken:
        def get(self, pid):
            raise RuntimeError("bad manifest")

        def get_all(self):
            raise RuntimeError("bad manifest")

    report = auth_setup.audit_credential_permissions(_Broken())

    assert [f.path for f in report.findings] == [_restore_auth_dir_mode]


@_nt
def test_audit_survives_a_plugin_without_auth_paths(_restore_auth_dir_mode):
    class _NoPaths:
        auth_paths = []

    report = auth_setup.audit_credential_permissions(
        _FakeRegistry({"x": _NoPaths()})
    )
    assert report.ok is True


@_nt
def test_audit_reports_nothing_without_posix_modes(
    _restore_auth_dir_mode, monkeypatch
):
    # Windows mode bits are meaningless; the audit must stay silent there
    # instead of warning on every launch.
    os.chmod(_restore_auth_dir_mode, 0o777)
    from services import health_check

    monkeypatch.setattr(health_check, "supports_posix_modes", lambda: False)

    report = auth_setup.audit_credential_permissions()

    assert report.supported is False
    assert report.ok is True
    assert report.findings == ()