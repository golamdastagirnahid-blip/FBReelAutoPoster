"""Regression tests for src.music_match and src.music scoring.

We do not hit the Jamendo API in tests \u2014 ``score_track`` and
``build_profile`` are pure functions that we can validate with synthetic
:class:`music.Track` instances.
"""
from __future__ import annotations

from src import music, music_match


def _track(**kw) -> music.Track:
    """Build a ``Track`` with sensible defaults; override via kwargs."""
    defaults = dict(
        id="t1", name="Test", artist="A", license_url="",
        audio_url="http://x", duration=180,
        vartags=[], musicinfo_tags=[], bpm=None, speed="",
        vocal_instrumental="",
    )
    defaults.update(kw)
    return music.Track(**defaults)


# ---------------------------------------------------------------------------
# build_profile
# ---------------------------------------------------------------------------
class TestBuildProfile:
    def test_hashtags_drive_profile(self):
        p = music_match.build_profile(
            hashtags=["flowers", "rain"], keywords=[],
        )
        # Must select calm/ambient leaning genres for water+flora context.
        assert "ambient" in p.genre_tags or "chillout" in p.genre_tags
        assert "low" in p.speed or "verylow" in p.speed

    def test_action_words_higher_energy(self):
        calm = music_match.build_profile(hashtags=["rain", "peaceful"])
        active = music_match.build_profile(hashtags=["action", "extreme", "sport"])
        assert active.energy > calm.energy

    def test_default_when_no_signal(self):
        p = music_match.build_profile()
        # Default fragment is ambient/instrumental at moderate energy.
        assert p.genre_tags
        assert p.speed
        assert 0.0 <= p.energy <= 1.0

    def test_semantic_energy_blended(self):
        # When semantic_energy is provided it must influence the result.
        p_low = music_match.build_profile(
            hashtags=["nature"], semantic_energy=0.05,
        )
        p_high = music_match.build_profile(
            hashtags=["nature"], semantic_energy=0.95,
        )
        assert p_high.energy > p_low.energy


# ---------------------------------------------------------------------------
# score_track
# ---------------------------------------------------------------------------
class TestScoreTrack:
    def test_vartag_overlap_dominates(self):
        profile = music_match.build_profile(hashtags=["flowers", "rain"])
        # Track that hits 2 vartags from the profile.
        t_match = _track(vartags=list(profile.vartags[:2]),
                         vocal_instrumental="instrumental")
        # Track with no overlap.
        t_miss = _track(vartags=["mood_powerful"],
                        vocal_instrumental="instrumental")
        s_match, _ = music.score_track(t_match, profile)
        s_miss, _ = music.score_track(t_miss, profile)
        assert s_match > s_miss

    def test_vocal_penalty_below_instrumental(self):
        profile = music_match.build_profile(hashtags=["flowers"])
        t_inst = _track(vocal_instrumental="instrumental")
        t_voc = _track(vocal_instrumental="vocal")
        s_inst, _ = music.score_track(t_inst, profile)
        s_voc, _ = music.score_track(t_voc, profile)
        assert s_inst > s_voc

    def test_duration_fit_bonus(self):
        profile = music_match.build_profile(hashtags=["flowers"])
        t_long = _track(duration=200, vocal_instrumental="instrumental")
        t_short = _track(duration=20, vocal_instrumental="instrumental")
        s_long, r_long = music.score_track(t_long, profile, video_duration=60.0)
        s_short, r_short = music.score_track(t_short, profile, video_duration=60.0)
        assert s_long > s_short
        assert any("dur" in r for r in r_long)

    def test_bpm_window_for_low_energy(self):
        # Energy 0.2 should accept BPM ~70.
        bmin, bmax = music._bpm_window_for_energy(0.2)
        assert bmin <= 70 <= bmax

    def test_bpm_window_for_high_energy(self):
        bmin, bmax = music._bpm_window_for_energy(0.9)
        assert bmin <= 140 <= bmax

    def test_score_reasons_returned(self):
        profile = music_match.build_profile(hashtags=["flowers"])
        t = _track(vocal_instrumental="instrumental",
                   musicinfo_tags=list(profile.genre_tags[:2]))
        s, reasons = music.score_track(t, profile)
        assert isinstance(reasons, list)
        assert any("genres+" in r for r in reasons)
