"""Producer-level video<->music matcher.

Builds a ``MusicProfile`` for a given source video by:

  1. **Keyword extraction** from the filename and AI-generated title
     (e.g. "naturebeauty_birds_morning" -> nature + wildlife mood).
  2. **Visual analysis** of 4 sampled frames via ffmpeg's signalstats
     filter (brightness, saturation, warmth) -> mood adjustments.
  3. **Taxonomy mapping** from the resulting mood tokens to Jamendo
     search parameters (vartags, speed bucket, genre tags) suitable
     for the ``/tracks`` endpoint.

The downstream music picker (``src/music.py``) uses this profile to
search Jamendo, then **scores every returned candidate** against the
profile (tag overlap, BPM, duration, vocal/instrumental) and picks
the best match \u2014 not just the most popular result.

Design goal: deterministic for the same video (so a re-run picks the
same track) and inspectable (the profile is logged + saved to state
for later analysis).
"""
from __future__ import annotations
import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Mood taxonomy: keyword -> profile fragment.
# Order matters: earlier rules win when multiple keywords match.
# Each rule contributes: vartags (Jamendo curated mood tags),
# genre_tags (Jamendo broad tags / fuzzytags), speed bucket, energy 0..1.
# ---------------------------------------------------------------------------
_TAXONOMY: list[tuple[tuple[str, ...], dict[str, Any]]] = [
    # Water / calm scenes
    (
        ("ocean", "sea", "wave", "beach", "river", "lake", "water", "rain", "stream"),
        {"vartags": ["mood_relaxing", "mood_dreamy", "mood_meditative"],
         "genre_tags": ["ambient", "chillout", "peaceful", "atmospheric"],
         "speed": ["verylow", "low"], "energy": 0.2},
    ),
    # Nature scenery / landscapes
    (
        ("nature", "scenery", "forest", "mountain", "wild", "wilderness",
         "landscape", "valley", "meadow", "field", "jungle", "trees", "leaves"),
        {"vartags": ["mood_relaxing", "mood_inspirational", "mood_meditative"],
         "genre_tags": ["ambient", "cinematic", "instrumental", "peaceful"],
         "speed": ["low", "medium"], "energy": 0.35},
    ),
    # Golden hour / sky
    (
        ("sunrise", "sunset", "golden", "dawn", "dusk", "sky", "cloud",
         "horizon", "morning", "twilight"),
        {"vartags": ["mood_uplifting", "mood_inspirational", "mood_dreamy"],
         "genre_tags": ["cinematic", "emotional", "ambient", "piano", "orchestral"],
         "speed": ["low", "medium"], "energy": 0.5},
    ),
    # Wildlife / cute animals
    (
        ("bird", "birds", "animal", "wildlife", "kitten", "puppy", "dog", "cat",
         "deer", "fox", "rabbit", "fawn", "baby"),
        {"vartags": ["mood_happy", "mood_uplifting", "mood_relaxing"],
         "genre_tags": ["acoustic", "instrumental", "light", "gentle", "folk"],
         "speed": ["low", "medium"], "energy": 0.45},
    ),
    # Floral / spring
    (
        ("flower", "flowers", "garden", "bloom", "spring", "butterfly",
         "petal", "blossom"),
        {"vartags": ["mood_happy", "mood_dreamy", "mood_relaxing"],
         "genre_tags": ["acoustic", "piano", "ambient", "instrumental"],
         "speed": ["low", "medium"], "energy": 0.4},
    ),
    # Snow / winter
    (
        ("snow", "winter", "ice", "frost", "arctic", "frozen"),
        {"vartags": ["mood_meditative", "mood_dreamy", "mood_relaxing"],
         "genre_tags": ["ambient", "piano", "cinematic", "atmospheric"],
         "speed": ["verylow", "low"], "energy": 0.25},
    ),
    # Night / city / urban
    (
        ("night", "city", "urban", "street", "lights", "neon", "rooftop"),
        {"vartags": ["mood_dark", "mood_dramatic", "mood_dreamy"],
         "genre_tags": ["downtempo", "electronic", "ambient", "lofi"],
         "speed": ["low", "medium"], "energy": 0.5},
    ),
    # Romantic
    (
        ("love", "romantic", "couple", "wedding", "kiss", "heart"),
        {"vartags": ["mood_romantic", "mood_dreamy", "mood_relaxing"],
         "genre_tags": ["piano", "romantic", "acoustic", "ambient"],
         "speed": ["low", "medium"], "energy": 0.45},
    ),
    # Action / energy / sport
    (
        ("fast", "action", "drive", "sport", "race", "extreme", "adventure",
         "workout", "training"),
        {"vartags": ["mood_powerful", "mood_energetic", "mood_epic"],
         "genre_tags": ["electronic", "rock", "energetic", "epic"],
         "speed": ["medium", "high", "veryhigh"], "energy": 0.85},
    ),
    # Sad / melancholy
    (
        ("sad", "tear", "melancholy", "alone", "lonely", "loss", "missing",
         "memory"),
        {"vartags": ["mood_sad", "mood_melancholic", "mood_dark"],
         "genre_tags": ["piano", "ambient", "melancholic", "cinematic"],
         "speed": ["verylow", "low"], "energy": 0.2},
    ),
    # Food / cooking
    (
        ("food", "cook", "cooking", "kitchen", "recipe", "meal", "dish",
         "coffee", "tea"),
        {"vartags": ["mood_happy", "mood_relaxing"],
         "genre_tags": ["jazz", "acoustic", "lounge", "lofi"],
         "speed": ["low", "medium"], "energy": 0.45},
    ),
    # Travel / explore
    (
        ("travel", "explore", "journey", "wander", "trip", "vacation"),
        {"vartags": ["mood_uplifting", "mood_inspirational", "mood_dreamy"],
         "genre_tags": ["cinematic", "world", "acoustic", "ambient"],
         "speed": ["medium"], "energy": 0.6},
    ),
]

