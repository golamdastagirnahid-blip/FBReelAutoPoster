"""Caption builder: cleaned title from filename + filename-aware hashtags.

Source filenames frequently carry the original creator's curated tags
(e.g. ``masstiktok_naturebeautyscenery__#flowers #rain #naturebeauty.mp4``).
Those tags are the **ground truth** for the video's content, so we:

- Strip the scraper/platform prefix (``masstiktok_<handle>__``).
- Extract any well-formed ``#tag`` tokens from the remaining filename.
- Use the rest as the human-readable title.
- Build the post's hashtag set as: filename-tags FIRST, then random fill
  from ``hashtags.txt`` to reach the target count. This keeps each post's
  topic consistent with the video while still adding reach via the pool.

A defensive token-validity filter is applied so a Drive-truncated tail
(``#nat``) does not contaminate the output \u2014 only tags >=4 chars are
kept from the filename.
"""
from __future__ import annotations
import os
import random
import re

HASHTAG_LINE_RE = re.compile(r"^[#]?([A-Za-z0-9_\u00C0-\uFFFF]{2,80})\s*$")

# Known scraper / platform prefixes. Case-insensitive ONLY for the platform
# name itself (via inline ``(?i:...)``) — the username/id portion stays
# strict-lowercase so the regex CAN'T eat into the real (PascalCase or
# Sentence case) title that follows.
_KNOWN_PLATFORMS = (
    "masstiktok", "tiktok", "instagram", "ig", "youtube", "yt", "ytshorts",
    "facebook", "fb", "snaptik", "ssstik", "savefrom", "tikmate", "tmate",
    "y2mate", "x2download", "snapsave", "fdown", "fbdown",
)
# Pattern: <platform>(_<lowercase-handle-segments>)+ <2+ separator chars>
_PLATFORM_PREFIX_RE = re.compile(
    r"^(?i:" + "|".join(_KNOWN_PLATFORMS) + r")"   # platform (case-insensitive)
    r"(?:[._-]+@?[a-z0-9._-]*)+"                   # one or more _user / .id / -tag segments (LOWERCASE strict)
    r"[_\s]{2,}"                                    # terminator: __ / _<space> / etc.
)

# Generic fallback for unknown scrapers. Same shape but the leading token
# itself must also be lowercase. Won't match "My Reel" (capital M).
_GENERIC_PREFIX_RE = re.compile(
    r"^[a-z0-9]{2,}"                                # lowercase platform-ish token
    r"(?:[._-]+@?[a-z0-9._-]*){1,4}"                # 1-4 user/id segments
    r"[_\s]{2,}"                                    # terminator: __ etc.
)


def load_title_pool(path: str = "titles.txt") -> list[str]:
    """Read fallback titles, one per line. ``# `` comments and blanks ignored."""
    if not os.path.exists(path):
        return []
    out: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("# "):
                continue
            out.append(line)
    return out


def load_hashtag_pool(path: str = "hashtags.txt") -> list[str]:
    """Read the hashtag pool file. Returns deduped list without '#'."""
    if not os.path.exists(path):
        return []
    pool: list[str] = []
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            # Treat "# something" (with space) as a comment, but a lone
            # "#tag" line is a tag.
            if line.startswith("#") and (len(line) == 1 or line[1] == " "):
                continue
            m = HASHTAG_LINE_RE.match(line)
            if not m:
                continue
            tag = m.group(1)
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            pool.append(tag)
    return pool


def clean_title(
    filename: str,
    extra_prefix_patterns: list[str] | None = None,
    fallback_pool: list[str] | None = None,
    rng: random.Random | None = None,
) -> str:
    """Turn ``masstiktok_muskoluk1__ A bunch of pink baby birds hugging  #.mp4``
    into ``A bunch of pink baby birds hugging``.

    If after cleaning nothing meaningful is left (e.g. the filename was
    just hashtags), pick a random title from ``fallback_pool``.
    """
    name = os.path.splitext(os.path.basename(filename))[0]

    # 1) Try known-platform prefix first (most precise, won't over-strip).
    new = _PLATFORM_PREFIX_RE.sub("", name)
    if new != name and new.strip():
        name = new
    else:
        # 2) Apply user-supplied extra patterns
        for pat in (extra_prefix_patterns or []):
            new = re.sub(pat, "", name, flags=re.IGNORECASE)
            if new != name and new.strip():
                name = new
                break
        else:
            # 3) Fall back to generic lowercase_handle__ detector
            new = _GENERIC_PREFIX_RE.sub("", name)
            if new != name and new.strip():
                name = new

    # Replace underscores with spaces
    name = name.replace("_", " ")
    # Strip ALL #hashtag tokens — they're unreliable / truncated in filenames
    name = re.sub(r"#\S*", "", name)
    # Collapse whitespace and trim punctuation/whitespace edges
    name = re.sub(r"\s{2,}", " ", name).strip(" -–—:|·\t")
    # Capitalize first letter for nicer presentation
    if name and name[0].islower():
        name = name[0].upper() + name[1:]

    # If nothing meaningful is left (or it's a single short token like
    # 'Bird'), prefer a random fallback title from the pool.
    is_weak = (not name) or (len(name) < 5) or (len(name.split()) < 2)
    if is_weak and fallback_pool:
        rng = rng or random.Random()
        return rng.choice(fallback_pool)
    return name or "Reel"


