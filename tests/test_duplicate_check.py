"""Unit tests for services/duplicate_check.py — near-duplicate matching.

Pure functions (no tkinter, no config needed for the matching core);
``resolve_near_duplicate`` tests monkeypatch ``read_settings`` and point
``duplicate_queue.extra_json`` at a temp file.
"""

import pytest

from services import duplicate_check as dc
from services import duplicate_queue


def _song(title, artists="Artist", duration=200, track_id="t"):
    return {"title": title, "artists": artists, "duration": duration,
            "track_id": track_id}


class TestNormalizeText:
    def test_lowercase_and_strip(self):
        assert dc._normalize_text("  Hello World  ") == "hello world"

    def test_punctuation_to_space(self):
        assert dc._normalize_text("Tales from da Guttah!") == "tales from da guttah"

    def test_diacritics_folded(self):
        assert dc._normalize_text("Café") == "cafe"

    def test_whitespace_collapsed(self):
        assert dc._normalize_text("a   b\t\tc\n") == "a b c"

    def test_empty_and_none(self):
        assert dc._normalize_text("") == ""
        assert dc._normalize_text(None) == ""

    def test_non_latin_kept(self):
        assert dc._normalize_text("Привет мир") == "привет мир"


class TestArtistSet:
    def test_string_input_wrapped(self):
        assert dc._artist_set("Artist") == {"artist"}

    def test_list_input(self):
        assert dc._artist_set(["A", "B"]) == {"a", "b"}

    def test_unknown_artist_normalized(self):
        assert dc._artist_set(["Unknown Artist"]) == {"unknown artist"}

    def test_empty_variants(self):
        assert dc._artist_set(None) == set()
        assert dc._artist_set([]) == set()
        assert dc._artist_set([""]) == set()

    def test_emoji_or_symbol_only_dropped(self):
        # "!!!" normalizes to "" and must not produce an empty-name key.
        assert dc._artist_set(["!!!"]) == set()


class TestArtistsKnown:
    def test_all_known(self):
        assert dc._artists_known([{"a"}, {"a", "b"}]) is True

    def test_unknown_artist_fails_closed(self):
        assert dc._artists_known([{"unknown artist"}]) is False
        assert dc._artists_known([{"a"}, {"unknown artist"}]) is False

    def test_empty_set_fails_closed(self):
        assert dc._artists_known([set(), {"a"}]) is False


class TestTitleSimilarity:
    def test_identical(self):
        assert dc.title_ratio("Same Title", "Same Title") == 1.0
        assert dc.titles_similar("Same Title", "Same Title") is True

    def test_tha_da_variant_above_threshold(self):
        # The measured anchor pair from the module docs: ~0.93.
        assert dc.title_ratio("Tales from Tha Guttah", "Tales from da Guttah") > 0.85

    def test_live_variant_below_threshold(self):
        assert dc.title_ratio("Hallelujah", "Hallelujah (Live)") < 0.85

    def test_titles_similar_threshold_respected(self):
        assert dc.titles_similar("abc", "abc", threshold=0.5) is True
        assert dc.titles_similar("abc", "xyz", threshold=0.5) is False

    def test_empty_titles(self):
        assert dc.title_ratio("", "") == 1.0
        assert dc.title_ratio("x", "") == 0.0


