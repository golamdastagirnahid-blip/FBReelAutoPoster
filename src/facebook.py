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

# Mimic the official Facebook for Android app's User-Agent so the upload
# stream looks the same as a real human tap-and-post in the mobile app.
# (FB's spam classifier weighs UA as one of many signals.) Multiple recent
# build IDs are rotated per call so we don't fingerprint as one device.
_MOBILE_UAS = [
    "Mozilla/5.0 (Linux; Android 14; SM-S918B Build/UP1A.231005.007; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/124.0.0.0 "
    "Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/465.0.0.42.93;]",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230901.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0.0.0 "
    "Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/460.0.0.34.85;]",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "[FBAN/FBIOS;FBAV/466.0.0.36.105;FBBV/612345678;FBDV/iPhone15,3;"
    "FBMD/iPhone;FBSN/iOS;FBSV/17.5;FBSS/3;FBID/phone;FBLC/en_US;FBOP/5]",
]


def _ua() -> str:
    return random.choice(_MOBILE_UAS)


def _headers(token: str | None = None, *, octet: bool = False) -> dict:
    h = {"User-Agent": _ua()}
    if token:
        h["Authorization"] = f"OAuth {token}"
    if octet:
        h["Content-Type"] = "application/octet-stream"
    return h


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
        headers=_headers(),
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
    headers = _headers(token, octet=True)
    headers.update({"offset": "0", "file_size": str(size)})
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
    content_category: str | None = None,
) -> dict:
    """Phase 3. Publish the reel immediately with full mobile-app metadata."""
    data = {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "PUBLISHED",
        "description": description,
        "access_token": token,
    }
    if title:
        data["title"] = title
    if content_category:
        # FB Reels supported categories include: BEAUTY_FASHION, BUSINESS,
        # CARS_TRUCKS, COMEDY, CUTE_ANIMALS, ENTERTAINMENT, FAMILY, FITNESS,
        # FOOD_HEALTH, HOME, LIFESTYLE, MUSIC, NEWS, OTHER, POLITICS, SCIENCE,
        # SPORTS, TECHNOLOGY, VIDEO_GAMING.
        data["content_category"] = content_category
    r = requests.post(
        f"{_graph(api_version)}/{page_id}/video_reels",
        data=data,
        headers=_headers(),
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
    content_category: str | None = None,
) -> dict:
    """Full 3-phase publish with per-phase retries + human-paced delays.

    Real users in the FB app don't fire start/upload/finish back-to-back
    instantly — there's a "tap record / preview / tap share" gap. We
    insert randomized pauses to mimic that cadence.
    """
    # Pre-tap "open composer" pause
    time.sleep(random.uniform(1.5, 4.0))

    video_id, upload_url = _retry(
        lambda: start_upload(page_id, token, api_version), label="start",
    )

    # "Selecting video / preview" pause
    time.sleep(random.uniform(2.0, 5.0))

    _retry(
        lambda: upload_bytes(upload_url, token, video_path), label="upload",
    )

    # "Reviewing caption / picking cover" pause — the longest gap, like
    # a real user typing the description before tapping Share.
    time.sleep(random.uniform(8.0, 18.0))

    result = _retry(
        lambda: finish_upload(
            page_id=page_id, token=token, api_version=api_version,
            video_id=video_id, description=description, title=title,
            content_category=content_category,
        ),
        label="finish",
    )
    result["video_id"] = video_id
    return result
