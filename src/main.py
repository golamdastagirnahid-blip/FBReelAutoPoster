"""Entry point. Run from GitHub Actions on a cron.

Flow each invocation:
  1. Load config + ensure today's randomized schedule exists.
  2. If no slot is due now -> exit cleanly.
  3. List videos in the source Drive folder, pick one not already posted.
  4. Download -> ffmpeg light enhance.
  5. Build caption (cleaned filename + sampled hashtags).
  6. Upload to Facebook Page as a Reel (3-phase).
  7. If a service account + archive folder are configured, MOVE the file
     from source -> archive folder so it disappears from the listing.
  8. Persist state (posted.json + schedule_*.json) so the commit step
     in the workflow can push it back to the repo.

Two Drive backends are supported:
  - SA mode (recommended): set DRIVE_SERVICE_ACCOUNT_JSON. Folders can be
    fully private. Enables the archive/move feature.
  - Keyless mode: source folder must be "Anyone with link → Viewer".
    No archive support.
"""
from __future__ import annotations
import os
import random
import sys
import tempfile
import time
import traceback

from . import captions, drive, enhance as enh, facebook as fb, scheduler, state
from .config import load_config


def _is_force_post() -> bool:
    """True when the run should bypass the scheduler and post immediately.

    Triggered by env var ``FORCE_POST=true`` (set by the workflow when
    triggered manually via workflow_dispatch).
    """
    return os.environ.get("FORCE_POST", "").strip().lower() in ("1", "true", "yes")


def _pick_video(files: list[dict], posted: dict) -> dict | None:
    candidates = [f for f in files if f["id"] not in posted]
    if not candidates:
        return None
    # Random pick for variety; could be sorted by modifiedTime ascending
    # if you'd rather post oldest-first. Random keeps it humanized.
    return random.choice(candidates)


