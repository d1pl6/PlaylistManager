"""
Download / uninstall platform integrations (the plugin repos).

GitHub hosts one repo per platform ("<platform>-integration"); each repo
carries its plugin package under ``integrations/<platform>/`` and downloads
are installed straight into ``<repo_root>/integrations/<platform>/`` where
``plugin_loader`` discovers them.  The network work (``download_integration``)
is designed to run on a worker thread - it never touches tkinter.

Uninstall is the "with database, etc." cleanup behind the Manage dialog and
mirrors ``cli.run_logout`` plus the plugin directory: credentials, playlist
registry entries and per-platform song databases are deleted before the
plugin folder itself is removed.
"""

from __future__ import annotations

import json
import logging
import shutil
import tarfile
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from plugin_loader import PluginRegistry
from services import auth_setup, duplicate_queue
from services.database import DatabaseManager
from services.playlist_store import PlaylistStore

logger = logging.getLogger(__name__)

# Serialises downloads so two threads cannot install into the same
# directory concurrently (the UI only ever starts one, but the CLI could
# share this module later).
_install_lock = threading.Lock()

# Hard cap for a plugin archive - the repos are a few KB; anything larger
# is almost certainly not the plugin we are expecting.
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024

_DOWNLOAD_TIMEOUT = 60


@dataclass(frozen=True)
class IntegrationRepo:
    """Download source for one platform plugin.

    ``copies`` maps paths inside the repo tree (relative to the archive
    root) to destinations relative to ``<repo_root>/integrations/``.
    Example - YouTube Music keeps the browser extension at the repo root
    while the plugin package sits under ``integrations/youtube_music/``;
    both must end up inside ``integrations/youtube_music/`` because the
    receiver port is pinned jointly by the plugin manifest and the
    extension's ``host_permissions`` (see AGENTS.md "Integration quirks").
    """

    platform_id: str
    display_name: str
    owner: str
    repo: str
    copies: Tuple[Tuple[str, str], ...]

    @property
    def download_url(self) -> str:
        """codeload tarball of the default branch (no API, no auth)."""
        return (
            f"https://codeload.github.com/{self.owner}/{self.repo}"
            "/tar.gz/refs/heads/main"
        )

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"


# The catalog of downloadable integrations.  Keys are plugin ids (the
# stable data keys used in plugin.json / db/playlists.json / keybinds).
INTEGRATION_REPOS: Dict[str, IntegrationRepo] = {
    "youtube_music": IntegrationRepo(
        platform_id="youtube_music",
        display_name="YouTube Music",
        owner="d1pl6",
        repo="youtube-music-integration",
        copies=(
            ("integrations/youtube_music", "youtube_music"),
            # Extension lives at the repo root but installs INSIDE the
            # plugin directory - see the dataclass docstring.
            (
                "youtube-music-extension",
                "youtube_music/youtube-music-extension",
            ),
        ),
    ),
    "spotify": IntegrationRepo(
        platform_id="spotify",
        display_name="Spotify",
        owner="d1pl6",
        repo="spotify-integration",
        copies=(
            ("integrations/spotify", "spotify"),
        ),
    ),
    "lastfm": IntegrationRepo(
        platform_id="lastfm",
        display_name="Last.fm",
        owner="d1pl6",
        repo="lastfm-integration",
        copies=(
            ("integrations/lastfm", "lastfm"),
        ),
    ),
}


# ---------------------------------------------------------------------------
# Download / install
# ---------------------------------------------------------------------------


def installable_ids() -> List[str]:
    """Platform ids that have a download source (catalog order)."""
    return list(INTEGRATION_REPOS)


