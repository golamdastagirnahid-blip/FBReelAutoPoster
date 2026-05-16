"""Preflight checks — runs before main.py to fail fast on misconfiguration.

Verifies (in order):

1. **Required env vars** are present.
2. **Facebook**: page token is valid AND it's for the expected page_id.
3. **Google Drive**: OAuth refresh token works (mints a fresh access token
   + calls /about) OR service-account JSON parses, then lists the source
   folder.
4. **Archive folder** (if set) is readable + the SA/user has write rights.
5. **Watermark font** file exists if a watermark is configured.

Designed to be cheap (a handful of HEAD/GET calls) so it can run on every
cron tick without burning quota.

Run from CLI:

    python -m src.preflight                # full check
    python -m src.preflight --quiet        # only print on failure
"""
from __future__ import annotations
import argparse
import os
import sys

import requests

from .config import load_config


GRAPH = "https://graph.facebook.com"


class PreflightError(RuntimeError):
    pass


def _redact(s: str, keep: int = 4) -> str:
    if not s:
        return "<empty>"
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}...{s[-keep:]} (len={len(s)})"


def _ok(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"  [OK]   {label}{suffix}")


def _info(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"  [info] {label}{suffix}")


def _fail(label: str, detail: str) -> None:
    raise PreflightError(f"{label}: {detail}")


def check_fb(page_id: str, token: str, api_version: str) -> None:
    """Confirm the token can read the target Page directly.

    We query ``/{page_id}`` (not ``/me``) because System User tokens resolve
    ``/me`` to the System User (which has no Page-only fields like
    ``category``). Hitting the page id directly works for both Page tokens
    and System User tokens that have the page assigned.
    """
    r = requests.get(
        f"{GRAPH}/{api_version}/{page_id}",
        params={"fields": "id,name,category,fan_count", "access_token": token},
        timeout=20,
    )
    if not r.ok:
        # Retry without fields that might be unavailable on some accounts
        r = requests.get(
            f"{GRAPH}/{api_version}/{page_id}",
            params={"fields": "id,name", "access_token": token},
            timeout=20,
        )
    if not r.ok:
        _fail("FB token", f"GET /{page_id} failed: {r.status_code} {r.text[:300]}")
    pg = r.json()
    if str(pg.get("id")) != str(page_id):
        _fail(
            "FB token",
            f"unexpected response id={pg.get('id')} for FB_PAGE_ID={page_id}",
        )
    _ok(
        "FB Page token",
        f"page='{pg.get('name')}' id={pg.get('id')}"
        + (f" category={pg.get('category')}" if pg.get("category") else "")
        + (f" fans={pg.get('fan_count')}" if pg.get("fan_count") else ""),
    )

    # Also check publish_video permission via /me/permissions (best-effort)
    try:
        rp = requests.get(
            f"{GRAPH}/{api_version}/me/permissions",
            params={"access_token": token},
            timeout=15,
        )
        if rp.ok:
            granted = {p["permission"] for p in rp.json().get("data", []) if p.get("status") == "granted"}
            need = {"pages_manage_posts", "pages_read_engagement", "pages_show_list"}
            missing = need - granted
            if missing:
                _info("FB permissions", f"granted={sorted(granted)} possibly missing={sorted(missing)} (some tokens don't expose this — ok to ignore if posting works)")
            else:
                _ok("FB permissions", f"granted includes {sorted(need)}")
    except requests.RequestException:
        pass  # non-fatal


def check_drive(cfg) -> None:
    has_oauth = bool(cfg.drive_oauth_refresh_token and cfg.drive_oauth_client_id and cfg.drive_oauth_client_secret)
    has_sa = bool(cfg.drive_sa_json)

    if not has_oauth and not has_sa:
        _info("Drive auth", "keyless mode (folders must be 'Anyone with link'); archiving DISABLED")
        return

    from . import drive_auth, drive
    try:
        session = drive_auth.make_session(
            sa_json=cfg.drive_sa_json,
            oauth_refresh_token=cfg.drive_oauth_refresh_token,
            oauth_client_id=cfg.drive_oauth_client_id,
            oauth_client_secret=cfg.drive_oauth_client_secret,
        )
    except Exception as e:  # noqa: BLE001
        _fail("Drive auth", f"make_session failed: {e}")

    who = drive_auth.whoami(session)
    mode = "oauth-user" if has_oauth else "service-account"
    _ok(f"Drive auth ({mode})", f"as {who}")

    # Verify source folder is accessible & list 1 file
    folder_id = drive.extract_folder_id(cfg.drive_folder_id)
    try:
        files = drive_auth.list_videos(session, folder_id)
    except Exception as e:  # noqa: BLE001
        _fail("Drive source folder", f"list failed for id={folder_id}: {e}")
    if not files:
        _info("Drive source folder", f"id={folder_id} contains 0 videos (nothing to post)")
    else:
        _ok("Drive source folder", f"id={folder_id} contains {len(files)} videos")

    # Verify archive folder if configured
    if cfg.drive_archive_folder_id:
        archive_id = drive.extract_folder_id(cfg.drive_archive_folder_id)
        # Probe by trying to read metadata
        r = session.get(
            f"{drive_auth.DRIVE_API}/files/{archive_id}",
            params={"fields": "id,name,capabilities(canAddChildren)", "supportsAllDrives": "true"},
            timeout=20,
        )
        if not r.ok:
            _fail("Drive archive folder", f"GET /files/{archive_id} failed: {r.status_code} {r.text[:200]}")
        meta = r.json()
        can_write = meta.get("capabilities", {}).get("canAddChildren", False)
        if not can_write:
            _fail("Drive archive folder", f"id={archive_id} ('{meta.get('name')}') — "
                  f"current user lacks write/Editor permission; cannot archive posts")
        _ok("Drive archive folder", f"id={archive_id} name='{meta.get('name')}' writable=yes")
    else:
        _info("Drive archive folder", "not set — archiving DISABLED (videos will stay in source)")


