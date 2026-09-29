"""Unit tests for services/credential_store.py - the OS-keyring mirror.

Every test swaps in a hermetic in-memory backend (``fake_keyring_backend``)
or the library's fail backend (``fail_keyring_backend``) via the conftest
fixtures, so the suite never depends on a secret-service daemon existing
(CI and this dev machine have none - the real backend resolves to the
fail backend there, which is exactly the file-only degradation path).

Overview of the contract under test:
* files stay the plugin-facing source of truth; the keyring is a mirror;
* mirror() copies a freshly written file (or an explicit payload);
* restore() re-materializes only MISSING files (an on-disk file wins -
  it may have been refreshed by a plugin since the last mirror);
* delete() clears the keyring copy even when mirroring is turned off
  (logout/uninstall must never leave a stale mirror behind);
* the profile lifecycle is mirrored too: delete_profile() sweeps a dead
  profile's entries, rename_profile() moves them to the new account
  prefix (an account name is "<profile>:<file>", so leaving one behind
  strands a live credential the renamed profile can no longer reach);
* only usable OS backends are trusted - the fail backend and the whole
  keyrings.alt package count as unavailable.
"""

import logging
import os
import sys
from pathlib import Path

import pytest

from services import credential_store

_nt = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


@pytest.fixture(autouse=True)
def _isolated_auth_dir():
    """Fresh auth dir before each test (the conftest sandbox is process-wide)."""
    auth_dir = credential_store._profile_store.auth_dir()
    if auth_dir.exists():
        for p in auth_dir.iterdir():
            p.unlink()
    yield


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_auth_file(name: str, payload: str) -> Path:
    """Place *payload* in the (sandboxed) auth dir as *name*, 0600."""
    p = credential_store.auth_file(name)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.write_text(payload, encoding="utf-8")
    os.chmod(p, 0o600)
    return p


def _seed(fake, name: str, payload: str) -> None:
    """Plant *payload* directly in the fake keyring for *name*."""
    fake.set_password(
        credential_store.KEYRING_SERVICE,
        credential_store._account(name),
        payload,
    )


def _peek(fake, name: str):
    return fake.get_password(
        credential_store.KEYRING_SERVICE,
        credential_store._account(name),
    )


def _disable_mirroring(monkeypatch) -> None:
    monkeypatch.setattr(credential_store, "mirroring_enabled", lambda: False)


# ---------------------------------------------------------------------------
# backend probing
# ---------------------------------------------------------------------------


def test_keyring_available_with_useful_backend(fake_keyring_backend):
    # The probe is a functional set/get/delete round trip, cached.
    assert credential_store.keyring_available() is True
    assert credential_store.keyring_available() is True  # cached now
    assert fake_keyring_backend.set_calls == 1  # no second probe write


def test_keyring_unavailable_with_fail_backend(fail_keyring_backend):
    assert credential_store.keyring_available() is False


class _AltBackend:
    """Impersonates a keyrings.alt backend (plaintext - must be vetoed)."""

    def set_password(self, *a):
        raise AssertionError("must not be used")

    def get_password(self, *a):
        raise AssertionError("must not be used")

    def delete_password(self, *a):
        raise AssertionError("must not be used")


_AltBackend.__module__ = "keyrings.alt.file"


def test_keyrings_alt_backend_vetoed(monkeypatch):
    monkeypatch.setattr(credential_store, "_backend", lambda: _AltBackend())
    monkeypatch.setattr(credential_store, "_available", None)
    assert credential_store.keyring_available() is False


def test_raising_backend_probe_degrades(fake_keyring_backend, monkeypatch):
    def _boom():
        raise RuntimeError("keyring backend error")

    monkeypatch.setattr(credential_store, "_backend", _boom)
    monkeypatch.setattr(credential_store, "_available", None)
    assert credential_store.keyring_available() is False


def test_missing_package_is_reported_as_missing_not_unusable(monkeypatch):
    # `keyring` is an optional dependency: an absent package must be told
    # apart from an absent daemon, or the log tells the user to start a
    # secret-service daemon they may already be running.
    def _no_module():
        raise ImportError("No module named 'keyring'")

    monkeypatch.setattr(credential_store, "_backend", _no_module)
    monkeypatch.setattr(credential_store, "_available", None)
    assert credential_store.keyring_available() is False
    assert credential_store._reason == "missing"