def run() -> int:
    cfg = load_config()
    salt = cfg.fb_page_id  # unique per page

    date_str, sched, tz = scheduler.ensure_today_schedule(
        tz_name=cfg.timezone,
        salt=salt,
        posts_min=cfg.posts_per_day_min,
        posts_max=cfg.posts_per_day_max,
        window_start_h=cfg.window_start_hour,
        window_end_h=cfg.window_end_hour,
    )
    print(f"[scheduler] {date_str} tz={cfg.timezone} slots={sched['slots']} done={sched.get('done', [])}")

    force = _is_force_post()
    due = None
    if force:
        print("[scheduler] FORCE_POST=true -> bypassing schedule, posting now")
    else:
        due = scheduler.due_slot(sched, date_str, tz, cfg.slot_tolerance_minutes)
        if due is None:
            print("[scheduler] no slot due; exiting")
            return 0
        print(f"[scheduler] slot due: {due.slot_time}")

    folder_id = drive.extract_folder_id(cfg.drive_folder_id)
    archive_id = drive.extract_folder_id(cfg.drive_archive_folder_id) if cfg.drive_archive_folder_id else ""

    # Decide auth mode: OAuth > SA > keyless
    sa_session = None
    has_oauth = bool(
        cfg.drive_oauth_refresh_token
        and cfg.drive_oauth_client_id
        and cfg.drive_oauth_client_secret
    )
    if has_oauth or cfg.drive_sa_json:
        from . import drive_auth
        sa_session = drive_auth.make_session(
            sa_json=cfg.drive_sa_json,
            oauth_refresh_token=cfg.drive_oauth_refresh_token,
            oauth_client_id=cfg.drive_oauth_client_id,
            oauth_client_secret=cfg.drive_oauth_client_secret,
        )
        mode = "oauth-user" if has_oauth else "service-account"
        print(f"[drive] auth={mode} ({drive_auth.whoami(sa_session)})")
        files = drive_auth.list_videos(sa_session, folder_id)
    else:
        print("[drive] auth=keyless (set DRIVE_OAUTH_REFRESH_TOKEN to enable archiving)")
        files = drive.list_videos(folder_id)

    print(f"[drive] found {len(files)} videos")
    if not files:
        print("[drive] folder is empty; nothing to post")
        return 0

    posted = state.load_posted()
    chosen = _pick_video(files, posted)
    if not chosen:
        print("[drive] all videos already posted; nothing new")
        return 0
    print(f"[drive] picked: {chosen['name']} ({chosen['id']})")

    with tempfile.TemporaryDirectory() as tmp:
        dl_dir = os.path.join(tmp, "dl")
        if sa_session is not None:
            from . import drive_auth
            raw = drive_auth.download_file(sa_session, chosen["id"], dl_dir)
        else:
            raw = drive.download_file(chosen["id"], dl_dir)
        real_name = os.path.basename(raw)
        print(f"[drive] downloaded -> {raw} ({os.path.getsize(raw)} bytes)")
        print(f"[drive] real filename: {real_name}")

        title_pool = captions.load_title_pool(cfg.titles_file)
        title = captions.clean_title(real_name, fallback_pool=title_pool)
        pool = captions.load_hashtag_pool(cfg.hashtags_file)
        tags = captions.sample_hashtags(
            pool, cfg.hashtags_per_post_min, cfg.hashtags_per_post_max,
        )
        print(f"[caption] title='{title}' tags={tags} "
              f"(hashtag_pool={len(pool)} title_pool={len(title_pool)})")

        enhanced = os.path.join(tmp, "enhanced.mp4")
        enh.enhance(
            raw, enhanced,
            watermark_text=cfg.watermark_text,
            filter_style=cfg.filter_style,
            font_file=cfg.watermark_font_file,
        )
        print(f"[enhance] -> {enhanced} ({os.path.getsize(enhanced)} bytes)")

        caption = captions.build_caption(title, tags)

        # Humanization jitter (auto/cron runs only). For manual runs we
        # post immediately, no waiting.
        if not force and not cfg.dry_run:
            jitter = random.randint(0, 240)  # 0..4 minutes
            if jitter:
                print(f"[humanize] sleeping {jitter}s before publish to vary post timing")
                time.sleep(jitter)

        fb_post_id = None
        fb_video_id = None
        if cfg.dry_run:
            print("[dry-run] would publish reel; caption follows:")
            print("---")
            print(caption)
            print("---")
        else:
            result = fb.publish_reel(
                page_id=cfg.fb_page_id,
                token=cfg.fb_page_token,
                api_version=cfg.fb_api_version,
                video_path=enhanced,
                description=caption,
                title=title,
                content_category=os.environ.get("FB_CONTENT_CATEGORY", "OTHER"),
            )
            print(f"[facebook] published: {result}")
            fb_post_id = result.get("post_id") or result.get("video_id")
            fb_video_id = result.get("video_id")

    # Archive (move source -> archive folder) ONLY after a real publish.
    archived = False
    if not cfg.dry_run and sa_session is not None and archive_id:
        from . import drive_auth
        try:
            current_parents = chosen.get("parents") or [folder_id]
            drive_auth.move_file(
                sa_session,
                file_id=chosen["id"],
                add_parent=archive_id,
                remove_parents=current_parents,
            )
            archived = True
            print(f"[archive] moved {chosen['id']} -> {archive_id}")
        except Exception as e:  # noqa: BLE001
            # Don't fail the run if archiving fails — the post already went out.
            print(f"[archive] WARN: move failed: {e}")
    elif not cfg.dry_run and not archive_id:
        print("[archive] skipped (DRIVE_ARCHIVE_FOLDER_ID not set)")
    elif not cfg.dry_run and sa_session is None:
        print("[archive] skipped (keyless mode — set DRIVE_SERVICE_ACCOUNT_JSON to enable)")

    state.mark_posted(
        chosen["id"], chosen["name"], fb_post_id,
        fb_video_id=fb_video_id if not cfg.dry_run else None,
    )
    if due is not None:
        scheduler.mark_slot_done(date_str, sched, due.slot_time)
    print(f"[state] saved (archived={archived} force={force})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] {e}")
        traceback.print_exc()
        sys.exit(1)
