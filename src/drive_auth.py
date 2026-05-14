"""Authenticated Drive operations.

Two auth methods are supported:

1. **OAuth user refresh token** (recommended; works around org policies
   that block service-account key creation). The tool acts AS YOU, so
   it can read/write any folder you own — no folder sharing needed.
2. **Service account JSON** (only when allowed by your org). The SA
   needs Editor on both source and archive folders.

In either case the module exposes the same listing / download / move
operations against Drive API v3.
"""
from __future__ import annotations
import json
import os
from typing import Iterable

from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as SACredentials
from google.auth.transport.requests import AuthorizedSession, Request

DRIVE_API = "https://www.googleapis.com/drive/v3"
SCOPES = ["https://www.googleapis.com/auth/drive"]
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
TOKEN_URI = "https://oauth2.googleapis.com/token"


def make_session_oauth(
    refresh_token: str, client_id: str, client_secret: str,
) -> AuthorizedSession:
    """Build an authenticated session from an OAuth user refresh token."""
    creds = UserCredentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    # Eager refresh so we fail fast if the token is invalid/expired.
    creds.refresh(Request())
    return AuthorizedSession(creds)


def make_session_sa(sa_json: str) -> AuthorizedSession:
    """Build an authenticated session from a service account JSON string."""
    info = json.loads(sa_json)
    creds = SACredentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds)


def make_session(
    *, sa_json: str = "",
    oauth_refresh_token: str = "",
    oauth_client_id: str = "",
    oauth_client_secret: str = "",
) -> AuthorizedSession:
    """Pick whichever auth is configured. OAuth takes precedence over SA."""
    if oauth_refresh_token and oauth_client_id and oauth_client_secret:
        return make_session_oauth(
            oauth_refresh_token, oauth_client_id, oauth_client_secret,
        )
    if sa_json:
        return make_session_sa(sa_json)
    raise ValueError(
        "No Drive auth configured: set DRIVE_OAUTH_REFRESH_TOKEN + "
        "DRIVE_OAUTH_CLIENT_ID + DRIVE_OAUTH_CLIENT_SECRET, or "
        "DRIVE_SERVICE_ACCOUNT_JSON."
    )


def list_videos(session: AuthorizedSession, folder_id: str) -> list[dict]:
    """Return every video file (one level) in the folder."""
    files: list[dict] = []
    page_token: str | None = None
    q = (
        f"'{folder_id}' in parents and trashed=false and "
        "(mimeType contains 'video/')"
    )
    while True:
        params = {
            "q": q,
            "fields": "nextPageToken, files(id,name,mimeType,size,parents,modifiedTime)",
            "pageSize": "1000",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        r = session.get(f"{DRIVE_API}/files", params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        for f in data.get("files", []):
            name = f.get("name", "")
            if name.lower().endswith(VIDEO_EXTS) or str(f.get("mimeType", "")).startswith("video/"):
                files.append(f)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return files


def download_file(session: AuthorizedSession, file_id: str, dest_dir: str) -> str:
    """Download a Drive file by ID to ``dest_dir``. Returns full saved path."""
    # Get the real (untruncated) filename
    meta = session.get(
        f"{DRIVE_API}/files/{file_id}",
        params={"fields": "name", "supportsAllDrives": "true"},
        timeout=60,
    )
    meta.raise_for_status()
    name = meta.json().get("name") or f"{file_id}.mp4"

    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, name)
    with session.get(
        f"{DRIVE_API}/files/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true"},
        stream=True,
        timeout=900,
    ) as r:
        r.raise_for_status()
        with open(path, "wb") as fp:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fp.write(chunk)
    return path


def move_file(
    session: AuthorizedSession,
    file_id: str,
    add_parent: str,
    remove_parents: Iterable[str],
) -> dict:
    """Move ``file_id`` from one or more parent folders into ``add_parent``.

    Drive permits multi-parenting, so we explicitly remove the OLD parents
    we know about. ``remove_parents`` should usually be the file's current
    parents (from the listing), or just the source folder ID.
    """
    remove_csv = ",".join(p for p in remove_parents if p)
    r = session.patch(
        f"{DRIVE_API}/files/{file_id}",
        params={
            "addParents": add_parent,
            "removeParents": remove_csv,
            "fields": "id,name,parents",
            "supportsAllDrives": "true",
        },
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"move_file failed: {r.status_code} {r.text}")
    return r.json()


def get_sa_email(sa_json: str) -> str:
    """Extract the service account's email from its JSON key (for logs)."""
    try:
        return json.loads(sa_json).get("client_email", "<unknown>")
    except (json.JSONDecodeError, AttributeError):
        return "<unparseable>"


def whoami(session: AuthorizedSession) -> str:
    """Return ``user@example.com`` for the authenticated identity."""
    try:
        r = session.get(f"{DRIVE_API}/about",
                        params={"fields": "user(emailAddress)"}, timeout=30)
        if r.ok:
            return r.json().get("user", {}).get("emailAddress", "<unknown>")
    except Exception:  # noqa: BLE001
        pass
    return "<unknown>"
