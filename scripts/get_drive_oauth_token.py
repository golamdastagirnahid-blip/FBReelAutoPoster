"""One-time helper: obtain a Drive OAuth refresh token for the runner.

Run this **locally** on your own machine. It opens your default browser,
asks you to log into the Google account that owns your Drive folders,
asks for permission to read/write Drive, and then prints a refresh
token + reminds you which three GitHub secrets to set.

PREREQUISITES (Google Cloud Console, ~3 min, one-time):
  1. Create or pick a Google Cloud project (any one works — even a
     brand-new empty project).
  2. Enable the Drive API:
     https://console.cloud.google.com/apis/library/drive.googleapis.com
  3. Configure the OAuth consent screen:
     APIs & Services -> OAuth consent screen
       - User type: "External" (or "Internal" if your account is in a
         Google Workspace org — Internal avoids the 7-day token expiry).
       - Fill the required app name + your email; you can leave the
         rest blank.
       - Under "Test users", click "Add users" and add YOUR OWN email
         (the one whose Drive folders the tool will use). Save.
  4. Create the OAuth Client:
     APIs & Services -> Credentials -> Create credentials
       -> OAuth client ID -> Application type: **Desktop app** -> name
       it (e.g. "fb-reel-poster") -> Create.
     Click DOWNLOAD JSON and save the file somewhere.

USAGE:
    pip install -r requirements.txt
    python scripts/get_drive_oauth_token.py --client-secrets path/to/credentials.json

The script will open a browser tab on http://localhost:8765, you log in,
grant access, then come back to the terminal to grab the printed token.

CAVEAT — token expiry:
  If your OAuth consent screen is in **Testing** status (External user
  type), refresh tokens currently expire after **7 days**. To avoid
  this, either:
    - Set User type to "Internal" (only if you're in a Workspace org).
    - Or click "Publish App" on the consent screen (no Google review is
      required just to escape the 7-day limit for sensitive scopes when
      you stay below 100 users; verification is only needed if you'd
      let other people authorise the app).
"""
from __future__ import annotations
import argparse
import json
import socket
import sys
import webbrowser

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _find_free_port() -> int:
    """Ask the OS for an unused TCP port so we never collide with a
    leftover server from a previous run."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--client-secrets", required=True,
        help="Path to the OAuth client JSON downloaded from GCP Console",
    )
    ap.add_argument(
        "--port", type=int, default=0,
        help="Local port for the OAuth callback (0 = pick a free port)",
    )
    args = ap.parse_args()

    port = args.port if args.port else _find_free_port()
    redirect_uri = f"http://localhost:{port}/"

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, SCOPES)
    flow.redirect_uri = redirect_uri
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent",
    )

    print()
    print("=" * 72)
    print("STEP 1 - Open this URL in any browser (Chrome / Edge / Firefox):")
    print("=" * 72)
    print()
    print(auth_url)
    print()
    print("=" * 72)
    print("STEP 2 - Log in with the Google account that owns your Drive folders.")
    print("STEP 3 - On 'Google hasn't verified this app':")
    print("           Advanced  ->  Go to <your app name> (unsafe)  ->  Continue.")
    print("STEP 4 - Click Allow.")
    print("=" * 72)
    print()
    try:
        webbrowser.open(auth_url, new=1, autoraise=True)
    except Exception:
        pass
    print(f"Waiting for the OAuth callback on {redirect_uri} ...")
    print("(This terminal will unblock automatically once you click Allow.)")
    print()

    # ``access_type=offline`` + ``prompt=consent`` ensures we get a
    # refresh_token even if the user previously authorised this client.
    creds = flow.run_local_server(
        host="localhost",
        port=port,
        access_type="offline",
        prompt="consent",
        open_browser=False,  # we already opened it above
    )

    if not creds.refresh_token:
        print("ERROR: No refresh_token returned. Try revoking previous "
              "consent at https://myaccount.google.com/permissions and "
              "re-run.", file=sys.stderr)
        return 1

    # Read the client_id/secret out of the JSON for convenience.
    with open(args.client_secrets, "r", encoding="utf-8") as f:
        client = json.load(f)
    block = client.get("installed") or client.get("web") or {}
    client_id = block.get("client_id", "")
    client_secret = block.get("client_secret", "")

    print()
    print("=" * 70)
    print("SUCCESS. Add these THREE secrets to your GitHub repo:")
    print("  Settings -> Secrets and variables -> Actions -> New repository secret")
    print("=" * 70)
    print(f"DRIVE_OAUTH_CLIENT_ID")
    print(f"  {client_id}")
    print()
    print(f"DRIVE_OAUTH_CLIENT_SECRET")
    print(f"  {client_secret}")
    print()
    print(f"DRIVE_OAUTH_REFRESH_TOKEN")
    print(f"  {creds.refresh_token}")
    print("=" * 70)
    print()
    print("Authenticated account:", getattr(creds, "id_token", None) or "(see Drive listing on next run)")
    print("Scopes granted       :", " ".join(creds.scopes or []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
