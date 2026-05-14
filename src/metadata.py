"""Extract title + hashtags + description from video metadata / filename.

Strategy (in order):
1. Parse `ffprobe` MP4 tags: `title`, `comment`, `description`, `keywords`,
   `synopsis`. Hashtags can be inside any field as `#word` tokens.
2. Fall back to the file's NAME (without extension). Hashtags embedded in
   the filename are extracted, the remainder becomes the title.

Filename convention (recommended when no MP4 tags present):
    My Awesome Reel Title #motivation #mindset #grow.mp4
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from dataclasses import dataclass

HASHTAG_RE = re.compile(r"#([A-Za-z0-9_\u00C0-\uFFFF]{2,50})")


@dataclass
class ReelMeta:
    title: str
    hashtags: list[str]
    description: str

    def caption(self) -> str:
        """Build the final FB Reel description: title + tags."""
        parts = [self.title.strip()]
        if self.description and self.description.strip() != self.title.strip():
            parts.append(self.description.strip())
        if self.hashtags:
            tags = " ".join(f"#{h.lstrip('#')}" for h in self.hashtags)
            parts.append(tags)
        return "\n\n".join(p for p in parts if p)


def _ffprobe_tags(path: str) -> dict:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", path,
            ],
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    try:
        data = json.loads(out.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {}
    tags: dict = {}
    fmt_tags = (data.get("format") or {}).get("tags") or {}
    tags.update({k.lower(): v for k, v in fmt_tags.items()})
    for s in data.get("streams") or []:
        for k, v in (s.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    return tags


def _split_hashtags(text: str) -> tuple[str, list[str]]:
    """Return (text_without_hashtags, hashtags_in_order_deduped)."""
    if not text:
        return "", []
    seen: set[str] = set()
    tags: list[str] = []
    for m in HASHTAG_RE.finditer(text):
        tag = m.group(1)
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    cleaned = HASHTAG_RE.sub("", text).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned, tags


def extract(path: str, fallback_name: str | None = None) -> ReelMeta:
    tags = _ffprobe_tags(path)

    title_raw = (
        tags.get("title")
        or tags.get("itunes_name")
        or ""
    ).strip()
    desc_raw = (
        tags.get("description")
        or tags.get("synopsis")
        or tags.get("comment")
        or ""
    ).strip()
    kw_raw = (
        tags.get("keywords")
        or tags.get("itunes_keywords")
        or ""
    ).strip()

    # If MP4 has nothing useful, use filename
    if not title_raw:
        base = os.path.splitext(fallback_name or os.path.basename(path))[0]
        # normalize separators
        base = base.replace("_", " ").strip()
        title_raw = base

    title, t_tags = _split_hashtags(title_raw)
    desc, d_tags = _split_hashtags(desc_raw)

    kw_tags: list[str] = []
    if kw_raw:
        # keywords field is usually comma or semicolon separated
        for token in re.split(r"[,;\n]+", kw_raw):
            token = token.strip().lstrip("#")
            if 2 <= len(token) <= 50:
                kw_tags.append(token)

    # Merge hashtags preserving order, dedup case-insensitively
    seen: set[str] = set()
    merged: list[str] = []
    for tag in t_tags + d_tags + kw_tags:
        k = tag.lower()
        if k not in seen:
            seen.add(k)
            merged.append(tag)

    if not title:
        title = os.path.splitext(fallback_name or os.path.basename(path))[0].replace("_", " ")

    return ReelMeta(title=title.strip(), hashtags=merged, description=desc.strip())