class TestPairRatio:
    def test_identical_songs(self):
        assert dc._pair_ratio(
            "Song", "Artist", 200, "Song", "Artist", 200,
            threshold=0.85, duration_tolerance=5,
        ) == 1.0

    def test_different_artist_fails_gate(self):
        assert dc._pair_ratio(
            "Song", "Artist A", 200, "Song", "Artist B", 200,
            threshold=0.85, duration_tolerance=5,
        ) is None

    def test_unknown_artist_fails_closed(self):
        assert dc._pair_ratio(
            "Intro", "Unknown Artist", 200, "Intro", "Unknown Artist", 200,
            threshold=0.85, duration_tolerance=5,
        ) is None

    def test_duration_gap_inside_tolerance(self):
        assert dc._pair_ratio(
            "Song A", "Artist", 200, "Song B", "Artist", 205,
            threshold=0.5, duration_tolerance=5,
        ) is not None

    def test_duration_gap_beyond_tolerance_and_ratio_low(self):
        assert dc._pair_ratio(
            "Completely Different Title", "Artist", 200, "Song B", "Artist", 260,
            threshold=0.85, duration_tolerance=5,
        ) is None

    def test_duration_gap_in_drift_band_high_ratio(self):
        # 12s gap but near-identical title → drift band admits it.
        assert dc._pair_ratio(
            "Tales from Tha Guttah", "Artist", 212, "Tales from da Guttah", "Artist", 200,
            threshold=0.85, duration_tolerance=5,
        ) >= 0.90

    def test_duration_gap_in_drift_band_low_ratio_rejected(self):
        # 12s gap but ratio below the drift floor → not a match.
        assert dc._pair_ratio(
            "Tales from Tha Guttah", "Artist", 212, "Tales from da Guttah (Live)", "Artist", 200,
            threshold=0.85, duration_tolerance=5,
        ) is None

    def test_missing_duration_uses_title_only(self):
        assert dc._pair_ratio(
            "Song A", "Artist", None, "Song B", "Artist", 0,
            threshold=0.5, duration_tolerance=5,
        ) is not None

    def test_shared_artist_among_multi(self):
        assert dc._pair_ratio(
            "Song", ["Artist A", "Artist B"], 200,
            "Song", ["Artist B", "Artist C"], 200,
            threshold=0.85, duration_tolerance=5,
        ) == 1.0


class TestFindSimilar:
    def _songs(self):
        return [
            _song("Tales from Tha Guttah", "Killah Priest", 212, "t1"),
            _song("Something Different", "Other Artist", 180, "t2"),
        ]

    def test_returns_best_match_with_similarity_key(self):
        match = dc.find_similar(
            self._songs(), "Tales from da Guttah", "Killah Priest", 200,
        )
        assert match is not None
        assert match["track_id"] == "t1"
        assert "similarity" in match
        assert match["similarity"] > 0.85

    def test_no_match_returns_none(self):
        assert dc.find_similar(
            self._songs(), "Unrelated Song", "Someone Else", 300,
        ) is None

    def test_returns_copy_not_original(self):
        match = dc.find_similar(
            self._songs(), "Tales from da Guttah", "Killah Priest", 200,
        )
        assert match is not None
        # Original must not carry the injected similarity key.
        assert "similarity" not in self._songs()[0]

    def test_best_of_many_wins(self):
        songs = [
            _song("Tales from da Guttah", "Artist", 200, "t1"),        # 0.927
            _song("Tales form Tha Guttah", "Artist", 200, "t2"),       # 0.952 → best
            _song("Tales from Tha Guttah Pt. 2", "Artist", 200, "t3"), # 0.894
        ]
        match = dc.find_similar(songs, "Tales from Tha Guttah", "Artist", 200)
        assert match is not None
        assert match["track_id"] == "t2"  # highest ratio (all above 0.85)

    def test_empty_song_list(self):
        assert dc.find_similar([], "Nothing", "Nobody", 1) is None

    def test_malformed_rows_skipped(self):
        songs = [{"no": "title"}, _song("Ok Song", "Artist", 200, "t1")]
        match = dc.find_similar(songs, "Ok Song", "Artist", 200)
        assert match is not None
        assert match["track_id"] == "t1"


class TestFindDuplicatePairs:
    def test_pair_found_with_newest_first_ordering(self):
        # newer first per get_all_songs' ORDER BY added_at DESC.
        songs = [
            _song("Tales from da Guttah", "Killah Priest", 200, "new"),
            _song("Tales from Tha Guttah", "Killah Priest", 212, "old"),
        ]
        pairs = dc.find_duplicate_pairs(songs)
        assert len(pairs) == 1
        assert pairs[0]["newer"]["track_id"] == "new"
        assert pairs[0]["older"]["track_id"] == "old"

    def test_no_shared_artist_no_pair(self):
        songs = [
            _song("Same Title", "Artist A", 200, "n"),
            _song("Same Title", "Artist B", 200, "o"),
        ]
        assert dc.find_duplicate_pairs(songs) == []

    def test_unknown_artists_excluded(self):
        songs = [
            _song("Intro", "Unknown Artist", 30, "n"),
            _song("Intro", "Unknown Artist", 31, "o"),
        ]
        assert dc.find_duplicate_pairs(songs) == []

    def test_results_sorted_most_similar_first(self):
        songs = [
            _song("AAA Perfect Match", "Artist", 200, "n"),
            _song("AAA Perfect Match", "Artist", 200, "o"),
            _song("ZZ", "Other Artist", 200, "x"),
            _song("ZZ", "Other Artist", 200, "y"),
        ]
        # Make the second pair much weaker by lowering title overlap.
        songs[2]["title"] = "Completely Unrelated Title One"
        songs[3]["title"] = "Totally Different Title Two"
        pairs = dc.find_duplicate_pairs(songs, threshold=0.3)
        assert len(pairs) == 2
        assert pairs[0]["similarity"] >= pairs[1]["similarity"]


