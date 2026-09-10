"""Thumbnail data-saver mode tests (headless).

Covers the ``[thumbnails] mode`` settings (off / download / dedupe /
cache / max), the ``--data-saver`` override (``thumbnail.DATA_SAVER``),
disk persistence, the dedupe index + duplicate-check integration,
cache-mode pruning, and the clear / size helpers.

No live network: ``utils.thumbnail._session.get`` is faked per test and
the cache tree is pinned under ``tmp_path`` per test (conftest keeps the
module globals sandboxed process-wide as a backstop).
"""

import json
from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

import utils.config as config
import utils.thumbnail as thumb
from utils.thumbnail import ThumbnailService

IMG_URL = "https://cdn.example.com/a.jpg"


def _fake_png(size=(16, 16), color=(120, 60, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _fake_session(*, fail: bool = False):
    import requests

    session = Mock()
    if fail:
        session.get = Mock(side_effect=requests.RequestException("boom"))
    else:
        resp = Mock()
        resp.content = _fake_png()
        resp.raise_for_status = Mock()
        session.get = Mock(return_value=resp)
    return session


@pytest.fixture(autouse=True)
def _thumb_env(monkeypatch, tmp_path):
    """Per-test cache tree, clean in-memory cache, default settings."""
    cache_root = tmp_path / "cache"
    for attr, value in (
        ("CACHE_ROOT", cache_root),
        ("_PLAYLIST_DIR", cache_root / "playlists"),
        ("_SONG_DIR", cache_root / "songs"),
        ("_FULL_DIR", cache_root / "full"),
        ("_DEDUPE_INDEX", cache_root / "songs" / "index.json"),
        ("DATA_SAVER", False),
    ):
        monkeypatch.setattr(thumb, attr, value)
    with ThumbnailService._cache_lock:
        ThumbnailService._cache.clear()
    monkeypatch.setattr(thumb, "_session", _fake_session())
    config.set_setting_value("thumbnails", "mode", "off")
    config.set_setting_value("duplicate_check", "is_true", "false")
    config.set_setting_value("duplicate_check", "title_threshold", "0.85")
    config.set_setting_value("duplicate_check", "duration_tolerance", "5")
    return cache_root


def _set_mode(mode: str) -> None:
    config.set_setting_value("thumbnails", "mode", mode)


def _set_dupcheck(enabled: bool) -> None:
    config.set_setting_value(
        "duplicate_check", "is_true", "true" if enabled else "false"
    )


def _clear_memory_cache() -> None:
    with ThumbnailService._cache_lock:
        ThumbnailService._cache.clear()


def _song_files(cache_root) -> list:
    return sorted((cache_root / "songs").glob("*.png")) if (cache_root / "songs").exists() else []


# SongManager-row-shaped dicts (title / artists / duration / thumbnail_url).
# Near-dup title pair borrowed from test_duplicate_check.py: "Tha"/"da"
# Guttah normalize differently yet score > 0.85, so they separate the
# fuzzy (dup-check ON) from the exact (OFF) matching paths.
SONG_A = {"title": "Tales from Tha Guttah", "artists": ["Killah Priest"], "duration": 200,
          "thumbnail_url": IMG_URL}
SONG_NEAR = {"title": "Tales from da Guttah", "artists": ["Killah Priest"], "duration": 204,
             "thumbnail_url": "https://cdn.example.com/near.jpg"}
SONG_IDENTICAL = {"title": "Tales from Tha Guttah", "artists": ["Killah Priest"], "duration": 200,
                  "thumbnail_url": "https://cdn.example.com/other.jpg"}
SONG_DRIFT = {"title": "Tales from Tha Guttah", "artists": ["Killah Priest"], "duration": 212,
              "thumbnail_url": "https://cdn.example.com/drift.jpg"}
SONG_DRIFT_FAR = {"title": "Tales from Tha Guttah", "artists": ["Killah Priest"], "duration": 226,
                  "thumbnail_url": "https://cdn.example.com/driftfar.jpg"}
SONG_FAR = {"title": "Nothing Alike At All", "artists": ["Killah Priest"], "duration": 90,
            "thumbnail_url": "https://cdn.example.com/far.jpg"}
SONG_UNKNOWN = {"title": "Intro", "artists": ["Unknown Artist"], "duration": 30,
                "thumbnail_url": "https://cdn.example.com/u.png"}
SONG_UNKNOWN2 = {"title": "Outro", "artists": ["Unknown Artist"], "duration": 25,
                 "thumbnail_url": "https://cdn.example.com/u2.png"}


# ---------------------------------------------------------------------------
# Mode basics
# ---------------------------------------------------------------------------

def test_off_mode_fetches_live_without_disk_writes(_thumb_env):
    _set_mode("off")
    img = ThumbnailService.fetch_image(IMG_URL, (64, 64))
    assert img is not None
    assert ThumbnailService.fetch_song_image(SONG_A) is not None
    for d in ("playlists", "songs"):
        assert not (_thumb_env / d).exists(), f"{d}/ must stay empty in off mode"


def test_unknown_mode_falls_back_to_off(_thumb_env):
    config.set_setting_value("thumbnails", "mode", "turbo")
    assert ThumbnailService.fetch_image(IMG_URL, (64, 64)) is not None
    assert not (_thumb_env / "playlists").exists()


def test_max_mode_returns_none_without_network(_thumb_env):
    _set_mode("max")
    assert ThumbnailService.fetch_image(IMG_URL, (64, 64)) is None
    assert ThumbnailService.fetch_song_image(SONG_A) is None
    assert ThumbnailService.fetch_full_image(IMG_URL) is None
    thumb._session.get.assert_not_called()
    for d in ("playlists", "songs", "full"):
        assert not (_thumb_env / d).exists()


def test_data_saver_flag_overrides_to_max(_thumb_env, monkeypatch):
    _set_mode("download")
    monkeypatch.setattr(thumb, "DATA_SAVER", True)
    assert ThumbnailService.fetch_song_image(SONG_A) is None
    thumb._session.get.assert_not_called()
    assert not (_thumb_env / "songs").exists()


def test_song_without_thumbnail_url_returns_none(_thumb_env):
    _set_mode("download")
    assert ThumbnailService.fetch_song_image({"title": "X", "artists": ["Y"]}) is None
    assert ThumbnailService.fetch_song_image(None) is None


# ---------------------------------------------------------------------------
# download mode: persistent disk cache
# ---------------------------------------------------------------------------

def test_download_mode_persists_and_serves_from_disk(_thumb_env, monkeypatch):
    _set_mode("download")
    assert ThumbnailService.fetch_song_image(SONG_A) is not None
    assert len(_song_files(_thumb_env)) == 1

    # Kill the network AND the in-memory cache: a fresh render must be
    # served purely from disk.
    monkeypatch.setattr(thumb, "_session", _fake_session(fail=True))
    _clear_memory_cache()
    img = ThumbnailService.fetch_song_image(SONG_A)
    assert img is not None
    thumb._session.get.assert_not_called()

    # Covers land under playlists/ and are disk-served too.
    monkeypatch.setattr(thumb, "_session", _fake_session())
    assert ThumbnailService.fetch_image(IMG_URL, (64, 64)) is not None
    assert len(list((_thumb_env / "playlists").glob("*.png"))) == 1
    monkeypatch.setattr(thumb, "_session", _fake_session(fail=True))
    _clear_memory_cache()
    assert ThumbnailService.fetch_image(IMG_URL, (64, 64)) is not None


def test_corrupt_disk_file_is_refetched_and_healed(_thumb_env):
    _set_mode("download")
    ThumbnailService.fetch_song_image(SONG_A)
    path = _song_files(_thumb_env)[0]
    path.write_bytes(b"not an image")
    _clear_memory_cache()
    img = ThumbnailService.fetch_song_image(SONG_A)
    assert img is not None
    # Corrupt file deleted, fresh copy stored.
    from PIL import Image as PILImage

    with PILImage.open(path) as opened:
        opened.verify()


def test_prefetch_song_thumbs(_thumb_env):
    _set_mode("download")
    ThumbnailService.prefetch_song_thumbs([SONG_A, SONG_FAR])
    assert len(_song_files(_thumb_env)) == 2
    # No-op outside download mode.
    thumb._session.get.reset_mock()
    _set_mode("off")
    ThumbnailService.prefetch_song_thumbs([SONG_A, SONG_FAR])
    thumb._session.get.assert_not_called()


def test_clear_disk_cache_and_size(_thumb_env):
    _set_mode("download")
    ThumbnailService.fetch_song_image(SONG_A)
    ThumbnailService.fetch_image(IMG_URL, (64, 64))
    ThumbnailService.fetch_full_image(IMG_URL)
    assert ThumbnailService.disk_cache_size() > 0
    ThumbnailService.clear_disk_cache()
    assert ThumbnailService.disk_cache_size() == 0
    for d in ("playlists", "songs", "full"):
        assert not (_thumb_env / d).exists()


def test_clear_cache_for_removes_disk_entry(_thumb_env, monkeypatch):
    _set_mode("download")
    assert ThumbnailService.fetch_full_image(IMG_URL) is not None
    assert len(list((_thumb_env / "full").glob("*.png"))) == 1
    ThumbnailService.clear_cache_for(IMG_URL, None)
    assert not list((_thumb_env / "full").glob("*.png"))
    monkeypatch.setattr(thumb, "_session", _fake_session(fail=True))
    assert ThumbnailService.fetch_full_image(IMG_URL) is None


# ---------------------------------------------------------------------------
# dedupe mode: duplicate-check integration
# ---------------------------------------------------------------------------

def test_dedupe_reuses_near_duplicate_song(_thumb_env):
    """duplicate-check ON: a near-dup title shares one downloaded image."""
    _set_mode("dedupe")
    _set_dupcheck(True)
    assert ThumbnailService.fetch_song_image(SONG_A) is not None
    assert ThumbnailService.fetch_song_image(SONG_NEAR) is not None
    thumb._session.get.assert_called_once()
    assert len(_song_files(_thumb_env)) == 1


def test_dedupe_exact_matching_when_dupcheck_off(_thumb_env):
    _set_mode("dedupe")
    _set_dupcheck(False)
    assert ThumbnailService.fetch_song_image(SONG_A) is not None
    assert ThumbnailService.fetch_song_image(SONG_NEAR) is not None
    assert thumb._session.get.call_count == 2
    assert len(_song_files(_thumb_env)) == 2


def test_dedupe_identical_song_reused_always(_thumb_env):
    """Even with dup-check OFF, exact title+artist+duration reuse works."""
    _set_mode("dedupe")
    _set_dupcheck(False)
    assert ThumbnailService.fetch_song_image(SONG_A) is not None
    assert ThumbnailService.fetch_song_image(SONG_IDENTICAL) is not None
    thumb._session.get.assert_called_once()
    assert len(_song_files(_thumb_env)) == 1


def test_dedupe_drift_band(_thumb_env):
    _set_mode("dedupe")
    _set_dupcheck(True)
    ThumbnailService.fetch_song_image(SONG_A)
    # +12 s gap: inside the drift band, identical title -> reuse.
    assert ThumbnailService.fetch_song_image(SONG_DRIFT) is not None
    assert thumb._session.get.call_count == 1
    # +26 s gap: outside tolerance + band -> different recording, refetch.
    assert ThumbnailService.fetch_song_image(SONG_DRIFT_FAR) is not None
    assert thumb._session.get.call_count == 2


def test_dedupe_unknown_artist_never_reused(_thumb_env):
    _set_mode("dedupe")
    _set_dupcheck(True)
    ThumbnailService.fetch_song_image(SONG_A)
    ThumbnailService.fetch_song_image(SONG_UNKNOWN)
    ThumbnailService.fetch_song_image(SONG_UNKNOWN2)
    assert thumb._session.get.call_count == 3
    assert len(_song_files(_thumb_env)) == 3


def test_dedupe_index_invalid_json_recovers(_thumb_env):
    _set_mode("dedupe")
    _set_dupcheck(True)
    ThumbnailService.fetch_song_image(SONG_A)
    (thumb._SONG_DIR / "index.json").write_text("{broken", encoding="utf-8")
    _clear_memory_cache()
    img = ThumbnailService.fetch_song_image(SONG_A)
    assert img is not None
    assert thumb._session.get.call_count == 2  # refetched after recovery


# ---------------------------------------------------------------------------
# cache mode: bounded to what's visible, pruned after refresh
# ---------------------------------------------------------------------------

def test_cache_mode_prunes_unreferenced_song_thumbs(_thumb_env):
    _set_mode("cache")
    ThumbnailService.fetch_song_image(SONG_A)
    ThumbnailService.fetch_song_image(SONG_FAR)
    assert len(_song_files(_thumb_env)) == 2
    ThumbnailService.prune_song_cache({SONG_A["thumbnail_url"]})
    assert [p.name for p in _song_files(_thumb_env)] == [
        ThumbnailService._disk_key(SONG_A["thumbnail_url"], thumb.SONG_THUMB_SIZE) + ".png"
    ]


def test_prune_noop_outside_cache_mode(_thumb_env):
    _set_mode("download")
    ThumbnailService.fetch_song_image(SONG_A)
    ThumbnailService.fetch_song_image(SONG_FAR)
    ThumbnailService.prune_song_cache(set())
    assert len(_song_files(_thumb_env)) == 2


def test_prune_drops_orphaned_dedupe_rows(_thumb_env):
    _set_mode("dedupe")
    _set_dupcheck(True)
    ThumbnailService.fetch_song_image(SONG_A)
    assert len(ThumbnailService._load_dedupe_index()) == 1
    # Switching to cache mode orphans identity-keyed files -> pruned rows too.
    _set_mode("cache")
    ThumbnailService.prune_song_cache(set())
    assert ThumbnailService._load_dedupe_index() == []