"""Unit tests for services/profile_store.py - profile metadata and paths.

All state and file constants are sandboxed: ``ACTIVE_JSON``,
``PROFILES_JSON``, ``_DEFAULT_DB_DIR``, ``_DEFAULT_CFG_DIR`` and
``_AUTH_ROOT`` are re-pointed at a fresh tmp_path per test, and the
module's in-memory caches (``_active``, ``_profiles_data``) are reset so
profile CRUD never touches the real ``db/``, ``cfg/`` or platformdirs
auth tree.
"""

import pytest

from services import profile_store as ps


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    root = tmp_path
    monkeypatch.setattr(ps, "ACTIVE_JSON", root / "cfg" / "profile.json")
    monkeypatch.setattr(ps, "PROFILES_JSON", root / "db" / "profiles.json")
    monkeypatch.setattr(ps, "_DEFAULT_DB_DIR", root / "db")
    monkeypatch.setattr(ps, "_DEFAULT_CFG_DIR", root / "cfg")
    monkeypatch.setattr(ps, "_AUTH_ROOT", root / "auth")
    # Legacy migration source: point at empty sandbox paths so
    # initialize()'s one-time migration never touches the real repo db/.
    monkeypatch.setattr(ps, "_LEGACY_DB_DIR", root / "legacy_db")
    monkeypatch.setattr(ps, "_LEGACY_CFG_DIR", root / "legacy_cfg")
    monkeypatch.setattr(ps, "_active", "")
    monkeypatch.setattr(ps, "_profiles_data", None)
    ps.initialize()
    yield ps


class TestInitialize:
    def test_default_profile_seeded(self, sandbox):
        assert sandbox.active_profile() == "default"
        assert sandbox.list_profiles() == ["default"]

    def test_all_buckets_off_by_default(self, sandbox):
        assert sandbox.get_bucket("default", "playlists") is False
        assert sandbox.get_bucket("default", "settings") is False
        assert sandbox.get_bucket("default", "logins") is False


class TestCreate:
    def test_create_and_list(self, sandbox):
        sandbox.create("work", logins=False, playlists=False, settings=False)
        assert sandbox.list_profiles() == ["default", "work"]

    def test_create_duplicate_raises(self, sandbox):
        sandbox.create("work", logins=False, playlists=False, settings=False)
        with pytest.raises(ValueError):
            sandbox.create("work", logins=False, playlists=False, settings=False)

    def test_empty_name_raises(self, sandbox):
        with pytest.raises(ValueError):
            sandbox.create("", logins=False, playlists=False, settings=False)

    def test_illegal_name_raises(self, sandbox):
        for bad in ("space name", "slash/name", ".hidden", "?"):
            with pytest.raises(ValueError):
                sandbox.create(bad, logins=False, playlists=False, settings=False)

    def test_name_validation_rules(self, sandbox):
        # Valid chars: alphanumeric, underscore, hyphen, must start
        # with a letter/digit.
        assert ps._NAME_RE.match("work-2") is not None
        assert ps._NAME_RE.match("_work") is None
        assert ps._NAME_RE.match("-work") is None

    def test_create_copies_playlists_bucket(self, sandbox):
        db_dir = sandbox._DEFAULT_DB_DIR
        db_dir.mkdir(parents=True, exist_ok=True)
        (db_dir / "playlists.json").write_text("[]", encoding="utf-8")
        sandbox.create("work", logins=False, playlists=True, settings=False)
        assert (db_dir / "profiles" / "work" / "playlists.json").exists()


class TestSetActive:
    def test_set_active_persists(self, sandbox):
        sandbox.create("work", logins=False, playlists=False, settings=False)
        sandbox.set_active("work")
        # Persisted to disk - re-initialize reads it back.
        sandbox._active = ""
        sandbox.initialize()
        assert sandbox.active_profile() == "work"

    def test_set_active_unknown_raises(self, sandbox):
        with pytest.raises(ValueError):
            sandbox.set_active("does_not_exist")


class TestPathResolution:
    def test_shared_dirs_when_buckets_off(self, sandbox):
        assert sandbox.db_dir() == sandbox._DEFAULT_DB_DIR
        assert sandbox.cfg_dir() == sandbox._DEFAULT_CFG_DIR
        assert sandbox.auth_dir() == sandbox._AUTH_ROOT

    def test_profile_dirs_when_buckets_on(self, sandbox):
        sandbox.create("work", logins=True, playlists=True, settings=True)
        sandbox.set_active("work")
        assert sandbox.db_dir() == sandbox._DEFAULT_DB_DIR / "profiles" / "work"
        assert sandbox.cfg_dir() == sandbox._DEFAULT_CFG_DIR / "profiles" / "work"
        assert sandbox.auth_dir() == sandbox._AUTH_ROOT / "work"

    def test_global_dirs_always_shared(self, sandbox):
        sandbox.create("work", logins=True, playlists=True, settings=True)
        assert sandbox.global_db_dir() == sandbox._DEFAULT_DB_DIR
        assert sandbox.global_cfg_dir() == sandbox._DEFAULT_CFG_DIR
        assert sandbox.global_auth_dir() == sandbox._AUTH_ROOT