def test_backend_failure_is_reported_as_unusable(monkeypatch):
    # The converse: a broken backend is a daemon problem, not a package one.
    def _boom():
        raise RuntimeError("no secret service")

    monkeypatch.setattr(credential_store, "_backend", _boom)
    monkeypatch.setattr(credential_store, "_available", None)
    assert credential_store.keyring_available() is False
    assert credential_store._reason == "unusable"


def test_probe_failure_hints_name_the_right_remedy(monkeypatch, caplog):
    # The two reasons must not share text: "missing" names the package,
    # "unusable" names the daemon.  A user with keyring installed and no
    # daemon must not be told to install the package, and vice versa.
    def _raises(exc):
        def _boom():
            raise exc

        return _boom

    cases = (
        (ImportError("No module named 'keyring'"), "PlaylistManager[keyring]"),
        (RuntimeError("no secret service"), "gnome-keyring"),
    )
    for exc, expected in cases:
        monkeypatch.setattr(credential_store, "_backend", _raises(exc))
        monkeypatch.setattr(credential_store, "_available", None)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert credential_store.keyring_available() is False
        assert expected in caplog.text


# ---------------------------------------------------------------------------
# mirror()
# ---------------------------------------------------------------------------


def test_mirror_stores_file_payload(fake_keyring_backend):
    payload = '{"client_id": "id", "client_secret": "s"}'
    _write_auth_file("spotify.json", payload)
    assert credential_store.mirror("spotify.json") is True
    assert _peek(fake_keyring_backend, "spotify.json") == payload


def test_mirror_accepts_explicit_payload(fake_keyring_backend):
    # Used when the caller holds the exact bytes just written; no file
    # on disk is needed.
    assert credential_store.mirror("yt_music.json", payload='{"a": 1}') is True
    assert _peek(fake_keyring_backend, "yt_music.json") == '{"a": 1}'


def test_mirror_skips_identical_payload(fake_keyring_backend):
    payload = '{"x": 1}'
    credential_store.keyring_available()  # warm the probe before counting
    _seed(fake_keyring_backend, "spotify.json", payload)
    calls_before = fake_keyring_backend.set_calls
    assert credential_store.mirror("spotify.json", payload=payload) is True
    assert fake_keyring_backend.set_calls == calls_before  # self-quiescing


def test_mirror_noop_when_toggle_off(fake_keyring_backend, monkeypatch):
    _disable_mirroring(monkeypatch)
    _write_auth_file("spotify.json", '{"a": 1}')
    assert credential_store.mirror("spotify.json") is False
    assert _peek(fake_keyring_backend, "spotify.json") is None


def test_mirror_noop_without_backend(fail_keyring_backend):
    _write_auth_file("spotify.json", '{"a": 1}')
    assert credential_store.mirror("spotify.json") is False
    assert credential_store.mirror("spotify.json", payload="{}") is False


def test_mirror_missing_file_noop(fake_keyring_backend):
    # No file, no explicit payload -> the read-back finds nothing.
    assert credential_store.mirror("spotify.json") is False
    assert _peek(fake_keyring_backend, "spotify.json") is None


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------


def test_restore_writes_missing_file(fake_keyring_backend):
    payload = '{"client_id": "id", "client_secret": "s", "refresh_token": "r"}'
    _seed(fake_keyring_backend, "spotify.json", payload)
    path = credential_store.auth_file("spotify.json")
    assert not path.exists()
    assert credential_store.restore("spotify.json") is True
    assert path.read_text(encoding="utf-8") == payload


@_nt
def test_restore_writes_0600(fake_keyring_backend):
    _seed(fake_keyring_backend, "spotify.json", "{}")
    credential_store.restore("spotify.json")
    assert (credential_store.auth_file("spotify.json").stat().st_mode & 0o777) == 0o600


@_nt
def test_restore_ignores_stale_loose_tmp_file(fake_keyring_backend):
    # Regression: restore() used to write the payload through a predictable
    # "<name>.tmp" opened with O_CREAT.  O_CREAT applies 0600 only when it
    # CREATES the file, so a pre-existing loose tmp (crash remnant, another
    # tool) was written through with its old mode - and os.replace carried
    # that mode onto the final credential file, publishing the secret
    # world/group-readable.
    secret = '{"client_secret": "SECRET"}'
    _seed(fake_keyring_backend, "spotify.json", secret)
    path = credential_store.auth_file("spotify.json")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stale = path.parent / "spotify.json.tmp"
    stale.write_text("crash remnant", encoding="utf-8")
    os.chmod(stale, 0o644)

    assert credential_store.restore("spotify.json") is True

    assert path.read_text(encoding="utf-8") == secret
    assert (path.stat().st_mode & 0o777) == 0o600
    # The unrelated loose file is neither clobbered nor reused.
    assert stale.read_text(encoding="utf-8") == "crash remnant"


