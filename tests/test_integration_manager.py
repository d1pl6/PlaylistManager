"""Unit tests for services/integration_manager.py.

Covers the security-critical download/install pipeline and the uninstall
ordering:

* ``_safe_extract`` - traversal / absolute-path rejection, link and device
  members skipped.
* ``_validate_plugin_manifest`` / ``_validate_install_target`` - the
  manifest-id and destination guards.
* ``download_integration`` - the release-asset + detached-``.minisig``
  download, fail-closed signature verification (missing signature,
  tampered archive, wrong key, disabled-toggle bypass, unavailable
  verifier, missing pinned key), and the staged-swap install: nothing is
  written to a destination until every copy has validated and staged, a
  failed reinstall leaves the previous installation intact, and a swap
  failure restores the parked installation.
* ``_remove_plugin_directories`` - on-disk manifest-id matching.
* ``uninstall_platform_data`` - credentials, registry, DBs and the
  duplicate/scrobble records are deleted *before* the plugin folder.

Network is never touched: ``_download_archive`` is replaced with a local
writer that serves the release asset and a valid signature from the
committed TEST-ONLY fixture keypair (``tests/fixtures/minisign-test.*``,
password ``minisign-test-password``), so the real extraction/staging/swap
code runs.  The plugin base dir is re-pointed at tmp_path; the real
``integrations/`` tree is never read or written.
"""

import io
import json
import tarfile
import urllib.error
from pathlib import Path

import pytest

import plugin_loader
from services import integration_manager
from services import plugin_signing
from utils.i18n import tr


# ---------------------------------------------------------------------------
# Signature fixture helpers (TEST-ONLY keypair - never the pinned key)
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_FIXTURE_KEY = _FIXTURE_DIR / "minisign-test.key"
_FIXTURE_PASSWORD = "minisign-test-password"


def _sign_bytes(payload: bytes, secret_key=None) -> bytes:
    """Detached ``.minisig`` bytes over *payload*.

    Defaults to the committed test key; pass a different (unencrypted)
    ``minisign.SecretKey`` to simulate a foreign signer.
    """
    import minisign

    sk = secret_key if secret_key is not None else minisign.SecretKey.from_file(
        str(_FIXTURE_KEY)
    )
    if secret_key is None:
        sk.decrypt(_FIXTURE_PASSWORD)
    return bytes(
        sk.sign(payload, trusted_comment="timestamp:0\tfile:plugin.tar.gz")
    )


def _foreign_secret():
    """An unencrypted SecretKey from a throwaway keypair (wrong signer)."""
    import minisign

    return minisign.KeyPair.generate().secret_key


@pytest.fixture(autouse=True)
def _use_fixture_public_key(monkeypatch):
    """Point the pinned key at the TEST-ONLY fixture public key.

    The download tests sign their archives with the fixture secret key, so
    verification (which defaults to the repo's real ``minisign.pub``) must
    resolve to the fixture's public half in this module.  The pinned-key
    contract itself is covered in tests/test_plugin_signing.py.
    """
    monkeypatch.setattr(
        plugin_signing,
        "pinned_public_key_path",
        lambda: _FIXTURE_DIR / "minisign-test.pub",
    )


# ---------------------------------------------------------------------------
# Archive builders
# ---------------------------------------------------------------------------