_DEFAULT_PROFILE_FRAGMENT: dict[str, Any] = {
    "vartags": ["mood_relaxing", "mood_meditative"],
    "genre_tags": ["instrumental", "ambient", "cinematic"],
    "speed": ["low", "medium"],
    "energy": 0.4,
}


@dataclass
class MusicProfile:
    """Resolved music search profile for a single source video.

    Attributes
    ----------
    vartags
        Jamendo curated mood tags (e.g. ``mood_relaxing``). These usually
        have the highest signal for matching emotional tone.
    genre_tags
        Broad style/genre tags used as Jamendo ``fuzzytags``.
    speed
        Allowed Jamendo speed buckets (``verylow`` ... ``veryhigh``).
    energy
        Normalized 0..1 scene energy (low = calm, high = action).
    keywords_matched
        Source tokens that triggered taxonomy rules (for audit).
    visual_stats
        Sampled brightness/saturation/warmth (for audit).
    """
    vartags: list[str] = field(default_factory=list)
    genre_tags: list[str] = field(default_factory=list)
    speed: list[str] = field(default_factory=list)
    energy: float = 0.4
    keywords_matched: list[str] = field(default_factory=list)
    visual_stats: dict[str, float] = field(default_factory=dict)

    def primary_fuzzytags(self) -> str:
        """Comma-joined genre tags for Jamendo ``fuzzytags`` param."""
        return ",".join(self.genre_tags[:4])

    def fallback_fuzzytags(self) -> str:
        """Broader fallback if the primary search returns nothing."""
        return ",".join(self.genre_tags[:2]) or "instrumental,ambient"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[a-z]+")


def _tokenize(*texts: str) -> list[str]:
    """Lowercase + split into alphabetic tokens. De-dup, preserve order."""
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        for tok in _TOKEN_RE.findall((t or "").lower()):
            if tok not in seen and len(tok) >= 3:
                seen.add(tok)
                out.append(tok)
    return out


