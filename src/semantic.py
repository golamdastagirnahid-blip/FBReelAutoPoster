"""Deep semantic analysis of source filename tokens.

This module turns raw filename tokens (hashtags + keywords) into a
structured analysis that drives:

  * **Title generation** \u2014 theme-based templates so every post's title
    actually reflects its content (no more random title-pool fallback).
  * **Hashtag expansion** \u2014 each input tag pulls in semantically related
    high-reach tags from a curated synonym map.
  * **Music profile** \u2014 each token contributes a weighted (energy,
    calmness, warmth) vector that is averaged into the final profile,
    giving more nuance than the coarse rule fusion alone.

Design notes
------------
This is a deliberately rule-based / lexicon-based analyzer rather than
an LLM call:

  * Deterministic per filename (same input always yields the same output).
  * Zero external dependencies, fast (sub-millisecond).
  * Inspectable: every decision is traceable to a lexicon entry or
    theme-template combination.

The lexicon is tuned for the nature / wildlife / scenic niche which is
what the source library actually contains. To extend to a new niche,
add words to ``LEXICON``, theme members to ``THEMES`` and ``SYNONYMS``,
and (optionally) title templates.
"""
from __future__ import annotations
import random
import re
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# 1. Lexicon \u2014 known atomic content words.
#    Used by the compound splitter to break ``naturebeautyscenery`` into
#    [nature, beauty, scenery]. Every word here should be a *single*
#    semantic concept (no plurals if we accept the singular, etc.).
# ---------------------------------------------------------------------------
LEXICON: set[str] = {
    # Wilderness / nature umbrella
    "nature", "natural", "beauty", "beautiful", "scenery", "scenic",
    "landscape", "wild", "wilderness", "earth", "planet", "world", "view",
    "views", "scene", "vista",
    # Water
    "rain", "rainy", "river", "ocean", "sea", "wave", "waves", "lake",
    "water", "waterfall", "stream", "creek", "drop", "drops", "pond",
    "splash", "mist",
    # Sky / time of day
    "sky", "cloud", "clouds", "sunrise", "sunset", "dawn", "dusk",
    "golden", "hour", "morning", "evening", "twilight", "horizon", "sun",
    "moon", "star", "stars", "night",
    # Wildlife
    "bird", "birds", "animal", "animals", "wildlife", "deer", "fox",
    "eagle", "owl", "hawk", "kitten", "puppy", "cat", "dog", "fish",
    "butterfly", "feather", "feathers", "wings", "nest", "creature",
    # Flora
    "flower", "flowers", "bloom", "blossom", "garden", "petal", "petals",
    "tree", "trees", "forest", "leaf", "leaves", "rose", "lily", "tulip",
    "lotus", "grass", "moss", "fern", "branch",
    # Seasons / weather
    "spring", "summer", "autumn", "winter", "snow", "snowy", "ice",
    "frost", "frozen", "fog", "foggy", "wind", "breeze", "storm",
    # Terrain
    "mountain", "mountains", "peak", "valley", "hill", "cliff", "canyon",
    "desert", "beach", "shore", "coast", "field", "meadow", "trail",
    "path", "rock", "rocks", "stone", "cave",
    # Emotion / vibe
    "peace", "peaceful", "calm", "serene", "quiet", "tranquil",
    "magic", "magical", "dream", "dreamy", "love", "lovely", "lover",
    "lovers", "heart", "soul", "soft", "gentle", "pure", "sweet",
    "cute", "tiny", "baby", "cozy", "warm", "cool",
    # Aesthetic / photo terms (signal for picking instrumental music)
    "aesthetic", "aesthetics", "vibe", "vibes", "moment", "moments",
    "life", "story", "stories", "magic", "wonder", "amazing",
    "stunning", "incredible", "breathtaking",
    # Filler / structural that we want to recognize but not bias
    "with", "the", "and", "from", "into",
}