def test_restore_leaves_no_tmp_residue(fake_keyring_backend):
    _seed(fake_keyring_backend, "spotify.json", "{}")
    credential_store.restore("spotify.json")
    assert [
        p.name for p in credential_store.auth_file("spotify.json").parent.iterdir()
    ] == ["spotify.json"]


def test_restore_noop_when_file_exists(fake_keyring_backend):
    _seed(fake_keyring_backend, "spotify.json", '"keyring copy"')
    path = _write_auth_file("spotify.json", '"on-disk copy (fresh)"')
    assert credential_store.restore("spotify.json") is False
    # On-disk wins - a plugin may have refreshed tokens since the mirror.
    assert path.read_text(encoding="utf-8") == '"on-disk copy (fresh)"'


def test_restore_noop_without_payload(fake_keyring_backend):
    assert credential_store.restore("spotify.json") is False


def test_restore_noop_when_toggle_off(fake_keyring_backend, monkeypatch):
    _disable_mirroring(monkeypatch)
    _seed(fake_keyring_backend, "spotify.json", "{}")
    assert credential_store.restore("spotify.json") is False
    assert not credential_store.auth_file("spotify.json").exists()


def test_restore_noop_without_backend(fail_keyring_backend):
    assert credential_store.restore("spotify.json") is False


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


def test_delete_removes_entry(fake_keyring_backend):
    _seed(fake_keyring_backend, "spotify.json", "{}")
    credential_store.delete("spotify.json")
    assert _peek(fake_keyring_backend, "spotify.json") is None


def test_delete_missing_entry_noop(fake_keyring_backend):
    credential_store.delete("spotify.json")  # must not raise


def test_delete_runs_even_when_toggle_off(fake_keyring_backend, monkeypatch):
    # Cleanup contract: logout/uninstall clears a mirror left by a
    # previously-enabled session even after the toggle is off.
    _disable_mirroring(monkeypatch)
    _seed(fake_keyring_backend, "spotify.json", "{}")
    credential_store.delete("spotify.json")
    assert _peek(fake_keyring_backend, "spotify.json") is None


def test_delete_noop_without_backend(fail_keyring_backend):
    credential_store.delete("spotify.json")  # must not raise


# ---------------------------------------------------------------------------
# profile sweeps: delete_profile() / rename_profile()
# ---------------------------------------------------------------------------


def _seed_for(fake, profile: str, name: str, payload: str) -> None:
    """Plant *payload* under an explicit profile prefix (not the active one)."""
    fake.set_password(
        credential_store.KEYRING_SERVICE,
        credential_store._account(name, profile),
        payload,
    )


def _peek_for(fake, profile: str, name: str):
    return fake.get_password(
        credential_store.KEYRING_SERVICE,
        credential_store._account(name, profile),
    )


def test_account_isolates_profiles():
    assert credential_store._account("spotify.json", "work") == "work:spotify.json"
    # No explicit profile -> the active one (active_profile() is "default"
    # in the conftest sandbox).
    assert credential_store._account("spotify.json") == "default:spotify.json"


def test_delete_profile_clears_only_that_profile(fake_keyring_backend):
    _seed_for(fake_keyring_backend, "work", "spotify.json", '{"t": "work"}')
    _seed_for(fake_keyring_backend, "work", "deezer.json", '{"arl": "work"}')
    _seed_for(fake_keyring_backend, "default", "spotify.json", '{"t": "default"}')

    credential_store.delete_profile("work")

    assert _peek_for(fake_keyring_backend, "work", "spotify.json") is None
    assert _peek_for(fake_keyring_backend, "work", "deezer.json") is None
    # The active profile's credentials are untouched - the sweep must
    # never resolve its target into the active account prefix.
    assert _peek_for(fake_keyring_backend, "default", "spotify.json") == '{"t": "default"}'


def test_delete_profile_runs_even_when_toggle_off(fake_keyring_backend, monkeypatch):
    _disable_mirroring(monkeypatch)
    _seed_for(fake_keyring_backend, "work", "spotify.json", "{}")
    credential_store.delete_profile("work")
    assert _peek_for(fake_keyring_backend, "work", "spotify.json") is None