def _tar_bytes(entries):
    """Build an in-memory .tar.gz from (name, data, kind) triples.

    ``kind`` is one of ``file``/``dir``/``symlink``/``hardlink``/``char``;
    ``data`` is the file body (``file``) or the link target (links).
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data
                tar.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = data
                tar.addfile(info)
            elif kind == "char":
                info.type = tarfile.CHRTYPE
                info.devmajor = 1
                info.devminor = 3
                tar.addfile(info)
    return buf.getvalue()


def _repo_tarball(
    platform_id,
    *,
    include_manifest=True,
    declared_id=None,
    extra_entries=(),
    wrap="repo-abc123",
):
    """A GitHub-style plugin repo tarball wrapped in one top-level dir."""
    declared = platform_id if declared_id is None else declared_id
    root = f"{wrap}/"
    entries = [(root, None, "dir")]
    if include_manifest:
        manifest = {
            "id": declared,
            "display_name": "Test Platform",
            "integration_class": "TestIntegration",
            "flow_type": "api",
        }
        entries.append((f"{root}plugin.json", json.dumps(manifest).encode(), "file"))
    entries.append((f"{root}flow.py", b"# code", "file"))
    entries.extend(extra_entries)
    return _tar_bytes(entries)


def _extract_bytes(payload, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        integration_manager._safe_extract(tar, dest)


def _serve(monkeypatch, payload, *, unsigned=False, sig=None):
    """Replace the network download with a local asset + signature writer.

    *payload* is served for the release-asset URL, and a valid detached
    signature over *payload* (fixture key) for the ``.minisig`` URL.
    ``unsigned=True`` makes the signature URL return HTTP 404 (a repo that
    never attached a signature); ``sig=`` overrides the signature bytes
    (e.g. a signature over different content, or from a foreign key).
    """

    def _fake_download(url, dest, **kwargs):
        if url.endswith(plugin_signing.SIGNATURE_SUFFIX):
            if unsigned:
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            dest.write_bytes(sig if sig is not None else _sign_bytes(payload))
        else:
            dest.write_bytes(payload)

    monkeypatch.setattr(integration_manager, "_download_archive", _fake_download)


@pytest.fixture
def base_dir(monkeypatch, tmp_path):
    """Point ``PluginRegistry().base_dir`` at a throwaway integrations dir."""
    base = tmp_path / "integrations"
    base.mkdir()
    monkeypatch.setattr(
        plugin_loader.PluginRegistry,
        "base_dir",
        property(lambda self: base),
    )
    return base


# ---------------------------------------------------------------------------
# _safe_extract
# ---------------------------------------------------------------------------


class TestSafeExtract:
    def test_regular_file_extracted(self, tmp_path):
        dest = tmp_path / "out"
        _extract_bytes(_tar_bytes([("pkg/flow.py", b"# code", "file")]), dest)
        assert (dest / "pkg" / "flow.py").read_bytes() == b"# code"

    def test_parent_traversal_rejected(self, tmp_path):
        dest = tmp_path / "out"
        payload = _tar_bytes([("../escape.txt", b"x", "file")])
        with pytest.raises(ValueError, match="Unsafe archive member"):
            _extract_bytes(payload, dest)
        assert not (tmp_path / "escape.txt").exists()

    def test_absolute_path_rejected(self, tmp_path):
        dest = tmp_path / "out"
        payload = _tar_bytes([("/abs_escape.txt", b"x", "file")])
        with pytest.raises(ValueError, match="Unsafe archive member"):
            _extract_bytes(payload, dest)

    def test_dot_dot_mid_path_rejected(self, tmp_path):
        dest = tmp_path / "out"
        payload = _tar_bytes([("pkg/../../escape.txt", b"x", "file")])
        with pytest.raises(ValueError, match="Unsafe archive member"):
            _extract_bytes(payload, dest)

    def test_symlink_skipped(self, tmp_path):
        dest = tmp_path / "out"
        payload = _tar_bytes([("link", "../target", "symlink")])
        _extract_bytes(payload, dest)
        assert not (dest / "link").exists()

    def test_hardlink_skipped(self, tmp_path):
        dest = tmp_path / "out"
        payload = _tar_bytes([("hard", "../target", "hardlink")])
        _extract_bytes(payload, dest)
        assert not (dest / "hard").exists()

    def test_device_member_skipped(self, tmp_path):
        dest = tmp_path / "out"
        payload = _tar_bytes([("dev", None, "char")])
        _extract_bytes(payload, dest)
        assert not (dest / "dev").exists()

    def test_skipped_links_do_not_block_later_files(self, tmp_path):
        dest = tmp_path / "out"
        payload = _tar_bytes(
            [
                ("link", "../x", "symlink"),
                ("dev", None, "char"),
                ("ok.txt", b"fine", "file"),
            ]
        )
        _extract_bytes(payload, dest)
        assert (dest / "ok.txt").read_bytes() == b"fine"


# ---------------------------------------------------------------------------
# _validate_install_target / _validate_plugin_manifest
# ---------------------------------------------------------------------------


class TestValidateInstallTarget:
    def test_inside_base_allowed(self, tmp_path):
        base = tmp_path / "integrations"
        integration_manager._validate_install_target(base / "spotify", base.resolve())

    def test_base_itself_allowed(self, tmp_path):
        base = tmp_path / "integrations"
        integration_manager._validate_install_target(base, base.resolve())

    def test_escape_rejected(self, tmp_path):
        base = tmp_path / "integrations"
        with pytest.raises(ValueError, match="escapes integrations dir"):
            integration_manager._validate_install_target(
                tmp_path / "evil", base.resolve()
            )


class TestValidatePluginManifest:
    def _manifest(self, tmp_path, payload):
        path = tmp_path / "plugin.json"
        path.write_text(payload, encoding="utf-8")
        return path

    def test_matching_id_passes(self, tmp_path):
        path = self._manifest(tmp_path, json.dumps({"id": "spotify"}))
        integration_manager._validate_plugin_manifest(path, "spotify")

    def test_foreign_id_rejected(self, tmp_path):
        path = self._manifest(tmp_path, json.dumps({"id": "deezer"}))
        with pytest.raises(ValueError, match="does not contain the 'spotify' plugin"):
            integration_manager._validate_plugin_manifest(path, "spotify")

    def test_missing_id_rejected(self, tmp_path):
        path = self._manifest(tmp_path, json.dumps({"display_name": "x"}))
        with pytest.raises(ValueError, match="manifest declares id ''"):
            integration_manager._validate_plugin_manifest(path, "spotify")

    def test_invalid_json_rejected(self, tmp_path):
        path = self._manifest(tmp_path, "{not json")
        with pytest.raises(ValueError, match="Invalid plugin.json in download"):
            integration_manager._validate_plugin_manifest(path, "spotify")

    def test_non_object_json_rejected(self, tmp_path):
        # A JSON array/string/null is valid JSON but not a manifest object.
        # Regression: raw.get() on a list raised AttributeError instead of
        # a clean refusal.
        for payload in ("[1, 2]", '"spotify"', "null"):
            path = self._manifest(tmp_path, payload)
            with pytest.raises(ValueError, match="Invalid plugin.json in download"):
                integration_manager._validate_plugin_manifest(path, "spotify")

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid plugin.json in download"):
            integration_manager._validate_plugin_manifest(
                tmp_path / "nope.json", "spotify"
            )


# ---------------------------------------------------------------------------
# download_integration - staged-swap install
# ---------------------------------------------------------------------------


class TestDownloadIntegration:
    def test_unknown_platform_raises_without_network(self, monkeypatch):
        def _fail(*a, **k):
            raise AssertionError("network must not be touched")

        monkeypatch.setattr(integration_manager, "_download_archive", _fail)
        with pytest.raises(ValueError, match="No download source"):
            integration_manager.download_integration("nope")

    def test_installs_plugin_tree(self, monkeypatch, base_dir):
        _serve(monkeypatch, _repo_tarball("youtube_music"))
        installed = integration_manager.download_integration("youtube_music")

        dst = base_dir / "youtube_music"
        assert installed == [dst]
        assert (dst / "plugin.json").is_file()
        assert (dst / "flow.py").read_text(encoding="utf-8") == "# code"
        # Staging and backup dirs are gone after a clean install.
        assert not (base_dir / ".youtube_music.install_tmp").exists()
        assert not (base_dir / ".youtube_music.old").exists()

    def test_vcs_and_pycache_entries_excluded(self, monkeypatch, base_dir):
        extra = [
            ("repo-abc123/.gitignore", b"x", "file"),
            ("repo-abc123/__pycache__/cached.pyc", b"x", "file"),
            ("repo-abc123/pkg/__pycache__/mod.pyc", b"x", "file"),
        ]
        _serve(monkeypatch, _repo_tarball("youtube_music", extra_entries=extra))
        integration_manager.download_integration("youtube_music")

        dst = base_dir / "youtube_music"
        assert not (dst / ".gitignore").exists()
        assert not (dst / "__pycache__").exists()
        assert not (dst / "pkg" / "__pycache__").exists()

    def test_missing_manifest_refuses_and_leaves_nothing(self, monkeypatch, base_dir):
        _serve(
            monkeypatch,
            _repo_tarball("youtube_music", include_manifest=False),
        )
        with pytest.raises(FileNotFoundError, match="missing"):
            integration_manager.download_integration("youtube_music")

        assert not (base_dir / "youtube_music").exists()
        assert not (base_dir / ".youtube_music.install_tmp").exists()
        assert not (base_dir / ".youtube_music.old").exists()

    def test_wrong_manifest_id_refuses(self, monkeypatch, base_dir):
        _serve(
            monkeypatch,
            _repo_tarball("youtube_music", declared_id="spotify"),
        )
        with pytest.raises(ValueError, match="does not contain the 'youtube_music'"):
            integration_manager.download_integration("youtube_music")
        assert not (base_dir / "youtube_music").exists()

    def test_non_object_manifest_refuses(self, monkeypatch, base_dir):
        # A repo whose plugin.json is valid JSON but not an object must be
        # refused, not crash with AttributeError.  The array entry shares
        # the manifest path and is written last, so it wins at extract time.
        manifest = ("repo-abc123/plugin.json", b"[1, 2, 3]", "file")
        _serve(
            monkeypatch,
            _repo_tarball("youtube_music", extra_entries=[manifest]),
        )
        with pytest.raises(ValueError, match="Invalid plugin.json in download"):
            integration_manager.download_integration("youtube_music")
        assert not (base_dir / "youtube_music").exists()

    def test_reinstall_replaces_existing_installation(self, monkeypatch, base_dir):
        dst = base_dir / "youtube_music"
        dst.mkdir()
        (dst / "stale.txt").write_text("stale", encoding="utf-8")

        _serve(monkeypatch, _repo_tarball("youtube_music"))
        integration_manager.download_integration("youtube_music")

        assert not (dst / "stale.txt").exists()
        assert (dst / "plugin.json").is_file()
        assert not (base_dir / ".youtube_music.old").exists()

    def test_failed_reinstall_keeps_previous_installation(self, monkeypatch, base_dir):
        dst = base_dir / "youtube_music"
        dst.mkdir()
        (dst / "marker.txt").write_text("keep me", encoding="utf-8")

        _serve(
            monkeypatch,
            _repo_tarball("youtube_music", include_manifest=False),
        )
        with pytest.raises(FileNotFoundError):
            integration_manager.download_integration("youtube_music")

        # Content validation failed before any swap: the old install is
        # untouched and no new files leaked in.
        assert (dst / "marker.txt").read_text(encoding="utf-8") == "keep me"
        assert not (dst / "flow.py").exists()
        assert not (base_dir / ".youtube_music.install_tmp").exists()
        assert not (base_dir / ".youtube_music.old").exists()

    def test_stale_staging_root_replaced(self, monkeypatch, base_dir):
        staging = base_dir / ".youtube_music.install_tmp"
        staging.mkdir()
        (staging / "junk.txt").write_text("junk", encoding="utf-8")

        _serve(monkeypatch, _repo_tarball("youtube_music"))
        integration_manager.download_integration("youtube_music")

        assert not staging.exists()
        assert (base_dir / "youtube_music" / "plugin.json").is_file()

    def test_swap_failure_restores_parked_installation(self, monkeypatch, base_dir):
        dst = base_dir / "youtube_music"
        dst.mkdir()
        (dst / "marker.txt").write_text("old", encoding="utf-8")

        _serve(monkeypatch, _repo_tarball("youtube_music"))

        real_rename = Path.rename
        state = {"raised": False}

        def flaky_rename(self, target):
            if not state["raised"] and Path(target) == dst:
                state["raised"] = True
                raise OSError("simulated swap failure")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", flaky_rename)

        with pytest.raises(OSError, match="simulated swap failure"):
            integration_manager.download_integration("youtube_music")

        # The parked installation was moved back and the new tree discarded.
        assert (dst / "marker.txt").read_text(encoding="utf-8") == "old"
        assert not (dst / "flow.py").exists()
        assert not (base_dir / ".youtube_music.old").exists()
        assert not (base_dir / ".youtube_music.install_tmp").exists()

    def test_park_failure_leaves_the_installation_intact(
        self, monkeypatch, base_dir
    ):
        # Moving the old install aside is itself a filesystem operation
        # and can fail (full disk, read-only integrations/, a Windows AV
        # lock).  It must be inside the restore handler, or the platform
        # would be left with no files and the staging copy thrown away.
        dst = base_dir / "youtube_music"
        dst.mkdir()
        (dst / "marker.txt").write_text("old", encoding="utf-8")

        _serve(monkeypatch, _repo_tarball("youtube_music"))

        real_move = integration_manager.shutil.move
        state = {"raised": False}

        def flaky_move(src, dst_arg, *args, **kwargs):
            # Fail the PARK (dst -> .dst.old), not the restore.
            if not state["raised"] and Path(src) == dst:
                state["raised"] = True
                raise PermissionError("simulated park failure")
            return real_move(src, dst_arg, *args, **kwargs)

        monkeypatch.setattr(integration_manager.shutil, "move", flaky_move)

        with pytest.raises(PermissionError, match="simulated park failure"):
            integration_manager.download_integration("youtube_music")

        # Nothing was destroyed: the old install is exactly where it was.
        assert (dst / "marker.txt").read_text(encoding="utf-8") == "old"
        assert not (base_dir / ".youtube_music.old").exists()
        assert not (base_dir / ".youtube_music.install_tmp").exists()

    # ------------------------------------------------------------------
    # Signature verification (fail-closed by default)
    # ------------------------------------------------------------------

    def test_unsigned_archive_refused_when_verification_on(
        self, monkeypatch, base_dir
    ):
        pytest.importorskip("minisign")
        _serve(monkeypatch, _repo_tarball("youtube_music"), unsigned=True)
        with pytest.raises(ValueError, match="no minisign signature"):
            integration_manager.download_integration("youtube_music")
        # Nothing was written - not even a staging dir.
        assert not (base_dir / "youtube_music").exists()
        assert not (base_dir / ".youtube_music.install_tmp").exists()

    def test_tampered_archive_refused(self, monkeypatch, base_dir):
        pytest.importorskip("minisign")
        payload = _repo_tarball("youtube_music")
        # Signature is valid, but over different content.
        _serve(monkeypatch, payload, sig=_sign_bytes(b"different payload"))
        with pytest.raises(ValueError, match="signature verification failed"):
            integration_manager.download_integration("youtube_music")
        assert not (base_dir / "youtube_music").exists()

    def test_signature_from_foreign_key_refused(self, monkeypatch, base_dir):
        pytest.importorskip("minisign")
        payload = _repo_tarball("youtube_music")
        _serve(
            monkeypatch, payload,
            sig=_sign_bytes(payload, secret_key=_foreign_secret()),
        )
        with pytest.raises(ValueError, match="signature verification failed"):
            integration_manager.download_integration("youtube_music")
        assert not (base_dir / "youtube_music").exists()

    def test_toggle_off_installs_unsigned_archive(self, monkeypatch, base_dir):
        pytest.importorskip("minisign")
        monkeypatch.setattr(
            integration_manager,
            "_signature_verification_enabled",
            lambda: False,
        )
        _serve(monkeypatch, _repo_tarball("youtube_music"), unsigned=True)
        installed = integration_manager.download_integration("youtube_music")
        assert (base_dir / "youtube_music" / "plugin.json").is_file()
        assert installed

    def test_verifier_unavailable_fails_closed(self, monkeypatch, base_dir):
        pytest.importorskip("minisign")

        def _boom(*args, **kwargs):
            raise plugin_signing.VerifierUnavailableError(
                tr("install.verifier_unavailable")
            )

        monkeypatch.setattr(plugin_signing, "verify_archive", _boom)
        _serve(monkeypatch, _repo_tarball("youtube_music"))
        with pytest.raises(ValueError, match="No plugin-signature verifier"):
            integration_manager.download_integration("youtube_music")
        assert not (base_dir / "youtube_music").exists()

    def test_pinned_key_missing_fails_closed(self, monkeypatch, base_dir, tmp_path):
        pytest.importorskip("minisign")
        monkeypatch.setattr(
            plugin_signing,
            "pinned_public_key_path",
            lambda: tmp_path / "no-such.pub",
        )
        _serve(monkeypatch, _repo_tarball("youtube_music"))
        with pytest.raises(ValueError, match="minisign.pub"):
            integration_manager.download_integration("youtube_music")
        assert not (base_dir / "youtube_music").exists()

    def test_signature_fetch_uses_small_cap_and_release_urls(
        self, monkeypatch, base_dir
    ):
        pytest.importorskip("minisign")
        payload = _repo_tarball("youtube_music")
        calls = []

        def _spy(url, dest, **kwargs):
            calls.append((url, kwargs.get("max_bytes")))
            if url.endswith(plugin_signing.SIGNATURE_SUFFIX):
                dest.write_bytes(_sign_bytes(payload))
            else:
                dest.write_bytes(payload)

        monkeypatch.setattr(integration_manager, "_download_archive", _spy)
        integration_manager.download_integration("youtube_music")

        assert len(calls) == 2
        archive_url, archive_cap = calls[0]
        sig_url, sig_cap = calls[1]
        assert archive_url.endswith("/releases/latest/download/plugin.tar.gz")
        # The archive fetch relies on the function default (no max_bytes
        # passed); the signature fetch must use the small signature cap.
        assert archive_cap is None
        assert sig_url.endswith("/releases/latest/download/plugin.tar.gz.minisig")
        assert sig_cap == integration_manager._MAX_SIGNATURE_BYTES


class TestReleaseAssetUrls:
    def test_catalog_points_at_release_assets_with_signatures(self):
        for pid, repo in integration_manager.INTEGRATION_REPOS.items():
            expected_asset = (
                f"https://github.com/{repo.owner}/{repo.repo}"
                f"/releases/latest/download/{repo.asset_name}"
            )
            assert repo.download_url == expected_asset, pid
            assert repo.signature_url == expected_asset + ".minisig", pid
            assert "codeload" not in repo.download_url, pid


# ---------------------------------------------------------------------------
# _download_archive - size cap
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDownloadArchive:
    def test_writes_payload_under_cap(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            integration_manager.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse([b"abc", b"def"]),
        )
        dest = tmp_path / "repo.tar.gz"
        integration_manager._download_archive("https://example.invalid/x", dest)
        assert dest.read_bytes() == b"abcdef"

    def test_oversized_archive_rejected(self, monkeypatch, tmp_path):
        # A compromised / re-purposed source must not be able to stream
        # unbounded bytes onto disk.
        monkeypatch.setattr(
            integration_manager.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse([b"x" * 8, b"y" * 8]),
        )
        with pytest.raises(OSError, match="exceeds"):
            integration_manager._download_archive(
                "https://example.invalid/x",
                tmp_path / "repo.tar.gz",
                max_bytes=10,
            )


# ---------------------------------------------------------------------------
# _remove_plugin_directories
# ---------------------------------------------------------------------------


class TestRemovePluginDirectories:
    def _mk(self, base, name, manifest):
        d = base / name
        d.mkdir()
        (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        return d

    def test_matching_manifest_removed(self, base_dir):
        match = self._mk(base_dir, "yt_music", {"id": "youtube_music"})
        other = self._mk(base_dir, "spotify", {"id": "spotify"})

        assert integration_manager._remove_plugin_directories("youtube_music") == 1
        assert not match.exists()
        assert other.exists()

    def test_manifest_with_trailing_metadata_still_matches(self, base_dir):
        d = self._mk(
            base_dir, "yt_music", {"id": "youtube_music", "version": "3"}
        )
        assert integration_manager._remove_plugin_directories("youtube_music") == 1
        assert not d.exists()

    def test_invalid_json_skipped(self, base_dir):
        d = base_dir / "broken"
        d.mkdir()
        (d / "plugin.json").write_text("{not json", encoding="utf-8")

        assert integration_manager._remove_plugin_directories("youtube_music") == 0
        assert d.exists()

    def test_non_object_manifest_skipped(self, base_dir):
        # Regression: raw.get() on a list crashed the uninstall with
        # AttributeError instead of skipping the bad entry.
        d = base_dir / "weird"
        d.mkdir()
        (d / "plugin.json").write_text("[1, 2, 3]", encoding="utf-8")

        assert integration_manager._remove_plugin_directories("youtube_music") == 0
        assert d.exists()

    def test_missing_base_dir_returns_zero(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            plugin_loader.PluginRegistry,
            "base_dir",
            property(lambda self: tmp_path / "missing"),
        )
        assert integration_manager._remove_plugin_directories("youtube_music") == 0

    def test_unrelated_dirs_untouched(self, base_dir):
        for name, pid in (("a", "spotify"), ("b", "deezer"), ("c", "lastfm")):
            self._mk(base_dir, name, {"id": pid})
        assert integration_manager._remove_plugin_directories("youtube_music") == 0
        assert sorted(p.name for p in base_dir.iterdir()) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# uninstall_platform_data
# ---------------------------------------------------------------------------


def _stub_downstream(monkeypatch):
    """Replace the post-credential service calls with silent stubs."""
    monkeypatch.setattr(
        integration_manager.PlaylistStore,
        "delete_playlists_for_platform",
        staticmethod(lambda pid: 0),
    )
    monkeypatch.setattr(
        integration_manager.DatabaseManager,
        "delete_platform_databases",
        staticmethod(lambda pid: 0),
    )
    monkeypatch.setattr(
        integration_manager.duplicate_queue,
        "purge_platform",
        lambda pid: (0, 0, 0),
    )
    monkeypatch.setattr(
        integration_manager.scrobble_log,
        "remove_platform_entries",
        lambda pid: None,
    )
    monkeypatch.setattr(
        integration_manager,
        "_remove_plugin_directories",
        lambda pid: 0,
    )


class TestUninstallOrdering:
    def test_destructive_steps_precede_plugin_removal(
        self, monkeypatch, tmp_path, base_dir
    ):
        events = []
        cred = tmp_path / "yt_music.json"
        cred.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            integration_manager.auth_setup,
            "PLATFORM_CREDENTIAL_FILES",
            {"youtube_music": [cred]},
        )

        def _check_credentials_gone(name, result):
            def _call(platform_id):
                # Credentials must already be deleted when the store / DB /
                # queue cleanup starts, and the plugin folder must go last.
                assert not cred.exists(), f"{name} ran before credentials were deleted"
                events.append(name)
                return result

            return _call

        monkeypatch.setattr(
            integration_manager.PlaylistStore,
            "delete_playlists_for_platform",
            staticmethod(_check_credentials_gone("playlists", 2)),
        )
        monkeypatch.setattr(
            integration_manager.DatabaseManager,
            "delete_platform_databases",
            staticmethod(_check_credentials_gone("databases", 3)),
        )
        monkeypatch.setattr(
            integration_manager.duplicate_queue,
            "purge_platform",
            _check_credentials_gone("duplicate_queue", (1, 4, 5)),
        )
        monkeypatch.setattr(
            integration_manager.scrobble_log,
            "remove_platform_entries",
            _check_credentials_gone("scrobble_log", None),
        )
        monkeypatch.setattr(
            integration_manager,
            "_remove_plugin_directories",
            _check_credentials_gone("plugin_dirs", 1),
        )

        report = integration_manager.uninstall_platform_data("youtube_music")

        # The plugin folder is torn down only after every data store has
        # been cleaned.  See the module docstring and AGENTS.md
        # "Uninstall ordering".
        assert events == [
            "playlists",
            "databases",
            "duplicate_queue",
            "scrobble_log",
            "plugin_dirs",
        ]
        assert not cred.exists()
        assert report == {
            "credentials": 1,
            "playlists": 2,
            "databases": 3,
            "pending": 1,
            "songs": 4,
            "errors": 5,
            "plugin_dirs": 1,
        }

    def test_missing_credentials_counted_zero(self, base_dir, monkeypatch, tmp_path):
        monkeypatch.setattr(
            integration_manager.auth_setup,
            "PLATFORM_CREDENTIAL_FILES",
            {"youtube_music": [tmp_path / "does_not_exist.json"]},
        )
        _stub_downstream(monkeypatch)
        report = integration_manager.uninstall_platform_data("youtube_music")
        assert report["credentials"] == 0

    def test_plugin_manifest_auth_paths_used_when_available(
        self, base_dir, monkeypatch, tmp_path
    ):
        # A fallback credential (e.g. browser.json) must be deleted even
        # when it is not in the hardcoded map - a survivor would
        # re-authenticate the plugin on the next download.
        fallback = tmp_path / "browser.json"
        fallback.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            integration_manager.auth_setup,
            "PLATFORM_CREDENTIAL_FILES",
            {"youtube_music": [tmp_path / "yt_music.json"]},
        )
        _stub_downstream(monkeypatch)

        class _Plugin:
            auth_paths = [fallback]

        report = integration_manager.uninstall_platform_data(
            "youtube_music", plugin=_Plugin()
        )
        assert report["credentials"] == 1
        assert not fallback.exists()

    def test_hardcoded_map_merged_with_manifest_paths(self, base_dir, monkeypatch, tmp_path):
        # Both the manifest path and the hardcoded fallback are removed,
        # without deleting a shared path twice.
        shared = tmp_path / "shared.json"
        shared.write_text("{}", encoding="utf-8")
        hardcoded = tmp_path / "yt_music.json"
        hardcoded.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            integration_manager.auth_setup,
            "PLATFORM_CREDENTIAL_FILES",
            {"youtube_music": [hardcoded]},
        )
        _stub_downstream(monkeypatch)

        class _Plugin:
            auth_paths = [shared, hardcoded]

        report = integration_manager.uninstall_platform_data(
            "youtube_music", plugin=_Plugin()
        )
        assert report["credentials"] == 2
        assert not shared.exists()
        assert not hardcoded.exists()


class TestUninstallBestEffort:
    def test_credential_delete_failure_is_logged_not_raised(
        self, base_dir, monkeypatch, tmp_path
    ):
        locked = tmp_path / "locked.json"
        locked.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            integration_manager.auth_setup,
            "PLATFORM_CREDENTIAL_FILES",
            {"youtube_music": [locked]},
        )
        _stub_downstream(monkeypatch)

        real_unlink = Path.unlink

        def _boom(self, *args, **kwargs):
            if self == locked:
                raise PermissionError("locked")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _boom)

        # Must not raise; the count reflects what was actually deleted.
        report = integration_manager.uninstall_platform_data("youtube_music")
        assert report["credentials"] == 0
        assert locked.exists()

    def test_duplicate_purge_failure_does_not_abort_the_uninstall(
        self, base_dir, monkeypatch, tmp_path
    ):
        # extra.json can be unwritable (full disk, permissions).  A stale
        # duplicate record is cosmetic next to a half-done uninstall, so
        # the raise must not skip the scrobble-ledger purge or the plugin
        # directory removal that follow it.
        removed = []
        _stub_downstream(monkeypatch)

        def _boom(pid):
            raise OSError("No space left on device")

        monkeypatch.setattr(
            integration_manager.duplicate_queue, "purge_platform", _boom
        )
        monkeypatch.setattr(
            integration_manager.scrobble_log,
            "remove_platform_entries",
            lambda pid: removed.append(pid),
        )
        monkeypatch.setattr(
            integration_manager,
            "_remove_plugin_directories",
            lambda pid: 1,
        )

        report = integration_manager.uninstall_platform_data("youtube_music")

        assert report["pending"] == 0
        assert report["songs"] == 0
        assert report["errors"] == 0
        # The steps AFTER the failed purge still ran.
        assert removed == ["youtube_music"]
        assert report["plugin_dirs"] == 1

    def test_typeerror_from_a_bad_record_is_also_tolerated(
        self, base_dir, monkeypatch
    ):
        # json.dump raises TypeError on an unserialisable value - a real
        # (if unlikely) way for a purge to fail, and the same class of
        # best-effort bookkeeping.
        _stub_downstream(monkeypatch)
        monkeypatch.setattr(
            integration_manager.duplicate_queue,
            "purge_platform",
            lambda pid: (_ for _ in ()).throw(TypeError("not serialisable")),
        )
        report = integration_manager.uninstall_platform_data("youtube_music")
        assert (report["pending"], report["songs"], report["errors"]) == (0, 0, 0)


class TestUninstallRealData:
    @pytest.fixture
    def sandboxed_store(self, monkeypatch, tmp_path):
        import services.playlist_store as ps

        monkeypatch.setattr(ps, "playlists_json", tmp_path / "db" / "playlists.json")
        monkeypatch.setattr(ps, "_playlist_cache", None)
        monkeypatch.setattr(ps, "_cache_timestamp", 0.0)
        return ps

    def test_registry_credentials_and_plugin_dir_removed(
        self, sandboxed_store, base_dir, monkeypatch, tmp_path
    ):
        store = sandboxed_store
        store.PlaylistStore.add_playlist("Chill", "youtube_music", "pl1")
        store.PlaylistStore.add_playlist("Keep", "spotify", "pl2")

        plugin_dir = base_dir / "youtube_music"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"id": "youtube_music"}), encoding="utf-8"
        )

        cred = tmp_path / "yt_music.json"
        cred.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            integration_manager.auth_setup,
            "PLATFORM_CREDENTIAL_FILES",
            {"youtube_music": [cred]},
        )
        # DB / duplicate-queue / scrobble cleanup is out of scope here.
        monkeypatch.setattr(
            integration_manager.DatabaseManager,
            "delete_platform_databases",
            staticmethod(lambda pid: 0),
        )
        monkeypatch.setattr(
            integration_manager.duplicate_queue, "purge_platform", lambda pid: (0, 0, 0)
        )
        monkeypatch.setattr(
            integration_manager.scrobble_log, "remove_platform_entries", lambda pid: None
        )

        report = integration_manager.uninstall_platform_data("youtube_music")

        assert report["credentials"] == 1
        assert report["playlists"] == 1
        assert report["plugin_dirs"] == 1
        assert not cred.exists()
        assert not plugin_dir.exists()
        assert [p["name"] for p in store.PlaylistStore.load_playlists()] == ["Keep"]


class TestUninstallClearsKeyringMirror:
    """Uninstall must clear the OS-keyring mirror (services/credential_store.py).

    The Manage dialog's uninstall path unlinks credential files itself
    instead of going through ``auth_setup.delete_platform_credentials``,
    so it needs its own ``credential_store.delete()`` call - without it a
    removed platform left live tokens in the OS keyring, contradicting the
    documented "logout/uninstall clears the keyring copy" contract.
    """

    @pytest.fixture
    def auth_dir(self, tmp_path, monkeypatch):
        from services import credential_store

        d = tmp_path / "auth"
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(credential_store._profile_store, "auth_dir", lambda: d)
        return d

    def test_auth_dir_credential_clears_mirror(
        self, auth_dir, base_dir, monkeypatch, fake_keyring_backend
    ):
        from services import credential_store

        _stub_downstream(monkeypatch)
        cred = auth_dir / "yt_music.json"
        cred.write_text('{"cookie": "SECRET"}', encoding="utf-8")
        monkeypatch.setattr(
            integration_manager.auth_setup,
            "PLATFORM_CREDENTIAL_FILES",
            {"youtube_music": [cred]},
        )
        fake_keyring_backend.set_password(
            credential_store.KEYRING_SERVICE,
            credential_store._account("yt_music.json"),
            '{"cookie": "SECRET"}',
        )

        report = integration_manager.uninstall_platform_data("youtube_music")

        assert report["credentials"] == 1
        assert not cred.exists()
        assert (
            fake_keyring_backend.get_password(
                credential_store.KEYRING_SERVICE,
                credential_store._account("yt_music.json"),
            )
            is None
        )

    def test_repo_root_fallback_is_not_touched_in_keyring(
        self, tmp_path, base_dir, monkeypatch, fake_keyring_backend
    ):
        # A manifest-declared ``auth_file_fallbacks`` entry is a repo-root
        # copy: it is never mirrored, so uninstall must not reach for it.
        from services import credential_store

        _stub_downstream(monkeypatch)
        fallback = tmp_path / "legacy_browser.json"
        fallback.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            integration_manager.auth_setup,
            "PLATFORM_CREDENTIAL_FILES",
            {"youtube_music": [fallback]},
        )

        deletes = []
        monkeypatch.setattr(
            integration_manager.credential_store, "delete", deletes.append
        )
        report = integration_manager.uninstall_platform_data("youtube_music")

        assert report["credentials"] == 1
        assert not fallback.exists()
        assert deletes == []
