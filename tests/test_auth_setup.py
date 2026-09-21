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