"""
Near-duplicate song detection (pure functions, no tkinter - headless-testable).

Some platform tracks exist under variant names with the same artist
(e.g. "Tales from Tha Guttah" vs "Tales from da Guttah") - different
titles, different uploads, effectively the same recording.  External
metadata services cannot help here (odesli maps per-platform IDs,
MusicBrainz lacks coverage), so matching happens locally:

    same artist + similar title + close duration  =>  near-duplicate

Title similarity is ``difflib.SequenceMatcher.ratio()`` over normalised
titles (diacritic-folded, lowercased, punctuation collapsed).  Edit
distance absorbs slang spelling ("tha" vs "da") that rule-based
normalisation could never cover.  Measured anchors: the Tha/da Guttah
pair scores ~0.93; live/remix variants ("Hallelujah" vs "Halleluja
(Live)") stay <=0.75 - hence the default threshold 0.85.

No word dropping, no parenthetical stripping: "(Live)" / "(Remix)"
suffixes must keep those variants BELOW the threshold, otherwise real
variants would be flagged as duplicates.
"""

from __future__ import annotations

import difflib
import logging
import unicodedata
from typing import List, Optional

from services import duplicate_queue

logger = logging.getLogger(__name__)

# Flows resolve artists heuristically and fall back to this sentinel;
# an unknown artist must never satisfy the artist gate.
UNKNOWN_ARTIST = "Unknown Artist"

# Drift band for upload-length differences beyond [duplicate_check]
# duration_tolerance: distinct uploads of the same recording routinely
# drift 10-20 s (intros/silence, speed).  Inside the band a much stronger
# title ratio is required (see _pair_ratio) so live/remix variants stay
# excluded regardless of gap.
DRIFT_MAX_GAP = 20        # extra seconds allowed past duration_tolerance
DRIFT_RATIO_FLOOR = 0.90  # minimum title ratio inside the drift band


