"""Central configuration loaded from environment variables / GitHub secrets."""
from __future__ import annotations
import os
from dataclasses import dataclass


def _req(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


@dataclass(frozen=True)
class Config:
    # Facebook
    fb_page_id: str
    fb_page_token: str
    fb_api_version: str

    # Google Drive
    drive_folder_id: str
    drive_archive_folder_id: str   # optional; "" disables archiving
    drive_sa_json: str             # optional service-account JSON
    drive_oauth_refresh_token: str # optional OAuth user refresh token
    drive_oauth_client_id: str     # required if oauth_refresh_token set
    drive_oauth_client_secret: str # required if oauth_refresh_token set

    # Scheduling
    timezone: str
    posts_per_day_min: int
    posts_per_day_max: int
    window_start_hour: int   # local time, inclusive
    window_end_hour: int     # local time, exclusive
    slot_tolerance_minutes: int  # how close to a slot time before we fire

    # Caption building
    hashtags_per_post_min: int
    hashtags_per_post_max: int
    hashtags_file: str
    titles_file: str

    # Behaviour
    dry_run: bool


def load_config() -> Config:
    return Config(
        fb_page_id=_req("FB_PAGE_ID"),
        fb_page_token=_req("FB_PAGE_TOKEN"),
        fb_api_version=_opt("FB_API_VERSION", "v21.0"),
        drive_folder_id=_req("DRIVE_FOLDER_ID"),
        drive_archive_folder_id=_opt("DRIVE_ARCHIVE_FOLDER_ID", ""),
        drive_sa_json=os.environ.get("DRIVE_SERVICE_ACCOUNT_JSON", ""),
        drive_oauth_refresh_token=os.environ.get("DRIVE_OAUTH_REFRESH_TOKEN", "").strip(),
        drive_oauth_client_id=os.environ.get("DRIVE_OAUTH_CLIENT_ID", "").strip(),
        drive_oauth_client_secret=os.environ.get("DRIVE_OAUTH_CLIENT_SECRET", "").strip(),
        timezone=_opt("TIMEZONE", "Asia/Dhaka"),
        posts_per_day_min=int(_opt("POSTS_PER_DAY_MIN", "5")),
        posts_per_day_max=int(_opt("POSTS_PER_DAY_MAX", "7")),
        window_start_hour=int(_opt("WINDOW_START_HOUR", "9")),
        window_end_hour=int(_opt("WINDOW_END_HOUR", "22")),
        slot_tolerance_minutes=int(_opt("SLOT_TOLERANCE_MINUTES", "20")),
        hashtags_per_post_min=int(_opt("HASHTAGS_PER_POST_MIN", "8")),
        hashtags_per_post_max=int(_opt("HASHTAGS_PER_POST_MAX", "12")),
        hashtags_file=_opt("HASHTAGS_FILE", "hashtags.txt"),
        titles_file=_opt("TITLES_FILE", "titles.txt"),
        dry_run=_opt("DRY_RUN", "false").lower() in ("1", "true", "yes"),
    )