# Match #tag tokens. Tag = letters+digits+underscore, 2-50 chars. We
# defensively require >=4 chars when extracting from filenames so a
# Drive-truncated tail like '#nat' is dropped.
_FILENAME_HASHTAG_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{2,49})")


def extract_filename_hashtags(filename: str, *, min_len: int = 4) -> list[str]:
    """Pull well-formed ``#tag`` tokens out of a source filename.

    De-duplicated, original case preserved (FB renders them case-insensitively
    so casing only affects display). Tags shorter than ``min_len`` are
    rejected to guard against Drive's display-name truncation.
    """
    name = os.path.basename(filename)
    out: list[str] = []
    seen: set[str] = set()
    for raw in _FILENAME_HASHTAG_RE.findall(name):
        if len(raw) < min_len:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


_PLATFORM_NAMES_LOWER = {p.lower() for p in _KNOWN_PLATFORMS}
# Common scraper noise tokens we don't want as content keywords.
_NOISE_TOKENS = _PLATFORM_NAMES_LOWER | {
    "video", "videos", "download", "downloaded", "final", "copy", "copies",
    "edit", "edited", "watermark", "nowatermark", "hd", "fhd", "uhd", "mp4",
}


def extract_filename_keywords(filename: str) -> list[str]:
    """Return clean keyword tokens from the filename's content area.

    The filename is split on the scraper terminator ``__`` (if present) into
    a *handle/category* half and a *content* half. We extract alphabetic
    tokens (>=4 chars) from BOTH halves but reject:

      - platform names (``masstiktok``, ``tiktok``, ``instagram``...)
      - common scraper noise tokens (``download``, ``hd``, ``mp4``...)

    This gives us tokens like ``naturebeautyscenery`` as content keywords
    even when they sit in the "handle" position of the original prefix.
    Hashtag content is removed first \u2014 those are returned separately by
    ``extract_filename_hashtags``.
    """
    name = os.path.splitext(os.path.basename(filename))[0]
    # Strip hashtag tokens entirely so their bodies don't double-count.
    name = re.sub(r"#\S*", " ", name)
    out: list[str] = []
    seen: set[str] = set()
    for tok in re.findall(r"[A-Za-z]{4,}", name):
        key = tok.lower()
        if key in seen or key in _NOISE_TOKENS:
            continue
        seen.add(key)
        out.append(key)
    return out


def sample_hashtags(pool: list[str], count_min: int, count_max: int,
                    rng: random.Random | None = None,
                    primary: list[str] | None = None) -> list[str]:
    """Build a hashtag list of size in ``[count_min, count_max]``.

    If ``primary`` (e.g. tags lifted from the source filename) is provided,
    those are placed first and the remainder is filled by random sampling
    from ``pool`` (skipping pool entries that duplicate a primary tag).
    Original casing of ``primary`` tags is preserved.
    """
    rng = rng or random.Random()
    primary = primary or []
    seen: set[str] = {p.lower() for p in primary}
    out: list[str] = list(primary)

    target = rng.randint(
        min(count_min, max(count_min, len(out))),
        max(count_max, len(out)),
    )
    if not pool:
        return out[:target]

    # Pool entries that aren't already in `out`
    pool_filtered = [p for p in pool if p.lower() not in seen]
    rng.shuffle(pool_filtered)
    while len(out) < target and pool_filtered:
        tag = pool_filtered.pop()
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out


def build_caption(title: str, hashtags: list[str]) -> str:
    parts = [title.strip()]
    if hashtags:
        parts.append(" ".join(f"#{h.lstrip('#')}" for h in hashtags))
    return "\n\n".join(p for p in parts if p)