# ---------------------------------------------------------------------------
# 2. Themes \u2014 each token classified into 0..N themes; multiple themes
#    are allowed (``rainforest`` -> water + flora). Theme membership
#    drives title templates and music genre selection.
# ---------------------------------------------------------------------------
THEMES: dict[str, set[str]] = {
    "water": {
        "rain", "rainy", "river", "ocean", "sea", "wave", "waves", "lake",
        "water", "waterfall", "stream", "creek", "drop", "drops", "pond",
        "splash", "mist", "shore", "coast", "beach",
    },
    "wildlife": {
        "bird", "birds", "animal", "animals", "wildlife", "deer", "fox",
        "eagle", "owl", "hawk", "kitten", "puppy", "cat", "dog", "fish",
        "butterfly", "feather", "feathers", "wings", "nest", "creature",
    },
    "flora": {
        "flower", "flowers", "bloom", "blossom", "garden", "petal",
        "petals", "tree", "trees", "forest", "leaf", "leaves", "rose",
        "lily", "tulip", "lotus", "grass", "moss", "fern", "branch",
    },
    "sky": {
        "sky", "cloud", "clouds", "sunrise", "sunset", "dawn", "dusk",
        "golden", "hour", "morning", "evening", "twilight", "horizon",
        "sun", "moon", "star", "stars",
    },
    "mountain": {
        "mountain", "mountains", "peak", "valley", "hill", "cliff",
        "canyon", "rock", "rocks", "stone", "cave", "trail", "path",
    },
    "winter": {
        "snow", "snowy", "ice", "frost", "frozen", "winter", "fog",
        "foggy",
    },
    "emotion": {
        "peace", "peaceful", "calm", "serene", "quiet", "tranquil",
        "magic", "magical", "dream", "dreamy", "love", "lovely",
        "lover", "lovers", "heart", "soul", "soft", "gentle", "pure",
        "sweet", "cute", "tiny", "baby", "cozy",
    },
    "wilderness": {
        "nature", "natural", "beauty", "beautiful", "scenery", "scenic",
        "landscape", "wild", "wilderness", "earth", "planet", "world",
        "view", "views", "scene", "vista", "field", "meadow", "desert",
    },
    "night": {"night", "moon", "star", "stars", "twilight", "evening"},
}

THEME_EMOJI: dict[str, str] = {
    "water":      "\U0001F4A7",   # \U0001F4A7
    "wildlife":   "\U0001F426",   # \U0001F426
    "flora":      "\U0001F33C",   # \U0001F33C
    "sky":        "\U0001F305",   # \U0001F305
    "mountain":   "\u26F0\uFE0F", # \u26F0
    "winter":     "\u2744\uFE0F", # \u2744
    "emotion":    "\U0001F496",   # \U0001F496
    "wilderness": "\U0001F33F",   # \U0001F33F
    "night":      "\U0001F31B",   # \U0001F31B
}