def download_integration(platform_id: str) -> List[Path]:
    """Download + install one integration; returns the installed paths.

    Runs on the calling thread (intended: a worker thread).  Downloads
    the repo tarball to a temp dir, extracts it safely (no path
    traversal, no symlinks), validates the plugin manifest inside, then
    copies each mapped directory into ``integrations/`` via a temp-sibling
    swap so a mid-copy failure never leaves a half-written plugin dir.

    Raises ``ValueError`` for unknown platforms / manifest mismatches and
    ``OSError``/``urllib`` errors for network or filesystem failures.
    """
    repo = INTEGRATION_REPOS.get(platform_id)
    if repo is None:
        raise ValueError(f"No download source for platform '{platform_id}'")

    with _install_lock:
        base = PluginRegistry().base_dir
        base.mkdir(parents=True, exist_ok=True)
        base_resolved = base.resolve()

        with tempfile.TemporaryDirectory(prefix="pm_integration_") as tmp_str:
            tmp = Path(tmp_str)
            archive = tmp / "repo.tar.gz"
            _download_archive(repo.download_url, archive)
            stage = tmp / "tree"
            stage.mkdir()
            with tarfile.open(archive, "r:gz") as tar:
                _safe_extract(tar, stage)
            # GitHub (codeload) tarballs wrap the tree in a single
            # "<repo>-<commit>/" directory - unwrap it so the copy paths
            # below are relative to the archive root.
            entries = [p for p in stage.iterdir()]
            if len(entries) == 1 and entries[0].is_dir():
                stage = entries[0]

            installed: List[Path] = []
            for src_rel, dst_rel in repo.copies:
                src = stage.joinpath(*src_rel.split("/"))
                dst = base.joinpath(*dst_rel.split("/"))
                _validate_install_target(dst, base_resolved)
                if not src.is_dir():
                    raise FileNotFoundError(
                        f"Repository {repo.repo} is missing '{src_rel}' - "
                        f"cannot install '{platform_id}'"
                    )
                # Only the plugin package itself carries the manifest;
                # verify its id matches so a re-purposed repo can never
                # install under a foreign platform id.
                manifest = src / "plugin.json"
                if src_rel.split("/")[-1] == platform_id and manifest.is_file():
                    _validate_plugin_manifest(manifest, platform_id)
                _replace_dir(src, dst)
                installed.append(dst)
                logger.info(
                    "Installed %s integration files into %s", platform_id, dst
                )
            return installed


def _download_archive(url: str, dest: Path) -> None:
    """Fetch *url* into *dest* with a size cap and timeout."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PlaylistManager-integration-manager/1.0"},
    )
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as resp, open(
        dest, "wb"
    ) as out:
        copied = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            copied += len(chunk)
            if copied > _MAX_ARCHIVE_BYTES:
                raise OSError(
                    f"Downloaded archive exceeds {_MAX_ARCHIVE_BYTES} bytes - "
                    "refusing to install"
                )
            out.write(chunk)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract *tar* into *dest*, rejecting traversal, links and devices.

    Manual member checks instead of ``tarfile``'s ``filter=`` because the
    app still supports Python 3.10/3.11 (``filter`` is 3.12+).  GitHub
    tarballs contain only regular files and directories, so every other
    member type is skipped.
    """
    dest = dest.resolve()
    for member in tar.getmembers():
        name = Path(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError(f"Unsafe archive member: {member.name!r}")
        if not (member.isfile() or member.isdir()):
            logger.debug("Skipping non-regular archive member: %s", member.name)
            continue
        target = (dest / name).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError(f"Archive member escapes extraction dir: {member.name!r}")
        tar.extract(member, dest, set_attrs=False)


def _validate_install_target(dst: Path, base: Path) -> None:
    """Refuse destinations that escape ``integrations/``."""
    candidate = dst.resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"Install path escapes integrations dir: {dst}")


def _validate_plugin_manifest(manifest: Path, expected_id: str) -> None:
    """The downloaded plugin.json must declare the expected platform id."""
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"Invalid plugin.json in download: {e}") from e
    declared = raw.get("id", "")
    if declared != expected_id:
        raise ValueError(
            f"Download does not contain the '{expected_id}' plugin "
            f"(manifest declares id {declared!r}) - refusing to install"
        )


