"""Unit tests for services/plugin_signing.py (release-signature verification).

Uses the committed TEST-ONLY keypair in ``tests/fixtures/``
(``minisign-test.key`` + ``minisign-test.pub``, password
``minisign-test-password``).  That keypair exists only so tests can mint
valid signatures at runtime; real installs always verify against the
pinned ``minisign.pub`` at the repo root.  The pinned-key tests therefore
assert the repo actually ships and parses the real anchor - deleting it is
a test failure.

The ``minisign`` python package (py-minisign, a core dependency) is used
to generate test signatures; the CLI-backend tests force the CLI path by
hiding the module from import.
"""

import shutil
import sys
from pathlib import Path

import pytest

from services import plugin_signing
from utils.i18n import tr

FIXTURES = Path(__file__).parent / "fixtures"
TEST_KEY = FIXTURES / "minisign-test.key"
TEST_PUB = FIXTURES / "minisign-test.pub"
TEST_PASSWORD = "minisign-test-password"


def _sign(payload: bytes, secret_key=None) -> bytes:
    """Detached ``.minisig`` bytes over *payload* (fixture key by default)."""
    import minisign

    sk = secret_key if secret_key is not None else minisign.SecretKey.from_file(
        str(TEST_KEY)
    )
    if secret_key is None:
        sk.decrypt(TEST_PASSWORD)
    sig = sk.sign(payload, trusted_comment="timestamp:0\tfile:plugin.tar.gz")
    return bytes(sig)


def _other_secret_key():
    import minisign

    return minisign.KeyPair.generate().secret_key  # unencrypted, signs directly


def _require_minisign():
    return pytest.importorskip("minisign")


class TestPinnedKey:
    def test_repo_ships_pinned_public_key(self):
        _require_minisign()
        assert plugin_signing.pinned_public_key_path().is_file()

    def test_pinned_key_parses(self):
        _require_minisign()
        plugin_signing.load_public_key()  # raises on missing/malformed

    def test_missing_key_file_raises_pinned_key_error(self, tmp_path):
        _require_minisign()
        with pytest.raises(plugin_signing.PinnedKeyError):
            plugin_signing.load_public_key(tmp_path / "nope.pub")

    def test_garbage_key_file_raises_pinned_key_error(self, tmp_path):
        _require_minisign()
        bad = tmp_path / "bad.pub"
        bad.write_text("untrusted comment: x\nNONSENSE\n")
        with pytest.raises(plugin_signing.PinnedKeyError):
            plugin_signing.load_public_key(bad)


class TestVerifyArchive:
    def test_valid_signature_passes(self, tmp_path):
        _require_minisign()
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(_sign(archive.read_bytes()))
        plugin_signing.verify_archive(archive, sig, pubkey_path=TEST_PUB)

    def test_tampered_archive_rejected(self, tmp_path):
        _require_minisign()
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(_sign(b"different-data"))
        with pytest.raises(plugin_signing.SignatureInvalidError):
            plugin_signing.verify_archive(archive, sig, pubkey_path=TEST_PUB)

    def test_signature_from_other_key_rejected(self, tmp_path):
        _require_minisign()
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(_sign(b"archive-data", secret_key=_other_secret_key()))
        with pytest.raises(plugin_signing.SignatureInvalidError):
            plugin_signing.verify_archive(archive, sig, pubkey_path=TEST_PUB)

    def test_wrong_pubkey_file_rejected(self, tmp_path):
        _require_minisign()
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(_sign(archive.read_bytes()))
        other_pub = tmp_path / "other.pub"
        secret = _other_secret_key()
        # PublicKey.from_file expects the 2-line minisign format
        # (untrusted comment + base64 key); the comment text is free-form.
        other_pub.write_bytes(
            b"untrusted comment: other test key\n"
            + secret.get_public_key().to_base64()
            + b"\n"
        )
        with pytest.raises(plugin_signing.SignatureInvalidError):
            plugin_signing.verify_archive(archive, sig, pubkey_path=other_pub)

    def test_missing_signature_file_raises_missing(self, tmp_path):
        _require_minisign()
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        with pytest.raises(plugin_signing.SignatureMissingError):
            plugin_signing.verify_archive(
                archive, tmp_path / "nope.minisig", pubkey_path=TEST_PUB
            )

    def test_empty_signature_file_raises_missing(self, tmp_path):
        _require_minisign()
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(b"")
        with pytest.raises(plugin_signing.SignatureMissingError):
            plugin_signing.verify_archive(archive, sig, pubkey_path=TEST_PUB)

    def test_garbage_signature_bytes_rejected(self, tmp_path):
        _require_minisign()
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(b"definitely not a minisign signature")
        with pytest.raises(plugin_signing.SignatureInvalidError):
            plugin_signing.verify_archive(archive, sig, pubkey_path=TEST_PUB)

    def test_error_messages_are_user_facing(self, tmp_path):
        _require_minisign()
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(_sign(b"different-data"))
        with pytest.raises(plugin_signing.SignatureInvalidError) as exc:
            plugin_signing.verify_archive(archive, sig, pubkey_path=TEST_PUB)
        assert str(exc.value) == tr("install.signature_invalid")


class TestBackends:
    def test_verifier_available_with_py_minisign(self):
        _require_minisign()
        assert plugin_signing.verifier_available()

    def test_verifier_reports_cli_when_package_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "minisign", None)
        monkeypatch.setattr(
            plugin_signing.shutil, "which", lambda name: "/usr/bin/minisign"
        )
        assert plugin_signing.verifier_available()

    def test_verifier_unavailable_without_backend(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "minisign", None)
        monkeypatch.setattr(plugin_signing.shutil, "which", lambda name: None)
        assert not plugin_signing.verifier_available()

    def test_verify_fails_closed_without_backend(self, monkeypatch, tmp_path):
        monkeypatch.setitem(sys.modules, "minisign", None)
        monkeypatch.setattr(plugin_signing.shutil, "which", lambda name: None)
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"x")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(b"x")
        with pytest.raises(plugin_signing.VerifierUnavailableError) as exc:
            plugin_signing.verify_archive(archive, sig, pubkey_path=TEST_PUB)
        assert str(exc.value) == tr("install.verifier_unavailable")

    @pytest.mark.skipif(
        shutil.which("minisign") is None, reason="minisign CLI not installed"
    )
    def test_cli_backend_verifies_valid_signature(self, monkeypatch, tmp_path):
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(_sign(archive.read_bytes()))
        monkeypatch.setitem(sys.modules, "minisign", None)  # force the CLI path
        plugin_signing.verify_archive(archive, sig, pubkey_path=TEST_PUB)

    @pytest.mark.skipif(
        shutil.which("minisign") is None, reason="minisign CLI not installed"
    )
    def test_cli_backend_rejects_tampered(self, monkeypatch, tmp_path):
        archive = tmp_path / "plugin.tar.gz"
        archive.write_bytes(b"archive-data")
        sig = tmp_path / "plugin.tar.gz.minisig"
        sig.write_bytes(_sign(b"other-data"))
        monkeypatch.setitem(sys.modules, "minisign", None)
        with pytest.raises(plugin_signing.SignatureInvalidError):
            plugin_signing.verify_archive(archive, sig, pubkey_path=TEST_PUB)