# ---------------------------------------------------------------------------
# 3. Token -> (energy, calmness, warmth) vector.
#    energy  : 0 (still) .. 1 (intense action)
#    calmness: 0 (chaotic) .. 1 (deeply calm)
#    warmth  : -1 (cool/blue tones) .. +1 (warm/orange tones)
#    Each value is hand-tuned for the dominant feel of the word; any
#    unknown token defaults to (0.4, 0.5, 0.0) (neutral-calm).
# ---------------------------------------------------------------------------
_TOKEN_VECTOR: dict[str, tuple[float, float, float]] = {
    # water
    "rain": (0.25, 0.85, -0.2), "rainy": (0.25, 0.85, -0.2),
    "river": (0.40, 0.65, 0.0), "ocean": (0.50, 0.55, -0.1),
    "sea": (0.45, 0.60, -0.1), "wave": (0.55, 0.45, -0.1),
    "waves": (0.55, 0.45, -0.1), "lake": (0.20, 0.85, 0.0),
    "water": (0.30, 0.75, 0.0), "waterfall": (0.65, 0.45, -0.1),
    "stream": (0.30, 0.80, 0.0), "mist": (0.15, 0.85, -0.05),
    # sky / golden hour
    "sunrise": (0.35, 0.70, 0.6), "sunset": (0.30, 0.75, 0.7),
    "dawn": (0.30, 0.80, 0.5), "dusk": (0.25, 0.80, 0.5),
    "golden": (0.40, 0.65, 0.7), "morning": (0.40, 0.70, 0.4),
    "evening": (0.25, 0.80, 0.4), "sky": (0.30, 0.70, 0.0),
    "cloud": (0.20, 0.80, -0.05), "clouds": (0.20, 0.80, -0.05),
    "sun": (0.50, 0.55, 0.7), "moon": (0.15, 0.90, -0.2),
    # wildlife
    "bird": (0.45, 0.55, 0.2), "birds": (0.45, 0.55, 0.2),
    "animal": (0.55, 0.45, 0.1), "wildlife": (0.55, 0.50, 0.1),
    "deer": (0.40, 0.65, 0.2), "fox": (0.55, 0.45, 0.3),
    "kitten": (0.45, 0.65, 0.4), "puppy": (0.55, 0.55, 0.4),
    "cat": (0.45, 0.65, 0.3), "dog": (0.60, 0.55, 0.4),
    "butterfly": (0.30, 0.75, 0.4), "fish": (0.35, 0.70, -0.05),
    # flora
    "flower": (0.30, 0.70, 0.5), "flowers": (0.30, 0.70, 0.5),
    "bloom": (0.35, 0.65, 0.5), "blossom": (0.30, 0.70, 0.5),
    "garden": (0.30, 0.75, 0.4), "petal": (0.20, 0.85, 0.4),
    "petals": (0.20, 0.85, 0.4), "tree": (0.25, 0.80, 0.2),
    "trees": (0.25, 0.80, 0.2), "forest": (0.30, 0.80, 0.1),
    "leaf": (0.20, 0.80, 0.2), "leaves": (0.25, 0.75, 0.2),
    "rose": (0.30, 0.70, 0.6), "lotus": (0.20, 0.90, 0.4),
    # mountain / terrain
    "mountain": (0.55, 0.55, 0.0), "mountains": (0.55, 0.55, 0.0),
    "peak": (0.65, 0.45, 0.0), "valley": (0.30, 0.75, 0.2),
    "hill": (0.35, 0.70, 0.2), "cliff": (0.65, 0.40, -0.1),
    "canyon": (0.55, 0.50, 0.4), "desert": (0.45, 0.55, 0.6),
    "field": (0.30, 0.75, 0.4), "meadow": (0.30, 0.80, 0.4),
    # winter
    "snow": (0.20, 0.85, -0.5), "snowy": (0.20, 0.85, -0.5),
    "ice": (0.25, 0.80, -0.6), "frost": (0.20, 0.85, -0.5),
    "winter": (0.25, 0.80, -0.4),
    # emotion
    "peace": (0.10, 0.95, 0.0), "peaceful": (0.10, 0.95, 0.0),
    "calm": (0.10, 0.95, 0.0), "serene": (0.10, 0.95, 0.1),
    "tranquil": (0.10, 0.95, 0.1), "quiet": (0.10, 0.90, 0.0),
    "magic": (0.40, 0.65, 0.3), "magical": (0.40, 0.65, 0.3),
    "dream": (0.20, 0.85, 0.2), "dreamy": (0.20, 0.85, 0.2),
    "love": (0.45, 0.60, 0.5), "lovely": (0.40, 0.65, 0.5),
    "heart": (0.45, 0.60, 0.5), "soft": (0.20, 0.85, 0.3),
    "gentle": (0.20, 0.85, 0.3), "pure": (0.30, 0.80, 0.2),
    "sweet": (0.35, 0.70, 0.4), "cute": (0.45, 0.65, 0.3),
    "tiny": (0.30, 0.75, 0.3), "baby": (0.40, 0.70, 0.4),
    "cozy": (0.25, 0.80, 0.5),
    # wilderness
    "nature": (0.35, 0.70, 0.1), "natural": (0.30, 0.75, 0.1),
    "beauty": (0.35, 0.70, 0.3), "beautiful": (0.35, 0.70, 0.3),
    "scenery": (0.30, 0.75, 0.2), "scenic": (0.30, 0.75, 0.2),
    "landscape": (0.40, 0.65, 0.1), "wild": (0.65, 0.40, 0.1),
    "wilderness": (0.55, 0.50, 0.1), "vista": (0.40, 0.70, 0.2),
}

_DEFAULT_VECTOR = (0.40, 0.50, 0.0)