def _replace_dir(src: Path, dst: Path) -> None:
    """Copy *src* onto *dst* atomically-ish (temp sibling + rename)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = dst.parent / f".{dst.name}.install_tmp"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(
        src,
        staging,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git*"),
    )
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    staging.rename(dst)


# ---------------------------------------------------------------------------
# Uninstall ("with database, etc.")
# ---------------------------------------------------------------------------


def uninstall_platform_data(
    platform_id: str, plugin=None
) -> Dict[str, int]:
    """Delete all local data for *platform* and remove its plugin folder.

    Order matters: credentials, playlist registry entries, per-platform
    song databases and the duplicate-queue records go first, then the
    plugin directory itself.  Every step is idempotent and best-effort
    (per-file errors are logged, not raised), matching ``cli.run_logout``;
    a reusable report dict with removed counts is returned:

    ``credentials`` auth files deleted
    ``playlists``  registry entries removed
    ``databases``  song DB / WAL files removed
    ``pending`` / ``songs`` / ``errors`` duplicate-queue records removed
    ``plugin_dirs`` plugin folders removed

    The UI layer is responsible for stopping the URL receiver, dropping
    the platform's flow and unregistering the live integration/plugin
    registry objects; this service only touches disk (+ the store and
    DatabaseManager, which are likewise service-layer).
    """
    report = {
        "credentials": 0,
        "playlists": 0,
        "databases": 0,
        "pending": 0,
        "songs": 0,
        "errors": 0,
        "plugin_dirs": 0,
    }

    # Credentials: every manifest-declared path (auth-dir file + declared
    # fallback copies) when the plugin is still known, else the hardcoded
    # map used by the CLI (same files).  Removing a platform must delete
    # all of them - a surviving fallback browser.json would otherwise
    # re-authenticate the plugin the next time it is downloaded.
    if plugin is not None:
        cred_files = list(plugin.auth_paths)
        # Defense in depth: a plugin whose manifest declares nothing (or
        # whose auth_file failed validation) still gets its hardcoded map
        # entries for the known platforms.
        for p in auth_setup.PLATFORM_CREDENTIAL_FILES.get(platform_id, []):
            if p not in cred_files:
                cred_files.append(p)
    else:
        cred_files = list(auth_setup.PLATFORM_CREDENTIAL_FILES.get(platform_id, []))
    for path in cred_files:
        try:
            if path.exists():
                path.unlink()
                report["credentials"] += 1
                logger.info("Deleted credentials %s", path)
        except OSError as e:
            logger.warning("Failed to delete credentials %s: %s", path, e)

    report["playlists"] = PlaylistStore.delete_playlists_for_platform(platform_id)

    report["databases"] = DatabaseManager.delete_platform_databases(platform_id)

    n_pending, n_songs, n_errors = duplicate_queue.purge_platform(platform_id)
    report["pending"], report["songs"], report["errors"] = (
        n_pending,
        n_songs,
        n_errors,
    )

    report["plugin_dirs"] = _remove_plugin_directories(platform_id)
    return report


def _remove_plugin_directories(platform_id: str) -> int:
    """Remove every ``integrations/*/`` dir whose manifest id matches.

    Scans disk rather than trusting the registry so an uninstallation
    whose PluginInfo was already dropped still removes the folder.
    """
    base = PluginRegistry().base_dir
    if not base.is_dir():
        return 0
    removed = 0
    for manifest in sorted(base.glob("*/plugin.json")):
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if raw.get("id") != platform_id:
            continue
        target = manifest.parent
        if target == base or target.name in ("", ".", ".."):
            logger.warning(
                "Refusing to remove suspicious integration directory: %s", target
            )
            continue
        try:
            shutil.rmtree(target)
            logger.info("Removed integration directory %s", target)
            removed += 1
        except OSError as e:
            logger.warning("Failed to remove %s: %s", target, e)
    return removed