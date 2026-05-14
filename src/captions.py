"""Caption builder: cleaned title from filename + sampled hashtag pool.

We deliberately do NOT trust hashtags embedded in Drive filenames because
Drive truncates display names to ~50 chars and the trailing tags get cut
off mid-word. Instead we:

- Strip a configurable junk prefix from the filename (e.g. scraper output
  like ``masstiktok_muskoluk1__``).
- Use the rest of the cleaned filename as the human-readable title.
- Randomly sample N hashtags from ``hashtags.txt`` for every post so each
  caption looks fresh and human-curated.
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


def sample_hashtags(pool: list[str], count_min: int, count_max: int,
                    rng: random.Random | None = None) -> list[str]:
    if not pool:
        return []
    rng = rng or random.Random()
    n = rng.randint(min(count_min, len(pool)), min(count_max, len(pool)))
    return rng.sample(pool, n)


def build_caption(title: str, hashtags: list[str]) -> str:
    parts = [title.strip()]
    if hashtags:
        parts.append(" ".join(f"#{h.lstrip('#')}" for h in hashtags))
    return "\n\n".join(p for p in parts if p)