# ---------------------------------------------------------------------------
# 4. Synonyms / related-tag expansion. Used to:
#    * generate richer FB hashtags from a small filename set
#    * widen Jamendo search terms
# ---------------------------------------------------------------------------
SYNONYMS: dict[str, list[str]] = {
    "rain": ["rainyday", "rainfall", "raindrops", "monsoon"],
    "flower": ["flowers", "bloom", "blooming", "petals", "floral", "garden"],
    "flowers": ["flower", "bloom", "blooming", "petals", "floral"],
    "naturebeauty": ["naturelovers", "naturephotography", "scenery", "earthpix"],
    "nature": ["naturelovers", "naturephotography", "earth", "wildlife"],
    "bird": ["birds", "birdsofinstagram", "wildlife", "birdphotography"],
    "birds": ["bird", "birdsofinstagram", "wildlife", "birdphotography"],
    "sunset": ["sunsetlovers", "goldenhour", "skyporn"],
    "sunrise": ["sunriselovers", "goldenhour", "morninglight"],
    "ocean": ["oceanlife", "seaview", "waves", "beachlife"],
    "sea":   ["seaview", "ocean", "waves", "beachlife"],
    "mountain": ["mountains", "peaks", "hiking", "wilderness"],
    "forest": ["forestlife", "woodland", "trees", "greenery"],
    "snow": ["snowfall", "winterscene", "snowy", "winterwonderland"],
    "love": ["lovelife", "soulful"],
    "peaceful": ["serenity", "calmvibes", "tranquility"],
    "wildlife": ["wildlifephotography", "animals", "natureonly"],
    "scenery": ["sceneryview", "landscapephotography", "naturebeauty"],
}


# ---------------------------------------------------------------------------
# Compound splitter
# ---------------------------------------------------------------------------
def split_compound(word: str, lexicon: set[str] = LEXICON,
                   max_pieces: int = 5) -> list[str]:
    """Greedy left-to-right longest-match split of a concatenated word.

    ``naturebeautyscenery`` -> ``[nature, beauty, scenery]``.
    Returns ``[word]`` if no clean split is possible (so single tokens
    like ``birds`` pass through unchanged).

    The lexicon is consulted at every prefix; the longest match wins,
    then we recurse on the remainder. We require *every* piece to be in
    the lexicon \u2014 partial splits are rejected so we don't emit garbage
    like ``[nature, beautys, cenery]``.
    """
    word = word.lower()
    if not word or word in lexicon:
        return [word] if word else []

    # Try every prefix length from longest to shortest.
    for end in range(len(word), 2, -1):  # min piece length 3
        prefix = word[:end]
        if prefix in lexicon:
            rest = word[end:]
            if not rest:
                return [prefix]
            sub = split_compound(rest, lexicon, max_pieces - 1)
            if sub and all(s in lexicon for s in sub) and len(sub) <= max_pieces:
                return [prefix] + sub
    return [word]


# ---------------------------------------------------------------------------
# Token expansion: hashtags + keywords + their compound splits.
# ---------------------------------------------------------------------------
@dataclass
class SemanticAnalysis:
    """Output of :func:`analyze`."""
    tokens: list[str] = field(default_factory=list)               # all expanded tokens
    splits: dict[str, list[str]] = field(default_factory=dict)    # original -> [parts]
    themes: dict[str, float] = field(default_factory=dict)        # theme -> weight
    dominant_themes: list[str] = field(default_factory=list)      # top 1\u20132
    energy: float = 0.4
    calmness: float = 0.5
    warmth: float = 0.0
    related_tags: list[str] = field(default_factory=list)
    emojis: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze(*, hashtags: list[str] | None = None,
            keywords: list[str] | None = None,
            extra_text: str = "") -> SemanticAnalysis:
    """Full semantic pass over the source signals.

    Returns a :class:`SemanticAnalysis` with every derived signal needed
    to generate a title, expanded hashtag set, and a music profile bias.
    """
    hashtags = list(hashtags or [])
    keywords = list(keywords or [])

    # Tokenize extra_text loosely
    extra_tokens = [t.lower() for t in re.findall(r"[A-Za-z]{3,}", extra_text)]

    raw_tokens: list[str] = []
    for src in (hashtags, keywords, extra_tokens):
        for t in src:
            tl = t.lower()
            if tl and tl not in raw_tokens:
                raw_tokens.append(tl)

    # Compound-split each raw token; record splits for audit.
    expanded: list[str] = []
    splits: dict[str, list[str]] = {}
    for t in raw_tokens:
        parts = split_compound(t)
        if len(parts) > 1:
            splits[t] = parts
        for p in parts:
            if p not in expanded:
                expanded.append(p)

    # Theme weighting: each token counts once per theme it belongs to.
    theme_weights: dict[str, float] = {}
    for tok in expanded:
        for theme, members in THEMES.items():
            if tok in members:
                theme_weights[theme] = theme_weights.get(theme, 0.0) + 1.0

    # Dominant themes: top 2 by weight, but only if weight >= 1.
    sorted_themes = sorted(theme_weights.items(), key=lambda kv: -kv[1])
    dominant = [t for t, w in sorted_themes if w >= 1.0][:2]

    # Average mood vector across tokens (only those we have data for).
    vecs = [_TOKEN_VECTOR.get(t) for t in expanded]
    vecs = [v for v in vecs if v is not None]
    if vecs:
        e = sum(v[0] for v in vecs) / len(vecs)
        c = sum(v[1] for v in vecs) / len(vecs)
        w = sum(v[2] for v in vecs) / len(vecs)
    else:
        e, c, w = _DEFAULT_VECTOR

    # Synonym expansion for richer FB hashtag set.
    related: list[str] = []
    seen_rel = {t.lower() for t in hashtags}
    for tok in expanded:
        for syn in SYNONYMS.get(tok, ()):
            sl = syn.lower()
            if sl not in seen_rel:
                seen_rel.add(sl)
                related.append(syn)

    # Emojis from dominant themes (max 2 distinct).
    emojis = [THEME_EMOJI[t] for t in dominant if t in THEME_EMOJI][:2]

    return SemanticAnalysis(
        tokens=expanded,
        splits=splits,
        themes=theme_weights,
        dominant_themes=dominant,
        energy=round(e, 3),
        calmness=round(c, 3),
        warmth=round(w, 3),
        related_tags=related,
        emojis=emojis,
    )