# ----------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    """Fold *text* for comparison: NFKD-diacritic fold, lowercase,
    punctuation -> space, collapse whitespace.

    Keeps alphanumeric characters from any script (``str.isalnum``),
    so non-Latin titles survive intact.
    """
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    stripped = "".join(ch if ch.isalnum() else " " for ch in folded.lower())
    return " ".join(stripped.split())


def _artist_set(artists) -> set[str]:
    """Normalised artist-name set for one side of the comparison."""
    if isinstance(artists, str):
        artists = [artists]
    return {
        _normalize_text(a)
        for a in (artists or [])
        if _normalize_text(a)
    }


def _artists_known(artist_sets: Iterable[set[str]]) -> bool:
    """False when ANY side carries only the Unknown-Artist sentinel.

    The gate must fail closed: with heuristically unresolved artists the
    "shared artist" signal is meaningless and matching would fire on bare
    title collisions ("Intro").
    """
    for names in artist_sets:
        if not names or names <= {_normalize_text(UNKNOWN_ARTIST)}:
            return False
    return True


# ----------------------------------------------------------------------
# Similarity primitives
# ----------------------------------------------------------------------

def title_ratio(title_a: str, title_b: str) -> float:
    """SequenceMatcher ratio over the normalised titles (0.0 .. 1.0)."""
    return difflib.SequenceMatcher(
        None, _normalize_text(title_a), _normalize_text(title_b)
    ).ratio()


def titles_similar(title_a: str, title_b: str, threshold: float = 0.85) -> bool:
    return title_ratio(title_a, title_b) >= threshold


def _pair_ratio(
    title_a, artists_a, dur_a,
    title_b, artists_b, dur_b,
    *,
    threshold: float,
    duration_tolerance: int,
) -> Optional[float]:
    """Similarity for two songs, or None when a gate fails."""
    set_a = _artist_set(artists_a)
    set_b = _artist_set(artists_b)
    # Artist gate: >=1 shared normalised artist name.
    if not (set_a & set_b):
        return None
    if not _artists_known((set_a, set_b)):
        return None

    ratio = title_ratio(title_a, title_b)

    try:
        int_a, int_b = int(dur_a or 0), int(dur_b or 0)
    except (TypeError, ValueError):
        int_a = int_b = 0
    if not int_a or not int_b:
        # Unknown length on either side: the title bar alone decides -
        # a missing duration should weaken the evidence, not disable
        # matching entirely.
        return ratio if ratio >= threshold else None

    gap = abs(int_a - int_b)
    if gap <= duration_tolerance:
        return ratio if ratio >= threshold else None

    # Drift band.  Distinct uploads of the SAME recording routinely sit
    # further apart than the tight band (extra intros/silence, slight
    # speed differences - real cases at +12 s).  Inside this wider band
    # a much stronger title signal is required, so live/remix variants
    # ("Hallelujah" vs "... (Live)", ratio ~0.75) stay excluded no matter
    # their gap.
    if (
        gap <= duration_tolerance + DRIFT_MAX_GAP
        and ratio >= max(threshold, DRIFT_RATIO_FLOOR)
    ):
        return ratio
    return None


# ----------------------------------------------------------------------
# Public queries
# ----------------------------------------------------------------------

def find_similar(
    songs: List[dict],
    title: str,
    artists,
    duration,
    *,
    threshold: float = 0.85,
    duration_tolerance: int = 5,
) -> Optional[dict]:
    """Best near-duplicate of (title, artists, duration) inside *songs*.

    Returns a copy of the winning song dict with an extra ``"similarity"``
    key, or None when nothing matches.  Song dicts need title / artists /
    duration keys (the shape ``SongManager.get_all_songs`` returns).
    """
    best: Optional[tuple[float, dict]] = None
    for song in songs:
        try:
            ratio = _pair_ratio(
                title, artists, duration,
                song.get("title"), song.get("artists"), song.get("duration"),
                threshold=threshold,
                duration_tolerance=duration_tolerance,
            )
        except Exception:
            logger.debug(
                "duplicate_check: skipping malformed row %r", song, exc_info=True
            )
            continue
        if ratio is not None and (best is None or ratio > best[0]):
            best = (ratio, song)
    if best is None:
        return None
    match = dict(best[1])
    match["similarity"] = round(best[0], 4)
    return match


def find_duplicate_pairs(
    songs: List[dict],
    *,
    threshold: float = 0.85,
    duration_tolerance: int = 5,
) -> List[dict]:
    """All near-duplicate pairs within one playlist's songs.

    *songs* must be ordered newest-first (the ``get_all_songs``
    ``ORDER BY added_at DESC`` ordering) so each pair can carry which
    side is the newer addition.  Returns a list of
    ``{"newer": song, "older": song, "similarity": ratio}``, most
    similar pair first.  O(N^2) with a cheap duration pre-screen - fine
    for playlists of up to a few thousand rows.
    """
    rows = []
    for song in songs:
        rows.append(
            (
                _normalize_text(song.get("title", "")),
                _artist_set(song.get("artists")),
                int(song.get("duration") or 0),
                song,
            )
        )

    unknown_unknown = _normalize_text(UNKNOWN_ARTIST)
    pairs: List[dict] = []
    n = len(rows)
    for i in range(n):
        nt_a, set_a, dur_a, song_a = rows[i]
        if not set_a or set_a <= {unknown_unknown}:
            continue
        for j in range(i + 1, n):
            nt_b, set_b, dur_b, song_b = rows[j]
            if not set_b or set_b <= {unknown_unknown}:
                continue
            if not (set_a & set_b):
                continue
            ratio = _pair_ratio(
                song_a.get("title"), song_a.get("artists"), dur_a,
                song_b.get("title"), song_b.get("artists"), dur_b,
                threshold=threshold,
                duration_tolerance=duration_tolerance,
            )
            if ratio is None:
                continue
            pairs.append(
                {
                    # i precedes j in the newest-first input order.
                    "newer": song_a,
                    "older": song_b,
                    "similarity": round(ratio, 4),
                }
            )
    pairs.sort(key=lambda p: p["similarity"], reverse=True)
    return pairs


# ----------------------------------------------------------------------
# Settings plumbing
# ----------------------------------------------------------------------

def read_settings() -> tuple[bool, float, int]:
    """(enabled, title_threshold, duration_tolerance) from settings.ini.

    The config import is lazy so this module stays importable without
    any side effects; malformed knob values fall back to the defaults.
    """
    from utils.config import get_setting, get_setting_value

    enabled = get_setting("duplicate_check", fallback=False)
    try:
        threshold = float(get_setting_value("duplicate_check", "title_threshold", "0.85"))
    except (TypeError, ValueError):
        threshold = 0.85
    try:
        tolerance = int(get_setting_value("duplicate_check", "duration_tolerance", "5"))
    except (TypeError, ValueError):
        tolerance = 5
    return enabled, threshold, tolerance


def resolve_near_duplicate(
    *,
    songs: List[dict],
    title: str,
    artists,
    duration,
    track_id: str,
    platform: str,
    playlist_id: str,
    playlist_name: str,
    url: Optional[str] = None,
    thumbnail: Optional[str] = None,
) -> tuple[str, Optional[dict]]:
    """The add-path duplicate policy, shared by every platform flow.

    Returns ``(action, match)`` where *action* is:

    ``"proceed"``
        No near-duplicate, the pair was already approved (``added``), or
        it is whitelisted (``not_duplicate``) - caller adds normally.
    ``"skip"``
        The pair was previously dismissed - caller reports an
        exists-like result and adds nothing.
    ``"queued"``
        Fresh match: a song record is parked in db/extra.json and
        the caller reports the ``"duplicate"`` status WITHOUT adding
        anywhere (platform-first invariant holds trivially - no platform
        call has happened yet).
    """
    enabled, threshold, tolerance = read_settings()
    if not enabled:
        return "proceed", None

    match = find_similar(
        songs, title, artists, duration,
        threshold=threshold, duration_tolerance=tolerance,
    )
    if match is None:
        return "proceed", None

    pair_key = duplicate_queue.make_pair_key(
        platform, playlist_id or "", track_id, str(match.get("track_id") or "")
    )
    song = (duplicate_queue.get_song(pair_key) or {}).get("song")
    if song in ("added", "not_duplicate"):
        return "proceed", match
    if song == "dismissed":
        return "skip", match

    duplicate_queue.add_pending(
        {
            # pair_key rides on the record so the activity window's
            # actions can record the song without recomputing it.
            "pair_key": pair_key,
            "playlist_name": playlist_name,
            "platform": platform,
            "playlist_id": playlist_id or "",
            "url": url,
            "track_id": track_id,
            "title": title,
            "artists": list(artists or []),
            "duration": duration,
            "thumbnail_url": thumbnail,
            "existing": {
                "track_id": match.get("track_id"),
                "title": match.get("title"),
                "artists": list(match.get("artists") or []),
                "duration": match.get("duration"),
            },
            "similarity": match.get("similarity"),
        }
    )
    logger.info(
        "Near-duplicate queued for '%s' vs '%s' (%.2f) in %s",
        title, match.get("title"), match.get("similarity", 0.0), playlist_name,
    )
    return "queued", match
