"""Google Drive helpers — **keyless**.

You only give the tool a public folder share URL
('Anyone with the link → Viewer'). No API key, no service account, no OAuth.

- Listing: uses the public ``embeddedfolderview`` endpoint, which exposes
  *all* files in the folder (the regular folder URL is JS-paginated and
  initially ships only ~50). Display names there are still truncated to
  ~50 chars, but that's fine — we treat the file ID as the dedup key and
  let gdown fetch the real filename at download time.
- Downloading: uses `gdown`, which queries Drive for the true (full)
  filename. Handles the large-file confirm token automatically.
"""
from __future__ import annotations
import html as _html
import os
import re
import requests
import gdown

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
_FOLDER_ID_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# embeddedfolderview HTML structure per file:
#   <a href="https://drive.google.com/file/d/<FILE_ID>/view?usp=drive_web" ...>
#     ...
#     <div class="flip-entry-title">filename.ext</div>
#   </a>
_FILE_LINK_RE = re.compile(
    r'href="https://drive\.google\.com/file/d/(?P<id>[A-Za-z0-9_-]+)/[^"]*"'
    r'.*?flip-entry-title">(?P<name>[^<]+)</div>',
    re.DOTALL,
)


def extract_folder_id(value: str) -> str:
    """Accept a raw folder ID or any Drive sharing URL."""
    m = _FOLDER_ID_RE.search(value)
    if m:
        return m.group(1)
    return value.strip()


def list_videos(folder_id: str) -> list[dict]:
    """List every video file in a public Drive folder (one level).

    Returns ``[{id, name, mimeType}, ...]``. ``name`` may be truncated by
    Drive (~50 chars) — ``id`` is the source of truth.
    """
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    r = requests.get(url, headers={"User-Agent": _UA}, timeout=60)
    r.raise_for_status()
    text = r.text

    seen: set[str] = set()
    out: list[dict] = []
    for m in _FILE_LINK_RE.finditer(text):
        fid = m.group("id")
        if fid in seen:
            continue
        name = _html.unescape(m.group("name")).strip()
        if not name.lower().endswith(VIDEO_EXTS):
            continue
        seen.add(fid)
        out.append({"id": fid, "name": name, "mimeType": "video/*"})
    return out


def download_file(file_id: str, dest_dir: str) -> str:
    """Download a public Drive file by ID into ``dest_dir``.

    Returns the actual saved path. gdown queries Drive for the real
    (untruncated) filename, so the on-disk name has the full hashtag
    list — important for metadata extraction.
    """
    os.makedirs(dest_dir, exist_ok=True)
    # Trailing separator tells gdown to keep the original filename.
    out_prefix = os.path.join(dest_dir, "")
    # gdown 6.x removed `fuzzy`; pass only stable kwargs.
    saved = gdown.download(
        id=file_id, output=out_prefix, quiet=True
    )
    if not saved or not os.path.exists(saved):
        raise RuntimeError(f"gdown failed to download file_id={file_id}")
    return saved
