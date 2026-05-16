"""Regression tests for src.semantic.

Covers the five layers of the deep analyzer:

  1. compound word splitting
  2. theme classification
  3. mood vector computation
  4. title generation (deterministic per seed)
  5. synonym expansion
"""
from __future__ import annotations

from src import semantic


# ---------------------------------------------------------------------------
# split_compound
# ---------------------------------------------------------------------------
class TestSplitCompound:
    def test_known_compound(self):
        assert semantic.split_compound("naturebeauty") == ["nature", "beauty"]

    def test_three_pieces(self):
        assert semantic.split_compound("naturebeautyscenery") == [
            "nature", "beauty", "scenery",
        ]

    def test_atomic_word_unchanged(self):
        assert semantic.split_compound("flowers") == ["flowers"]

    def test_unknown_word_returns_self(self):
        assert semantic.split_compound("zxqvquark") == ["zxqvquark"]

    def test_empty_input(self):
        assert semantic.split_compound("") == []

    def test_case_insensitive(self):
        assert semantic.split_compound("NatureBeauty") == ["nature", "beauty"]


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
class TestAnalyze:
    def test_themes_for_flowers_rain(self):
        a = semantic.analyze(hashtags=["flowers", "rain", "naturebeauty"])
        # 'flora' from flowers; 'water' from rain; 'wilderness' from
        # nature+beauty (split out of 'naturebeauty').
        assert "flora" in a.themes
        assert "water" in a.themes
        assert "wilderness" in a.themes

    def test_dominant_themes_top_two(self):
        a = semantic.analyze(hashtags=["flowers", "rain", "naturebeauty"])
        assert len(a.dominant_themes) <= 2
        # 'wilderness' should win because nature + beauty + scenery
        # contribute heavily.
        assert "wilderness" in a.dominant_themes

    def test_compound_split_recorded(self):
        a = semantic.analyze(hashtags=["naturebeauty"])
        assert "naturebeauty" in a.splits
        assert a.splits["naturebeauty"] == ["nature", "beauty"]

    def test_energy_in_range(self):
        a = semantic.analyze(hashtags=["flowers", "rain"])
        assert 0.0 <= a.energy <= 1.0
        assert 0.0 <= a.calmness <= 1.0
        assert -1.0 <= a.warmth <= 1.0

    def test_calm_words_lower_energy(self):
        calm = semantic.analyze(hashtags=["peace", "calm", "rain", "soft"])
        action = semantic.analyze(hashtags=["wild", "waterfall", "cliff"])
        assert calm.energy < action.energy

    def test_synonym_expansion(self):
        a = semantic.analyze(hashtags=["flowers"])
        # Each filename hashtag pulls related tags from the synonym map.
        assert any(t in {"bloom", "petals", "floral", "garden"}
                   for t in a.related_tags)

    def test_empty_input_safe(self):
        a = semantic.analyze()
        # Default-constructed analysis must be usable (no crash, sensible
        # neutral values).
        assert a.tokens == []
        assert a.dominant_themes == []
        assert 0.0 <= a.energy <= 1.0


# ---------------------------------------------------------------------------
# generate_title
# ---------------------------------------------------------------------------
class TestGenerateTitle:
    def test_deterministic_with_seed(self):
        a = semantic.analyze(hashtags=["flowers", "rain", "naturebeauty"])
        t1 = semantic.generate_title(a, seed="seed-x")
        t2 = semantic.generate_title(a, seed="seed-x")
        assert t1 == t2  # MUST be deterministic per seed

    def test_different_seeds_can_differ(self):
        a = semantic.analyze(hashtags=["flowers", "rain", "naturebeauty"])
        # Iterate a few seeds; we only assert at least one differs from
        # another (the template pool has multiple entries).
        titles = {semantic.generate_title(a, seed=str(i)) for i in range(8)}
        assert len(titles) >= 2

    def test_no_unfilled_placeholders(self):
        a = semantic.analyze(hashtags=["flowers", "rain"])
        t = semantic.generate_title(a, seed="x")
        assert "{a}" not in t
        assert "{b}" not in t
        assert "{emoji}" not in t

    def test_falls_back_when_no_themes(self):
        a = semantic.analyze(hashtags=[])
        t = semantic.generate_title(a, seed="x")
        # Generic template still produces a usable title (non-empty).
        assert t and isinstance(t, str)

    def test_emoji_present_when_themes_known(self):
        a = semantic.analyze(hashtags=["flowers", "rain"])
        t = semantic.generate_title(a, seed="x")
        # At least one of the theme emojis from THEME_EMOJI should appear.
        assert any(emoji in t for emoji in semantic.THEME_EMOJI.values())


# ---------------------------------------------------------------------------
# expanded_hashtags
# ---------------------------------------------------------------------------
class TestExpandedHashtags:
    def test_filename_first(self):
        a = semantic.analyze(hashtags=["flowers", "rain"])
        out = semantic.expanded_hashtags(a, ["flowers", "rain"], max_extra=4)
        assert out[:2] == ["flowers", "rain"]

    def test_no_duplicates(self):
        a = semantic.analyze(hashtags=["flowers"])
        out = semantic.expanded_hashtags(a, ["flowers"], max_extra=4)
        lower = [t.lower() for t in out]
        assert len(lower) == len(set(lower))

    def test_respects_max_extra(self):
        a = semantic.analyze(hashtags=["flowers", "rain", "naturebeauty"])
        out = semantic.expanded_hashtags(a, ["flowers", "rain", "naturebeauty"],
                                         max_extra=2)
        assert len(out) == 3 + 2  # 3 primary + 2 extras max
