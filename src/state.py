"""Persistent state stored as JSON files in the repo.

- state/posted.json   : { drive_file_id: { posted_at, fb_post_id, name } }
- state/schedule_YYYY-MM-DD.json : { slots: ["09:14", "12:42", ...], done: [...] }

The workflow commits the state directory back to the repo each run so
deduplication and schedules persist across runs (free, no external DB).
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from typing import Any

STATE_DIR = "state"
POSTED_PATH = os.path.join(STATE_DIR, "posted.json")


def _ensure_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def load_posted() -> dict[str, Any]:
    """Load the dedup ledger. If the file is missing return ``{}``.

    If the file exists but is corrupt (truncated JSON, wrong type, OS
    error), we **never silently return an empty dict** \u2014 doing so would
    cause every previously-posted video to be re-uploaded as a duplicate.
    Instead we:
      1. Rename the corrupt file to ``posted.json.corrupt-<utcstamp>`` so
         it is preserved for manual recovery.
      2. Raise ``StateCorruptError`` so the caller (main / healthcheck)
         can fail loudly rather than silently nuking the dedup state.
    """
    if not os.path.exists(POSTED_PATH):
        return {}
    try:
        with open(POSTED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        backup = POSTED_PATH + ".corrupt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            os.replace(POSTED_PATH, backup)
            print(f"[state] WARN: posted.json corrupt ({e}); preserved as {backup}")
        except OSError as backup_err:
            print(f"[state] WARN: posted.json corrupt and could not be backed up: {backup_err}")
        raise StateCorruptError(
            f"posted.json corrupt: {e}. Backed up to {backup}. Refusing to "
            f"continue with empty dedup state to prevent mass re-posting."
        ) from e
    if not isinstance(data, dict):
        backup = POSTED_PATH + ".corrupt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        os.replace(POSTED_PATH, backup)
        raise StateCorruptError(
            f"posted.json is not a JSON object (got {type(data).__name__}). "
            f"Backed up to {backup}."
        )
    return data


class StateCorruptError(RuntimeError):
    """Raised when the persisted state file is unreadable / wrong shape."""


def save_posted(data: dict[str, Any]) -> None:
    _ensure_dir()
    tmp = POSTED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, POSTED_PATH)


def mark_posted(
    file_id: str,
    name: str,
    fb_post_id: str | None,
    fb_video_id: str | None = None,
    music_track: dict | None = None,
    caption: str | None = None,
) -> None:
    """Record a successful publish. ``music_track`` (if set) is the dict
    from ``music.Track.to_dict()`` and forms the per-post audit trail
    required for monetization / license disputes (which CC track, who,
    license URL, etc.)."""
    data = load_posted()
    entry: dict[str, Any] = {
        "name": name,
        "fb_post_id": fb_post_id,
        "fb_video_id": fb_video_id,
        "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if music_track is not None:
        entry["music_track"] = music_track
    if caption is not None:
        entry["caption"] = caption
    data[file_id] = entry
    save_posted(data)


def schedule_path(date_str: str) -> str:
    return os.path.join(STATE_DIR, f"schedule_{date_str}.json")


def load_schedule(date_str: str) -> dict | None:
    p = schedule_path(date_str)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_schedule(date_str: str, sched: dict) -> None:
    _ensure_dir()
    p = schedule_path(date_str)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sched, f, indent=2, sort_keys=True)
    os.replace(tmp, p)
