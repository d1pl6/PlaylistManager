"""Minisign verification of downloaded plugin release archives.

Every catalog install (``--install``, the Manage dialog's install/update)
downloads a plugin repo's **latest GitHub release asset** plus its
detached minisign signature, and - with the default fail-closed policy -
refuses to install unless the signature verifies against the public key
pinned in this repo (``minisign.pub`` at the repo root, tracked).  The
matching SECRET key lives offline with the maintainer and never touches
GitHub: a fully compromised plugin repo or GitHub account still cannot
mint a valid signature, so modified code cannot be installed.

Backends, in order of preference:

1. ``py-minisign`` (the ``minisign`` module, pure-Python on top of
   ``cryptography``) - the primary path for the packaged app.
2. the ``minisign`` CLI (``minisign -Vm <archive> -p <pubkey> -x <sig>``)
   for source checkouts that lack the package.

Policy is FAIL-CLOSED: a missing signature file, an unparseable
signature, a missing or unparseable pinned key, an unavailable verifier
and a failed cryptographic check all raise a ``PluginSignatureError``
(a ``ValueError``) whose message is user-facing (``tr()``).  The only
documented bypass is the ``[security] verify_plugin_signatures = off``
setting in ``cfg/settings.ini`` (see docs/plugins.md "Release signing");
there is no silent path.

Hand-dropped plugins (a directory copied into ``integrations/`` by hand)
never pass through this module - the local filesystem that launches the
app is the trust boundary for local edits.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from utils.i18n import tr

logger = logging.getLogger(__name__)

#: Filename of the pinned public key at the repo root (tracked).  The
#: matching SECRET key is never committed anywhere.
PINNED_PUBKEY_FILENAME = "minisign.pub"

#: Detached-signature suffix appended to a release asset name, matching
#: ``minisign -S`` output (``plugin.tar.gz`` -> ``plugin.tar.gz.minisig``).
SIGNATURE_SUFFIX = ".minisig"

_CLI_TIMEOUT = 30


class PluginSignatureError(ValueError):
    """Base for all plugin-signature failures (messages are user-facing)."""


class PinnedKeyError(PluginSignatureError):
    """The pinned public key is missing, unreadable or unparseable."""


class SignatureMissingError(PluginSignatureError):
    """No usable detached signature file was found."""


class SignatureInvalidError(PluginSignatureError):
    """The signature failed cryptographic verification."""


class VerifierUnavailableError(PluginSignatureError):
    """Neither py-minisign nor the minisign CLI is available."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def pinned_public_key_path() -> Path:
    """Path of the tracked public key that pins plugin authenticity."""
    return _repo_root() / PINNED_PUBKEY_FILENAME


def verifier_available() -> bool:
    """True when at least one verification backend can run."""
    try:
        import minisign  # noqa: F401
        return True
    except ImportError:
        return shutil.which("minisign") is not None


def load_public_key(pubkey_path: Path | None = None):
    """Parse the minisign public key at *pubkey_path* (default: pinned).

    Raises ``VerifierUnavailableError`` when no backend is importable and
    ``PinnedKeyError`` when the file is missing or malformed.
    """
    path = (
        Path(pubkey_path) if pubkey_path is not None else pinned_public_key_path()
    )
    try:
        import minisign
    except ImportError:
        raise VerifierUnavailableError(tr("install.verifier_unavailable"))
    try:
        return minisign.PublicKey.from_file(str(path))
    except Exception as e:
        logger.debug("Could not load public key %s: %s", path, e)
        raise PinnedKeyError(tr("install.signing_key_unavailable")) from e


def verify_archive(
    archive: Path,
    signature: Path,
    pubkey_path: Path | None = None,
) -> None:
    """Verify *signature* over *archive* against the public key (pinned by
    default) and raise ``PluginSignatureError`` on any failure.

    Fail-closed: missing signature file, empty/unparseable signature,
    missing or unparseable key, unavailable verifier and a failed
    cryptographic check all raise with a user-facing message.
    """
    try:
        import minisign  # noqa: F401
    except ImportError:
        _verify_via_cli(archive, signature, pubkey_path)
        return

    pubkey = load_public_key(pubkey_path)
    try:
        sig_data = signature.read_bytes()
    except OSError as e:
        logger.debug("Could not read signature file %s: %s", signature, e)
        raise SignatureMissingError(tr("install.signature_missing")) from e
    if not sig_data:
        raise SignatureMissingError(tr("install.signature_missing"))
    try:
        sig = minisign.Signature.from_bytes(sig_data)
    except Exception as e:
        logger.debug("Unparseable signature file %s: %s", signature, e)
        raise SignatureInvalidError(tr("install.signature_invalid")) from e
    try:
        pubkey.verify(archive.read_bytes(), sig)
    except Exception as e:
        logger.warning(
            "Plugin signature verification failed for %s: %s", archive, e
        )
        raise SignatureInvalidError(tr("install.signature_invalid")) from e


def _verify_via_cli(
    archive: Path, signature: Path, pubkey_path: Path | None
) -> None:
    """Fallback backend: the ``minisign`` CLI (verify-only, file inputs)."""
    cli = shutil.which("minisign")
    if cli is None:
        raise VerifierUnavailableError(tr("install.verifier_unavailable"))
    path = (
        Path(pubkey_path) if pubkey_path is not None else pinned_public_key_path()
    )
    if not path.is_file():
        raise PinnedKeyError(tr("install.signing_key_unavailable"))
    if not signature.is_file():
        raise SignatureMissingError(tr("install.signature_missing"))
    try:
        result = subprocess.run(
            [
                cli,
                "-Vm", str(archive),
                "-p", str(path),
                "-x", str(signature),
            ],
            capture_output=True,
            timeout=_CLI_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("minisign CLI verification failed: %s", e)
        raise SignatureInvalidError(tr("install.signature_invalid")) from e
    if result.returncode != 0:
        logger.warning(
            "Plugin signature verification failed via CLI: %s",
            result.stderr.decode(errors="replace").strip()[:200],
        )
        raise SignatureInvalidError(tr("install.signature_invalid"))