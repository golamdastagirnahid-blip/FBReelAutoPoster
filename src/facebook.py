"""Facebook Page Reels upload (3-phase Resumable Upload API).

Docs: https://developers.facebook.com/docs/video-api/guides/publishing
Phases:
  1) start  -> get video_id and upload_url
  2) upload -> POST raw bytes to upload_url with Authorization: OAuth <token>
  3) finish -> publish with description / metadata

All three phases are wrapped in an exponential-backoff retry so that
transient errors (429 rate-limit, 500/502/503/504) don't fail the run.
"""
from __future__ import annotations
import os
import random
import time
import requests

GRAPH = "https://graph.facebook.com"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FacebookError(RuntimeError):
    pass


def _graph(api_version: str) -> str:
    return f"{GRAPH}/{api_version}"


def _retry(fn, *, label: str, attempts: int = 4, base_sleep: float = 2.0):
    """Run ``fn()`` with exponential backoff + jitter on transient HTTP errors.

    ``fn`` must raise ``FacebookError`` whose message embeds the status code
    via the canonical "<phase> failed: <status> <body>" format, OR may raise
    ``requests.RequestException`` for network errors.
    """
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except requests.RequestException as e:
            last_exc = e
            transient = True
        except FacebookError as e:
            last_exc = e
            msg = str(e)
            transient = any(f" {s} " in msg for s in (str(c) for c in RETRYABLE_STATUS))
            if not transient:
                raise
        if i == attempts:
            break
        sleep = base_sleep * (2 ** (i - 1)) + random.uniform(0, 1.5)
        print(f"[facebook] {label} transient failure (attempt {i}/{attempts}): "
              f"{last_exc}; retrying in {sleep:.1f}s")
        time.sleep(sleep)
    raise FacebookError(f"{label} exhausted retries: {last_exc}")


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
    """Full 3-phase publish with per-phase retries. Returns finish payload."""
    video_id, upload_url = _retry(
        lambda: start_upload(page_id, token, api_version), label="start",
    )
    _retry(
        lambda: upload_bytes(upload_url, token, video_path), label="upload",
    )
    # Tiny pacing wait so FB's internal pipeline is happy
    time.sleep(2)
    result = _retry(
        lambda: finish_upload(
            page_id=page_id, token=token, api_version=api_version,
            video_id=video_id, description=description, title=title,
        ),
        label="finish",
    )
    result["video_id"] = video_id
    return result