class TestSetBucket:
    def test_toggle_playlists_copies_shared(self, sandbox):
        db_dir = sandbox._DEFAULT_DB_DIR
        db_dir.mkdir(parents=True, exist_ok=True)
        (db_dir / "playlists.json").write_text("[]", encoding="utf-8")
        sandbox.set_bucket("default", "playlists", True)
        assert (db_dir / "profiles" / "default" / "playlists.json").exists()

    def test_toggle_off_keeps_data_on_disk(self, sandbox):
        sandbox.set_bucket("default", "playlists", True)
        sandbox.set_bucket("default", "playlists", False)
        assert sandbox.get_bucket("default", "playlists") is False

    def test_unknown_bucket_raises(self, sandbox):
        with pytest.raises(ValueError):
            sandbox.set_bucket("default", "nope", True)


class TestRenameDelete:
    def test_rename_switches_pointer_if_active(self, sandbox):
        sandbox.create("work", logins=False, playlists=False, settings=False)
        sandbox.set_active("work")
        sandbox.rename("work", "work2")
        assert sandbox.list_profiles() == ["default", "work2"]
        assert sandbox.active_profile() == "work2"

    def test_rename_default_raises(self, sandbox):
        with pytest.raises(ValueError):
            sandbox.rename("default", "something")

    def test_delete_default_raises(self, sandbox):
        with pytest.raises(ValueError):
            sandbox.delete("default")

    def test_delete_missing_raises(self, sandbox):
        with pytest.raises(ValueError):
            sandbox.delete("ghost")

    def test_delete_active_raises(self, sandbox):
        sandbox.create("work", logins=False, playlists=False, settings=False)
        sandbox.set_active("work")
        with pytest.raises(ValueError):
            sandbox.delete("work")

    def test_delete_inactive_succeeds(self, sandbox):
        sandbox.create("work", logins=False, playlists=False, settings=False)
        sandbox.delete("work")
        assert sandbox.list_profiles() == ["default"]


class TestKeyringMirrorFollowsProfile:
    """The OS-keyring mirror (services/credential_store.py) is keyed
    ``"<profile>:<file>"``, so the profile lifecycle has to carry it:

    * deleting a profile purges its auth dir but NOT the keyring - a
      sweep is the only thing standing between a deleted profile and
      live credentials (refresh tokens, ARL cookies) that outlive it;
    * renaming a profile moves the auth dir; without moving the mirror
      too, the new profile resolves an empty prefix while the old one
      keeps a reachable-if-you-know-the-name credential.
    """

    @pytest.fixture
    def mirrored(self, sandbox, fake_keyring_backend):
        """A 'work' profile with its spotify credential mirrored."""
        from services import credential_store

        sandbox.create("work", logins=True, playlists=True, settings=True)
        cred = sandbox._AUTH_ROOT / "work" / "spotify.json"
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text('{"refresh_token": "SECRET"}', encoding="utf-8")
        fake_keyring_backend.set_password(
            credential_store.KEYRING_SERVICE,
            credential_store._account("spotify.json", "work"),
            '{"refresh_token": "SECRET"}',
        )
        return fake_keyring_backend

    def _peek(self, fake, profile, name="spotify.json"):
        from services import credential_store

        return fake.get_password(
            credential_store.KEYRING_SERVICE,
            credential_store._account(name, profile),
        )

    def test_delete_sweeps_keyring_mirror(self, sandbox, mirrored):
        sandbox.set_active("default")
        sandbox.delete("work")
        assert not (sandbox._AUTH_ROOT / "work").exists()
        assert self._peek(mirrored, "work") is None

    def test_delete_leaves_other_profiles_alone(self, sandbox, mirrored):
        from services import credential_store

        mirrored.set_password(
            credential_store.KEYRING_SERVICE,
            credential_store._account("spotify.json", "default"),
            '{"refresh_token": "OTHER"}',
        )
        sandbox.set_active("default")
        sandbox.delete("work")
        assert self._peek(mirrored, "default") == '{"refresh_token": "OTHER"}'

    def test_rename_moves_keyring_mirror(self, sandbox, mirrored):
        sandbox.set_active("default")
        sandbox.rename("work", "office")
        assert (sandbox._AUTH_ROOT / "office" / "spotify.json").exists()
        assert self._peek(mirrored, "office") == '{"refresh_token": "SECRET"}'
        # Old prefix emptied - a stranded entry is a lingering credential.
        assert self._peek(mirrored, "work") is None

    def test_renamed_profile_can_restore_from_its_mirror(self, sandbox, mirrored):
        from services import credential_store

        sandbox.set_active("default")
        sandbox.rename("work", "office")
        # The renamed profile loses its credential file ...
        (sandbox._AUTH_ROOT / "office" / "spotify.json").unlink()
        # ... and gets it back from the mirror, because the mirror moved.
        sandbox.set_active("office")
        credential_store.restore_all()
        restored = sandbox._AUTH_ROOT / "office" / "spotify.json"
        assert restored.exists()
        assert '"refresh_token": "SECRET"' in restored.read_text(encoding="utf-8")