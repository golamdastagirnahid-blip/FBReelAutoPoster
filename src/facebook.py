"""Facebook Page Reels upload (3-phase Resumable Upload API).

Docs: https://developers.facebook.com/docs/video-api/guides/publishing
Phases:
  1) start  -> get video_id and upload_url
  2) upload -> POST raw bytes to upload_url with Authorization: OAuth <token>
  3) finish -> publish with description / metadata
"""
from __future__ import annotations
import os
import time
import requests

GRAPH = "https://graph.facebook.com"


class FacebookError(RuntimeError):
    pass


def _graph(api_version: str) -> str:
    return f"{GRAPH}/{api_version}"


def start_upload(page_id: str, token: str, api_version: str) -> tuple[str, str]:
    """Phase 1. Returns (video_id, upload_url)."""
    r = requests.post(
        f"{_graph(api_version)}/{page_id}/video_reels",
        data={"upload_phase": "start", "access_token": token},
        timeout=60,
    )
    if not r.ok:
        raise FacebookError(f"start failed: {r.status_code} {r.text}")
    data = r.json()
    vid = data.get("video_id")
    url = data.get("upload_url")
    if not vid or not url:
        raise FacebookError(f"start missing fields: {data}")
    return vid, url


def upload_bytes(upload_url: str, token: str, path: str) -> None:
    """Phase 2. Streaming upload of the file's bytes."""
    size = os.path.getsize(path)
    headers = {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(size),
        "Content-Type": "application/octet-stream",
    }
    with open(path, "rb") as fp:
        r = requests.post(upload_url, headers=headers, data=fp, timeout=900)
    if not r.ok:
        raise FacebookError(f"upload failed: {r.status_code} {r.text}")
    try:
        ok = r.json().get("success", True)
    except ValueError:
        ok = True
    if not ok:
        raise FacebookError(f"upload not successful: {r.text}")


def finish_upload(
    page_id: str,
    token: str,
    api_version: str,
    video_id: str,
    description: str,
    title: str | None = None,
) -> dict:
    """Phase 3. Publish the reel immediately."""
    data = {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "PUBLISHED",
        "description": description,
        "access_token": token,
    }
    if title:
        data["title"] = title
    r = requests.post(
        f"{_graph(api_version)}/{page_id}/video_reels",
        data=data,
        timeout=120,
    )
    if not r.ok:
        raise FacebookError(f"finish failed: {r.status_code} {r.text}")
    return r.json()


def publish_reel(
    page_id: str,
    token: str,
    api_version: str,
    video_path: str,
    description: str,
    title: str | None = None,
) -> dict:
    """Full 3-phase publish. Returns finish payload incl. post_id."""
    video_id, upload_url = start_upload(page_id, token, api_version)
    upload_bytes(upload_url, token, video_path)
    # Tiny pacing wait so FB's internal pipeline is happy
    time.sleep(2)
    result = finish_upload(
        page_id=page_id,
        token=token,
        api_version=api_version,
        video_id=video_id,
        description=description,
        title=title,
    )
    result["video_id"] = video_id
    return result