# ---------------------------------------------------------------------------
# Title generation
# ---------------------------------------------------------------------------
# Templates keyed by (theme_a, theme_b) sorted alphabetically. Single-theme
# fallbacks are keyed by (theme,). Each template uses placeholders
# ``{a}`` / ``{b}`` filled with a representative token from each theme,
# and ``{emoji}`` filled with up to 2 theme emojis.
_TEMPLATES_PAIRED: dict[tuple[str, ...], list[str]] = {
    ("flora", "water"): [
        "{a} kissed by gentle {b} {emoji}",
        "When the {b} meets the {a}",
        "{a} after the {b} — pure magic {emoji}",
        "Soft {b}, soft {a} {emoji}",
    ],
    ("flora", "wildlife"): [
        "{b} among the {a} {emoji}",
        "A tiny {b} in a sea of {a} {emoji}",
        "{a} and {b} — nature's quiet duet {emoji}",
    ],
    ("water", "wildlife"): [
        "{b} by the {a} {emoji}",
        "The {a} carries the {b} home {emoji}",
        "Where {b} meet the {a}",
    ],
    ("flora", "sky"): [
        "{a} under the {b} sky {emoji}",
        "{b} hour over the {a} {emoji}",
        "Where {a} reach for the {b}",
    ],
    ("sky", "water"): [
        "{a} hour over still {b} {emoji}",
        "{b} reflecting the {a} {emoji}",
    ],
    ("mountain", "sky"): [
        "Where the {a} meets the {b} {emoji}",
        "{b} over silent {a} {emoji}",
    ],
    ("emotion", "flora"): [
        "{a} {b} — a moment of stillness {emoji}",
        "{b} that quietly hold your {a} {emoji}",
    ],
    ("emotion", "wildlife"): [
        "{a} {b} — they will steal your heart {emoji}",
        "These {b} will melt your heart {emoji}",
        "Watch these {a} little {b} {emoji}",
    ],
    ("water", "winter"): [
        "Frozen {a} — {b} in stillness {emoji}",
    ],
    ("flora", "wilderness"): [
        "Wild {b} blooming with {a} {emoji}",
        "{a} that grow where {b} stays untouched {emoji}",
        "Pure {b}, painted with {a} {emoji}",
    ],
    ("water", "wilderness"): [
        "Pure {b} \u2014 just {a} and silence {emoji}",
        "The sound of {a} in untouched {b} {emoji}",
        "Wild {b} carved by gentle {a} {emoji}",
    ],
    ("wilderness", "wildlife"): [
        "{b} of the wild {a} {emoji}",
        "Where {b} roam free across the {a} {emoji}",
    ],
    ("sky", "wilderness"): [
        "Wild {b} under a painted {a} {emoji}",
        "{a} hour over endless {b} {emoji}",
    ],
    ("emotion", "water"): [
        "Soft {a} that the {b} carries away {emoji}",
    ],
    ("emotion", "sky"): [
        "{a} skies, {b} hearts {emoji}",
    ],
}