def test_delete_profile_noop_without_backend(fail_keyring_backend):
    credential_store.delete_profile("work")  # must not raise


def test_rename_profile_moves_entries(fake_keyring_backend):
    _seed_for(fake_keyring_backend, "work", "spotify.json", '{"t": "secret"}')
    _seed_for(fake_keyring_backend, "work", "deezer.json", '{"arl": "secret"}')
    _seed_for(fake_keyring_backend, "default", "spotify.json", '{"t": "default"}')

    credential_store.rename_profile("work", "office")

    assert _peek_for(fake_keyring_backend, "office", "spotify.json") == '{"t": "secret"}'
    assert _peek_for(fake_keyring_backend, "office", "deezer.json") == '{"arl": "secret"}'
    # The old prefix must be empty: a stranded entry is both unreachable
    # from the renamed profile and a lingering live credential.
    assert _peek_for(fake_keyring_backend, "work", "spotify.json") is None
    assert _peek_for(fake_keyring_backend, "work", "deezer.json") is None
    assert _peek_for(fake_keyring_backend, "default", "spotify.json") == '{"t": "default"}'


def test_rename_profile_keeps_old_entry_when_write_fails(fake_keyring_backend, monkeypatch):
    # Copy-then-delete: a failed write to the new prefix must leave the
    # only copy in place rather than dropping the credential.  The write
    # failure is modelled at the BACKEND (what really happens when the
    # secret-service daemon dies mid-sweep), not at the primitive.
    _seed_for(fake_keyring_backend, "work", "spotify.json", '{"t": "secret"}')

    class _WritesFail:
        """Reads delegate to the fake; every set_password raises."""

        def __init__(self, fake):
            self._fake = fake

        def get_password(self, service, account):
            return self._fake.get_password(service, account)

        def set_password(self, service, account, password):
            raise RuntimeError("secret-service daemon died")

        def delete_password(self, service, account):
            raise RuntimeError("secret-service daemon died")

    monkeypatch.setattr(credential_store, "_backend", lambda: _WritesFail(fake_keyring_backend))
    credential_store.rename_profile("work", "office")

    monkeypatch.undo()
    assert _peek_for(fake_keyring_backend, "work", "spotify.json") == '{"t": "secret"}'
    assert _peek_for(fake_keyring_backend, "office", "spotify.json") is None


def test_rename_profile_same_name_is_noop(fake_keyring_backend):
    _seed_for(fake_keyring_backend, "work", "spotify.json", '{"t": "secret"}')
    credential_store.rename_profile("work", "work")
    assert _peek_for(fake_keyring_backend, "work", "spotify.json") == '{"t": "secret"}'


def test_rename_profile_no_entries_is_noop(fake_keyring_backend):
    credential_store.rename_profile("work", "office")  # must not raise


def test_profile_sweeps_never_raise(fail_keyring_backend):
    credential_store.delete_profile("work")
    credential_store.rename_profile("work", "office")


# ---------------------------------------------------------------------------
# path validation
# ---------------------------------------------------------------------------


def test_auth_file_accepts_bare_filename():
    assert credential_store.auth_file("spotify.json").name == "spotify.json"


@pytest.mark.parametrize("bad", ["../spotify.json", "a/b.json", "x/../y.json", ""])
def test_auth_file_rejects_traversal(bad):
    with pytest.raises(ValueError):
        credential_store.auth_file(bad)


# ---------------------------------------------------------------------------
# restore_all()
# ---------------------------------------------------------------------------


def test_restore_all_materializes_missing_files(fake_keyring_backend):
    _seed(fake_keyring_backend, "spotify.json", '{"s": true}')
    _seed(fake_keyring_backend, "browser.json", '{"cookie": "c"}')
    credential_store.restore_all()
    assert credential_store.auth_file("spotify.json").read_text() == '{"s": true}'
    assert credential_store.auth_file("browser.json").read_text() == '{"cookie": "c"}'


def test_restore_all_never_raises(fail_keyring_backend):
    credential_store.restore_all()


def test_restore_all_leaves_existing_files_alone(fake_keyring_backend):
    _seed(fake_keyring_backend, "spotify.json", '"keyring"')
    path = _write_auth_file("spotify.json", '"on disk"')
    credential_store.restore_all()
    assert path.read_text() == '"on disk"'