def check_watermark_font(cfg) -> None:
    if not cfg.watermark_text:
        _info("Watermark", "WATERMARK_TEXT empty — watermark disabled")
        return
    from .enhance import _default_font  # noqa: PLC2701
    font = cfg.watermark_font_file or _default_font()
    if not os.path.exists(font):
        _fail("Watermark font", f"text='{cfg.watermark_text}' but font file not found at {font}")
    _ok("Watermark", f"text='{cfg.watermark_text}' font={font}")


def check_jamendo() -> None:
    """Connectivity + credentials check ONLY. The actual track is chosen
    per-video by ``music_match.build_profile`` + ``fetch_best_music_for_video``
    after the source filename's hashtags + keywords have been semantically
    analyzed \u2014 this preflight does NOT pre-select any music.

    We just verify:
      * ``JAMENDO_CLIENT_ID`` works against the API.
      * At least one commercial-OK (CC0/CC-BY) track is reachable.

    The probe uses a deliberately-broad tag ladder; it is unrelated to
    what the per-video matcher will actually search for at runtime.
    """
    client_id = os.environ.get("JAMENDO_CLIENT_ID", "").strip()
    if not client_id:
        _info("Music (Jamendo)", "JAMENDO_CLIENT_ID not set — music replacement disabled")
        return
    print("[music] preflight = connectivity probe only "
          "(real per-video selection happens in main.py after semantic analysis)")
    # Probe ladder: broad-but-likely-to-hit. Not used for actual music pick.
    stages = ["instrumental,ambient", "ambient", ""]
    from . import music as music_mod
    tracks = []
    matched_stage = None
    for stg in stages:
        try:
            tracks = music_mod.search_tracks(client_id, stg)
        except Exception as e:  # noqa: BLE001
            _fail("Music (Jamendo)", f"search failed at stage {stg!r}: {e}")
        if tracks:
            matched_stage = stg or "<no-tags broad>"
            break
    if not tracks:
        _fail("Music (Jamendo)",
              "no commercial-OK (CC0/CC-BY) tracks reachable from any "
              "probe stage. Check client id permissions or Jamendo status.")
    sample = tracks[0]
    _ok("Music (Jamendo)",
        f"connectivity ok ({len(tracks)} commercial-OK tracks reachable via "
        f"probe stage {matched_stage!r}); per-video matcher will choose the "
        f"actual track later. Sample: '{sample.name}' by "
        f"{sample.artist} [{sample.license_short}]")


def check_filter_style(cfg) -> None:
    from .enhance import FILTER_PRESETS  # noqa: PLC2701
    valid = {"random", *FILTER_PRESETS}
    if cfg.filter_style not in valid:
        _fail("FILTER_STYLE", f"got {cfg.filter_style!r}; must be one of {sorted(valid)}")
    _ok("FILTER_STYLE", cfg.filter_style)


def run(quiet: bool = False) -> int:
    print("=" * 60)
    print("Preflight checks")
    print("=" * 60)

    try:
        cfg = load_config()
    except KeyError as e:
        print(f"  [FAIL] Missing required env var: {e}", file=sys.stderr)
        return 2

    print("  Secrets loaded (redacted):")
    print(f"    FB_PAGE_ID                : {cfg.fb_page_id}")
    print(f"    FB_PAGE_TOKEN             : {_redact(cfg.fb_page_token)}")
    print(f"    DRIVE_FOLDER_ID           : {cfg.drive_folder_id}")
    print(f"    DRIVE_ARCHIVE_FOLDER_ID   : {cfg.drive_archive_folder_id or '<unset>'}")
    print(f"    DRIVE_OAUTH_CLIENT_ID     : {_redact(cfg.drive_oauth_client_id, 8)}")
    print(f"    DRIVE_OAUTH_CLIENT_SECRET : {_redact(cfg.drive_oauth_client_secret)}")
    print(f"    DRIVE_OAUTH_REFRESH_TOKEN : {_redact(cfg.drive_oauth_refresh_token)}")
    print(f"    DRIVE_SERVICE_ACCOUNT_JSON: {'<set>' if cfg.drive_sa_json else '<unset>'}")
    print(f"    TIMEZONE                  : {cfg.timezone}")
    print(f"    POSTS_PER_DAY             : {cfg.posts_per_day_min}-{cfg.posts_per_day_max}")
    print(f"    WINDOW                    : {cfg.window_start_hour:02d}:00-{cfg.window_end_hour:02d}:00")
    print(f"    FILTER_STYLE              : {cfg.filter_style}")
    print(f"    WATERMARK_TEXT            : {cfg.watermark_text or '<unset>'}")
    print(f"    DRY_RUN                   : {cfg.dry_run}")
    print(f"    FORCE_POST                : {os.environ.get('FORCE_POST', '<unset>')}")
    print()

    try:
        check_fb(cfg.fb_page_id, cfg.fb_page_token, cfg.fb_api_version)
        check_drive(cfg)
        check_watermark_font(cfg)
        check_filter_style(cfg)
        check_jamendo()
    except PreflightError as e:
        print(f"\n  [FAIL] {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n  [FAIL] Unexpected preflight error: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        return 1

    print()
    print("All preflight checks passed. Safe to proceed.")
    print("=" * 60)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    return run(quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