_TEMPLATES_SINGLE: dict[str, list[str]] = {
    "flora": [
        "Quiet bloom \u2014 a closer look at {a} {emoji}",
        "Tiny worlds inside every {a} {emoji}",
    ],
    "wildlife": [
        "Meet the {a} of the wild {emoji}",
        "A {a}'s secret morning {emoji}",
    ],
    "water": [
        "The sound of {a} \u2014 turn it up {emoji}",
        "Just {a}. Just breathe {emoji}",
    ],
    "sky": [
        "{a} hour. Hold this moment {emoji}",
        "Watching the {a} change colors {emoji}",
    ],
    "mountain": [
        "Standing where the {a} meets the sky {emoji}",
        "Silent giants \u2014 the {a} {emoji}",
    ],
    "winter": [
        "First {a} of the season {emoji}",
        "When the world goes quiet under {a} {emoji}",
    ],
    "emotion": [
        "A pure {a} moment {emoji}",
    ],
    "wilderness": [
        "Deep into wild {a} {emoji}",
        "Untouched {a} \u2014 some places stay quiet {emoji}",
        "Pure {a}, nothing else needed {emoji}",
        "Just {a}. Just stillness {emoji}",
    ],
    "night": [
        "Under a {a} sky {emoji}",
    ],
}

_GENERIC_TEMPLATES: list[str] = [
    "A quiet moment in nature {emoji}",
    "Some scenes don't need words {emoji}",
    "Stop scrolling \u2014 watch this {emoji}",
]


def _representative_token(theme: str, tokens: list[str]) -> str:
    """Pick the most representative token in ``tokens`` belonging to
    ``theme``. Falls back to the theme name itself if none match.
    """
    members = THEMES.get(theme, set())
    for t in tokens:
        if t in members:
            return t
    return theme


def generate_title(analysis: SemanticAnalysis,
                   *, seed: str | None = None) -> str:
    """Produce a single human-readable title from the analysis.

    Selection is deterministic given ``seed`` (typically the source
    filename) so the same video always gets the same title across reruns.
    """
    rng = random.Random(seed) if seed else random.Random()

    emoji = " ".join(analysis.emojis) if analysis.emojis else ""
    dominant = analysis.dominant_themes

    template_pool: list[str] = []
    template_kind = ""
    a_tok = b_tok = ""

    if len(dominant) >= 2:
        key = tuple(sorted(dominant[:2]))
        if key in _TEMPLATES_PAIRED:
            template_pool = _TEMPLATES_PAIRED[key]
            template_kind = f"paired:{key[0]}+{key[1]}"
            # In paired templates, '{a}' / '{b}' map to the alphabetically-
            # first / second theme so output is consistent.
            a_tok = _representative_token(key[0], analysis.tokens)
            b_tok = _representative_token(key[1], analysis.tokens)
    if not template_pool and dominant:
        first = dominant[0]
        if first in _TEMPLATES_SINGLE:
            template_pool = _TEMPLATES_SINGLE[first]
            template_kind = f"single:{first}"
            a_tok = _representative_token(first, analysis.tokens)
    if not template_pool:
        template_pool = _GENERIC_TEMPLATES
        template_kind = "generic"

    template = rng.choice(template_pool)
    title = template.format(a=a_tok or "moment", b=b_tok or "scene",
                            emoji=emoji).strip()
    # Title-case the first character only; keep the rest natural.
    if title and title[0].islower():
        title = title[0].upper() + title[1:]
    # Collapse spaces (in case emoji was empty).
    title = re.sub(r"\s{2,}", " ", title).strip()
    print(f"[semantic] title kind={template_kind!r} dominant={dominant} "
          f"a='{a_tok}' b='{b_tok}' emoji='{emoji}' -> '{title}'")
    return title


# ---------------------------------------------------------------------------
# Hashtag expansion
# ---------------------------------------------------------------------------
def expanded_hashtags(analysis: SemanticAnalysis,
                      filename_hashtags: list[str],
                      *, max_extra: int = 6) -> list[str]:
    """Return ``filename_hashtags`` followed by up to ``max_extra``
    semantically-related tags from the synonym map. De-duplicated,
    case-insensitively.
    """
    out = list(filename_hashtags)
    seen = {h.lower() for h in out}
    for tag in analysis.related_tags:
        if max_extra <= 0:
            break
        if tag.lower() in seen:
            continue
        out.append(tag)
        seen.add(tag.lower())
        max_extra -= 1
    return out