def _apply_taxonomy(tokens: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Run tokens through the taxonomy. Merge all matching rules; if no rule
    matches, return the default. Returns (profile_fragment, matched_keywords).

    Matching is two-stage:
      1. Exact token equality (highest signal).
      2. Substring match: keyword appears inside a longer token. This
         catches social-media-style filenames where words are concatenated
         without separators (e.g. ``naturebeautyscenery`` -> nature + beauty + scenery).
    """
    matched: list[str] = []
    bucket: dict[str, Any] = {
        "vartags": [], "genre_tags": [], "speed": [], "energy_sum": 0.0,
    }
    n_rules = 0
    for keywords, fragment in _TAXONOMY:
        hits: list[str] = []
        for tok in tokens:
            if tok in keywords:
                hits.append(tok)
                continue
            # Substring: only if the keyword is >=4 chars to avoid noisy
            # matches like "art" inside "start" or "ice" inside "police".
            for kw in keywords:
                if len(kw) >= 4 and kw in tok:
                    hits.append(kw)
                    break
        if not hits:
            continue
        matched.extend(hits)
        for k in ("vartags", "genre_tags", "speed"):
            for v in fragment[k]:
                if v not in bucket[k]:
                    bucket[k].append(v)
        bucket["energy_sum"] += fragment["energy"]
        n_rules += 1

    if n_rules == 0:
        return dict(_DEFAULT_PROFILE_FRAGMENT), []

    energy = bucket["energy_sum"] / n_rules
    return {
        "vartags": bucket["vartags"],
        "genre_tags": bucket["genre_tags"],
        "speed": bucket["speed"],
        "energy": energy,
    }, matched


# ---------------------------------------------------------------------------
# Visual analysis (ffmpeg signalstats)
# ---------------------------------------------------------------------------
def _sample_visual_stats(video_path: str, samples: int = 4) -> dict[str, float]:
    """Sample ``samples`` evenly-spaced frames and compute average:
      - brightness  (0..1)
      - saturation  (0..1)
      - warmth      (-1 cool .. +1 warm)  via U-V chroma comparison

    Returns an empty dict if ffmpeg/ffprobe is unavailable or the probe
    fails. Caller treats missing stats as "neutral".
    """
    if not os.path.exists(video_path):
        return {}
    # Duration probe
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", video_path],
            capture_output=True, text=True, timeout=15,
        )
        duration = float(r.stdout.strip() or 0.0)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return {}
    if duration <= 0:
        return {}

    timestamps = [duration * (i + 0.5) / samples for i in range(samples)]
    y_vals: list[float] = []
    s_vals: list[float] = []
    warmth_vals: list[float] = []

    for ts in timestamps:
        # Use a single ffmpeg per timestamp: extract frame, run signalstats,
        # parse YAVG, UAVG, VAVG from stderr. We keep stderr small via
        # -loglevel stats and a 1-frame filter.
        try:
            r = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "info", "-y",
                    "-ss", f"{ts:.3f}", "-i", video_path,
                    "-frames:v", "1",
                    "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG,"
                           "metadata=print:key=lavfi.signalstats.UAVG,"
                           "metadata=print:key=lavfi.signalstats.VAVG,"
                           "metadata=print:key=lavfi.signalstats.SATAVG",
                    "-f", "null", "-",
                ],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {}

        text = r.stderr
        yavg = _extract_signalstat(text, "YAVG")
        uavg = _extract_signalstat(text, "UAVG")
        vavg = _extract_signalstat(text, "VAVG")
        satavg = _extract_signalstat(text, "SATAVG")
        if yavg is not None:
            y_vals.append(yavg / 255.0)
        if satavg is not None:
            s_vals.append(satavg / 128.0)   # SATAVG is 0..~128
        if uavg is not None and vavg is not None:
            # V channel (red) > U channel (blue) -> warm.
            # Normalize around 128 neutral.
            warmth_vals.append(((vavg - 128.0) - (uavg - 128.0)) / 128.0)

    if not y_vals:
        return {}
    return {
        "brightness": round(sum(y_vals) / len(y_vals), 3),
        "saturation": round(sum(s_vals) / len(s_vals), 3) if s_vals else 0.0,
        "warmth":     round(sum(warmth_vals) / len(warmth_vals), 3) if warmth_vals else 0.0,
    }


_SIGNALSTAT_RE_CACHE: dict[str, re.Pattern] = {}


def _extract_signalstat(text: str, key: str) -> float | None:
    pat = _SIGNALSTAT_RE_CACHE.get(key)
    if pat is None:
        pat = re.compile(rf"lavfi\.signalstats\.{key}=([\-0-9.]+)")
        _SIGNALSTAT_RE_CACHE[key] = pat
    matches = pat.findall(text)
    if not matches:
        return None
    try:
        # Average across all frames found (usually just 1 per invocation)
        return sum(float(m) for m in matches) / len(matches)
    except ValueError:
        return None


def _adjust_for_visuals(profile: dict[str, Any], stats: dict[str, float]) -> dict[str, Any]:
    """Nudge the profile based on visual statistics.

    - Bright + warm + saturated -> +happy, +uplifting
    - Dark / desaturated        -> +dramatic, +meditative; slower speed
    - Very high brightness      -> nudge energy up
    - Very low brightness       -> nudge energy down + add piano/ambient
    """
    if not stats:
        return profile
    out = {
        "vartags":    list(profile["vartags"]),
        "genre_tags": list(profile["genre_tags"]),
        "speed":      list(profile["speed"]),
        "energy":     float(profile["energy"]),
    }
    brightness = stats.get("brightness", 0.5)
    saturation = stats.get("saturation", 0.5)
    warmth = stats.get("warmth", 0.0)

    def _add(lst: list[str], val: str) -> None:
        if val not in lst:
            lst.append(val)

    if brightness >= 0.55 and saturation >= 0.35 and warmth >= 0.05:
        _add(out["vartags"], "mood_happy")
        _add(out["vartags"], "mood_uplifting")
        out["energy"] = min(1.0, out["energy"] + 0.10)
    if brightness <= 0.30:
        _add(out["vartags"], "mood_dark")
        _add(out["vartags"], "mood_meditative")
        _add(out["genre_tags"], "piano")
        _add(out["genre_tags"], "ambient")
        out["energy"] = max(0.05, out["energy"] - 0.10)
        # Bias to slower speeds
        for s in ("verylow", "low"):
            _add(out["speed"], s)
    if warmth >= 0.10:
        _add(out["genre_tags"], "cinematic")
    if warmth <= -0.10:
        _add(out["genre_tags"], "ambient")
        _add(out["vartags"], "mood_dreamy")
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_profile(
    *,
    filename: str = "",
    title: str = "",
    video_path: str = "",
    hashtags: list[str] | None = None,
    keywords: list[str] | None = None,
) -> MusicProfile:
    """Build a ``MusicProfile`` from any combination of source signals.

    Signal priority (highest to lowest):
      1. ``hashtags`` \u2014 explicit ``#tags`` from filename. Ground truth.
      2. ``keywords`` \u2014 non-hashtag descriptive tokens from filename.
      3. ``filename`` + ``title`` \u2014 free-text fallback.
      4. ``video_path`` \u2014 visual analysis nudges.

    All inputs optional, but at least one should be provided. The function
    always returns a usable profile (falls back to a sensible
    ambient/instrumental default).
    """
    # Hashtags are the highest-signal source: place them first so the
    # taxonomy matches them directly. Keywords next, then filename/title.
    primary_text = " ".join(hashtags or []) + " " + " ".join(keywords or [])
    tokens = _tokenize(primary_text, filename, title)
    fragment, matched = _apply_taxonomy(tokens)
    stats: dict[str, float] = {}
    if video_path:
        try:
            stats = _sample_visual_stats(video_path)
        except Exception as e:  # noqa: BLE001
            print(f"[music-match] visual analysis skipped: {e}")
            stats = {}
    fragment = _adjust_for_visuals(fragment, stats)

    profile = MusicProfile(
        vartags=fragment["vartags"],
        genre_tags=fragment["genre_tags"],
        speed=fragment["speed"] or ["low", "medium"],
        energy=float(fragment["energy"]),
        keywords_matched=matched,
        visual_stats=stats,
    )
    print(
        f"[music-match] tokens={tokens[:8]}{'...' if len(tokens)>8 else ''} "
        f"matched={matched or '(none, default)'} "
        f"vartags={profile.vartags} genres={profile.genre_tags} "
        f"speed={profile.speed} energy={profile.energy:.2f} "
        f"visuals={stats or '(skipped)'}"
    )
    return profile
