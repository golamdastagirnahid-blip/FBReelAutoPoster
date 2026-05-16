"""Regression tests for src.captions.

These pin behavior of the filename-derived hashtag + keyword extraction.
Any change to ``extract_filename_hashtags`` / ``extract_filename_keywords``
/ ``sample_hashtags`` MUST keep these passing or be accompanied by an
explicit test update.
"""
from __future__ import annotations
import random

from src import captions


# ---------------------------------------------------------------------------
# extract_filename_hashtags
# ---------------------------------------------------------------------------
class TestExtractFilenameHashtags:
    def test_basic(self):
        out = captions.extract_filename_hashtags(
            "masstiktok_naturebeautyscenery__#flowers #rain #naturebeauty.mp4"
        )
        assert out == ["flowers", "rain", "naturebeauty"]

    def test_dedup_case_insensitive(self):
        out = captions.extract_filename_hashtags(
            "video__#Flowers #flowers #FLOWERS.mp4"
        )
        # Case-insensitive dedup; first occurrence (with original case) wins.
        assert out == ["Flowers"]

    def test_drops_truncated_short_tags(self):
        # Drive truncated tail '#nat' must be dropped (default min_len=4).
        out = captions.extract_filename_hashtags("video__#flowers #nat.mp4")
        assert out == ["flowers"]

    def test_no_hashtags(self):
        assert captions.extract_filename_hashtags("just_a_video.mp4") == []

    def test_punctuation_around_tag(self):
        out = captions.extract_filename_hashtags(
            "video__#flowers, #rain! and #birds.mp4"
        )
        assert "flowers" in out and "rain" in out and "birds" in out


# ---------------------------------------------------------------------------
# extract_filename_keywords
# ---------------------------------------------------------------------------
class TestExtractFilenameKeywords:
    def test_extracts_handle_compound(self):
        # The compound 'naturebeautyscenery' sits in the handle position
        # but is high-signal — must be captured.
        out = captions.extract_filename_keywords(
            "masstiktok_naturebeautyscenery__#flowers #rain.mp4"
        )
        assert "naturebeautyscenery" in out

    def test_drops_platform_names(self):
        out = captions.extract_filename_keywords(
            "masstiktok_naturebeauty__#x.mp4"
        )
        assert "masstiktok" not in out
        assert "naturebeauty" in out

    def test_drops_noise_tokens(self):
        out = captions.extract_filename_keywords("video_HD_download_mp4.mp4")
        # 'video', 'download', 'mp4' all in noise set.
        assert "video" not in out
        assert "download" not in out
        assert "mp4" not in out

    def test_strips_hashtag_bodies(self):
        # Hashtag content must NOT appear in keywords (returned by the
        # hashtag extractor instead) — prevents double-counting.
        out = captions.extract_filename_keywords("vid__#flowers #rain.mp4")
        assert "flowers" not in out
        assert "rain" not in out


# ---------------------------------------------------------------------------
# sample_hashtags
# ---------------------------------------------------------------------------
class TestSampleHashtags:
    def test_primary_first(self):
        rng = random.Random(0)
        out = captions.sample_hashtags(
            ["a", "b", "c", "d", "e"], 5, 5, rng=rng,
            primary=["flowers", "rain"],
        )
        assert out[:2] == ["flowers", "rain"]
        assert len(out) == 5

    def test_no_duplicate_with_pool(self):
        rng = random.Random(0)
        out = captions.sample_hashtags(
            ["flowers", "FLOWERS", "x", "y"], 3, 3, rng=rng,
            primary=["flowers"],
        )
        # 'flowers' comes from primary; pool's 'flowers'/'FLOWERS' must
        # be skipped (case-insensitive dedup).
        lower = [t.lower() for t in out]
        assert lower.count("flowers") == 1

    def test_empty_pool_returns_primary(self):
        out = captions.sample_hashtags([], 5, 8, primary=["a", "b"])
        assert out == ["a", "b"]

    def test_no_pool_no_primary(self):
        out = captions.sample_hashtags([], 5, 8)
        assert out == []

    def test_count_respects_minimum_with_primary(self):
        rng = random.Random(0)
        out = captions.sample_hashtags(
            ["x", "y", "z", "w", "q"], 5, 5, rng=rng,
            primary=["a", "b"],
        )
        assert len(out) == 5


# ---------------------------------------------------------------------------
# clean_title (still used as fallback when no theme matches)
# ---------------------------------------------------------------------------
class TestCleanTitle:
    def test_strips_platform_prefix(self):
        out = captions.clean_title(
            "masstiktok_user1__ A bunch of pink baby birds.mp4",
        )
        assert "masstiktok" not in out.lower()
        assert "birds" in out.lower()

    def test_pool_fallback_when_weak(self):
        rng = random.Random(0)
        out = captions.clean_title(
            "vid.mp4", fallback_pool=["Pool A", "Pool B"], rng=rng,
        )
        assert out in {"Pool A", "Pool B"}