class TestResolveNearDuplicate:
    @pytest.fixture(autouse=True)
    def _sandbox(self, monkeypatch, tmp_path):
        monkeypatch.setattr(duplicate_queue, "extra_json",
                            tmp_path / "db" / "extra.json")
        # Enabled by default in the policy tests; a dedicated test covers off.
        monkeypatch.setattr(dc, "read_settings",
                            lambda: (True, 0.85, 5))

    def test_disabled_returns_proceed(self, monkeypatch):
        monkeypatch.setattr(dc, "read_settings", lambda: (False, 0.85, 5))
        action, match = dc.resolve_near_duplicate(
            songs=[_song("Tales from Tha Guttah", "Artist", 200, "t1")],
            title="Tales from da Guttah", artists="Artist", duration=205,
            track_id="new", platform="spotify", playlist_id="pl", playlist_name="X",
        )
        assert action == "proceed"
        assert match is None

    def test_no_match_proceeds(self):
        action, match = dc.resolve_near_duplicate(
            songs=[_song("Unrelated", "Artist", 200, "t1")],
            title="Nothing Alike", artists="Artist", duration=200,
            track_id="new", platform="spotify", playlist_id="pl", playlist_name="X",
        )
        assert action == "proceed"
        assert match is None

    def test_fresh_match_queues_pending(self):
        action, match = dc.resolve_near_duplicate(
            songs=[_song("Tales from Tha Guttah", "Artist", 212, "t1")],
            title="Tales from da Guttah", artists="Artist", duration=200,
            track_id="new", platform="spotify", playlist_id="pl", playlist_name="X",
        )
        assert action == "queued"
        assert match is not None
        pend = duplicate_queue.find_pending("pl", "new")
        assert pend is not None
        assert pend["title"] == "Tales from da Guttah"
        assert pend["similarity"] == match["similarity"]

    def test_remembered_added_proceeds(self):
        action, match = dc.resolve_near_duplicate(
            songs=[_song("Tales from Tha Guttah", "Artist", 212, "t1")],
            title="Tales from da Guttah", artists="Artist", duration=200,
            track_id="new", platform="spotify", playlist_id="pl", playlist_name="X",
        )
        assert action == "queued"
        # User approved: memory says "added" → next identical add proceeds.
        pair_key = duplicate_queue.make_pair_key("spotify", "pl", "new", "t1")
        duplicate_queue.set_song(pair_key, "added")
        action, match = dc.resolve_near_duplicate(
            songs=[_song("Tales from Tha Guttah", "Artist", 212, "t1")],
            title="Tales from da Guttah", artists="Artist", duration=200,
            track_id="new", platform="spotify", playlist_id="pl", playlist_name="X",
        )
        assert action == "proceed"

    def test_remembered_dismissed_skips(self):
        dc.resolve_near_duplicate(
            songs=[_song("Tales from Tha Guttah", "Artist", 212, "t1")],
            title="Tales from da Guttah", artists="Artist", duration=200,
            track_id="new", platform="spotify", playlist_id="pl", playlist_name="X",
        )
        pair_key = duplicate_queue.make_pair_key("spotify", "pl", "new", "t1")
        duplicate_queue.set_song(pair_key, "dismissed")
        action, match = dc.resolve_near_duplicate(
            songs=[_song("Tales from Tha Guttah", "Artist", 212, "t1")],
            title="Tales from da Guttah", artists="Artist", duration=200,
            track_id="new", platform="spotify", playlist_id="pl", playlist_name="X",
        )
        assert action == "skip"
        assert match is not None