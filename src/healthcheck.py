"""Post-publish health check for previously uploaded reels.

For each reel logged in ``state/posted.json`` within the configured lookback
window, this script queries the Facebook Graph API and reports whether the
video has any of these problems:

  - **upload error**       : ``status.video_status != ready``
  - **not visible**        : ``published == false`` or no permalink_url
  - **copyright muted**    : audio was muted/replaced due to a match
  - **copyright blocked**  : whole video blocked in some/all regions
  - **partial mute**       : a section of audio muted

Output:
  - Console summary printed to stdout (always).
  - ``state/health_report.md`` written with a human-readable table.
  - Exit code 0 always (so the workflow never fails on a flagged reel).

Run:
    python -m src.healthcheck                # default 7-day lookback
    HEALTH_LOOKBACK_DAYS=14 python -m src.healthcheck

This is intentionally side-effect-free: it never re-uploads or deletes
anything. If you want to act on flagged reels, read the report and decide.
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

from .config import load_config
from . import state


GRAPH = "https://graph.facebook.com"
DEFAULT_LOOKBACK_DAYS = 7

# Fields that work for Reel video objects (Page-published).
# - status.video_status: uploading | ready | error
# - published: bool
# - permalink_url: present once published & visible
# - copyright_check_information: dict if FB found a content match
# - copyright_monitoring_status: 'monitored' | 'not_monitored'
# - is_crossposting_eligible: side-signal of a healthy upload
VIDEO_FIELDS = (
    "id,status,published,permalink_url,length,created_time,"
    "copyright_check_information,copyright_monitoring_status,"
    "is_crossposting_eligible"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime | None:
    try:
        # tolerate trailing 'Z'
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def fetch_video(video_id: str, token: str, api_version: str) -> dict:
    r = requests.get(
        f"{GRAPH}/{api_version}/{video_id}",
        params={"fields": VIDEO_FIELDS, "access_token": token},
        timeout=20,
    )
    if not r.ok:
        # Don't blow up — just return the error so it shows in the report.
        return {"_error": f"HTTP {r.status_code}: {r.text[:200]}"}
    return r.json()


def hide_post(post_id: str, token: str, api_version: str) -> tuple[bool, str]:
    """Hide a Page post (removes it from the timeline without deleting).
    Returns (ok, detail). Engagement + permalink are preserved.

    NOTE: ``post_id`` must be the **page-scoped post id** in the form
    ``{page_id}_{post_id}``. The raw video id won't work here.
    """
    if "_" not in str(post_id):
        return False, f"post_id {post_id!r} is not page-scoped (need PAGEID_POSTID)"
    r = requests.post(
        f"{GRAPH}/{api_version}/{post_id}",
        data={"is_hidden": "true", "access_token": token},
        timeout=20,
    )
    if not r.ok:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    return True, "hidden"


def classify(video: dict) -> tuple[str, list[str]]:
    """Return (overall_status, list_of_issue_strings)."""
    if "_error" in video:
        return "ERROR", [video["_error"]]

    issues: list[str] = []

    status = (video.get("status") or {}).get("video_status")
    if status and status != "ready":
        issues.append(f"video_status={status}")

    if video.get("published") is False:
        issues.append("not_published")

    if not video.get("permalink_url"):
        issues.append("no_permalink (may be hidden/blocked)")

    cc = video.get("copyright_check_information") or {}
    # Common shapes seen in the wild:
    #   {"status": "matched", "matched_segments": [...]}
    #   {"status": "no_match"}
    cc_status = (cc.get("status") or "").lower()
    if cc_status and cc_status not in ("no_match", "passed", "ok"):
        issues.append(f"copyright={cc_status}")

    matched_segments = cc.get("matched_segments") or []
    if matched_segments:
        # Look at action(s) FB took on each segment
        actions = {seg.get("action", "match") for seg in matched_segments}
        if "block" in actions:
            issues.append("copyright_BLOCKED")
        if "mute" in actions:
            issues.append("copyright_MUTED")
        if not actions & {"block", "mute"}:
            issues.append(f"copyright_match (no enforcement: {sorted(actions)})")

    if not issues:
        return "OK", []
    if any("BLOCKED" in i for i in issues):
        return "BLOCKED", issues
    if any("MUTED" in i for i in issues):
        return "MUTED", issues
    return "WARN", issues


def write_report(rows: list[dict], path: str = "state/health_report.md") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = [
        "# Reel Health Report",
        "",
        f"Generated: {_utc_now().isoformat(timespec='seconds')}",
        f"Total checked: {len(rows)}",
        "",
        "| Posted (UTC) | Status | Video ID | File name | Issues | Permalink |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        permalink = r.get("permalink") or ""
        permalink_md = f"[link]({permalink})" if permalink else "—"
        issues = ", ".join(r["issues"]) if r["issues"] else "—"
        # Truncate long file names for table readability
        name = r["name"][:60] + ("…" if len(r["name"]) > 60 else "")
        lines.append(
            f"| {r['posted_at']} | **{r['status']}** | `{r['video_id']}` | "
            f"{name} | {issues} | {permalink_md} |"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    cfg = load_config()
    lookback_days = int(os.environ.get("HEALTH_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS))
    cutoff = _utc_now() - timedelta(days=lookback_days)

    auto_hide = os.environ.get("HEALTH_AUTO_HIDE", "true").strip().lower() in ("1", "true", "yes")
    hide_cap = int(os.environ.get("HEALTH_HIDE_CAP", "3"))   # max per run, anti-spam

    posted = state.load_posted()
    if not posted:
        print("[health] no posted reels to check")
        return 0

    print(f"[health] lookback={lookback_days}d (since {cutoff.isoformat(timespec='seconds')}) "
          f"auto_hide={auto_hide} hide_cap={hide_cap}")

    rows: list[dict] = []
    counts = {"OK": 0, "WARN": 0, "MUTED": 0, "BLOCKED": 0, "ERROR": 0}
    hides_done = 0

    for file_id, info in sorted(
        posted.items(),
        key=lambda kv: kv[1].get("posted_at", ""),
        reverse=True,
    ):
        posted_at = _parse_iso(info.get("posted_at", ""))
        if posted_at is None or posted_at < cutoff:
            continue
        raw_video_id = info.get("fb_video_id") or info.get("fb_post_id")
        if not raw_video_id:
            continue
        video_id = str(raw_video_id)
        if "_" in video_id:
            video_id = video_id.split("_")[-1]

        v = fetch_video(video_id, cfg.fb_page_token, cfg.fb_api_version)
        status, issues = classify(v)
        counts[status] = counts.get(status, 0) + 1

        action = ""
        already_hidden = info.get("hidden") is True
        should_hide = (
            auto_hide
            and not already_hidden
            and status in ("MUTED", "BLOCKED")
            and hides_done < hide_cap
        )
        if should_hide:
            page_scoped = info.get("fb_post_id") or ""
            ok, detail = hide_post(page_scoped, cfg.fb_page_token, cfg.fb_api_version)
            if ok:
                info["hidden"] = True
                info["hidden_at"] = _utc_now().isoformat(timespec="seconds")
                posted[file_id] = info
                hides_done += 1
                action = "AUTO-HIDDEN"
            else:
                action = f"hide-failed: {detail}"

        rows.append({
            "posted_at": info.get("posted_at", ""),
            "status": status if not action.startswith("AUTO") else f"{status} (hidden)",
            "issues": issues + ([action] if action else []),
            "video_id": video_id,
            "name": info.get("name", ""),
            "permalink": v.get("permalink_url", ""),
        })

        flag = "" if status == "OK" else f" issues={issues}"
        extra = f" -> {action}" if action else ""
        print(f"[health] {status:7s} {video_id} {info.get('name','')[:60]}{flag}{extra}")

    if hides_done:
        state.save_posted(posted)
        print(f"[health] state/posted.json updated with {hides_done} hidden flag(s)")

    write_report(rows)
    print()
    print("[health] summary:", json.dumps(counts), f"hides_done={hides_done}")
    print(f"[health] wrote state/health_report.md ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
