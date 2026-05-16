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
    if not os.path.exists(POSTED_PATH):
        return {}
    try:
        with open(POSTED_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


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
) -> None:
    data = load_posted()
    data[file_id] = {
        "name": name,
        "fb_post_id": fb_post_id,
        "fb_video_id": fb_video_id,
        "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
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
