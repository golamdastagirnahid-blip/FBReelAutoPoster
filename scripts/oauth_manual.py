"""Manual 2-step OAuth flow — no local server needed.

Step 1: Generate the auth URL.
    python scripts/oauth_manual.py url --client-secrets PATH

    Open the printed URL in your browser. Log in. Allow.
    Your browser will then try to load
    http://localhost:9876/?code=XXXXX&...
    and show "this site can't be reached" — that's fine!
    COPY THE URL FROM THE ADDRESS BAR.

Step 2: Exchange the code for a refresh token.
    python scripts/oauth_manual.py exchange --client-secrets PATH --redirect "PASTED_URL_FROM_BROWSER"

    The script extracts the ``code=`` parameter, exchanges it with
    Google, and prints the three GitHub secret values.
"""
from __future__ import annotations
import argparse
import json
import sys
import urllib.parse
import urllib.request

REDIRECT_URI = "http://localhost:9876/"
SCOPE = "https://www.googleapis.com/auth/drive"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/auth"


def _load_client(path: str) -> tuple[str, str]:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    block = d.get("installed") or d.get("web") or {}
    return block["client_id"], block["client_secret"]


def cmd_url(args: argparse.Namespace) -> int:
    client_id, _ = _load_client(args.client_secrets)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print()
    print("=" * 72)
    print("STEP 1: Open this URL in your browser:")
    print("=" * 72)
    print()
    print(url)
    print()
    print("=" * 72)
    print("Log in, click Advanced -> Go to ... (unsafe), then Allow.")
    print()
    print("Your browser will end up at a 'this site can't be reached' page")
    print("with a URL like:")
    print("   http://localhost:9876/?code=4/0AX...&scope=...")
    print()
    print("COPY THAT FULL URL from the address bar, then run:")
    print()
    print('  python scripts/oauth_manual.py exchange \\')
    print(f'    --client-secrets "{args.client_secrets}" \\')
    print('    --redirect "PASTE_THE_URL_HERE"')
    print("=" * 72)
    return 0


def cmd_exchange(args: argparse.Namespace) -> int:
    client_id, client_secret = _load_client(args.client_secrets)
    # Extract code from the pasted URL
    parsed = urllib.parse.urlparse(args.redirect)
    qs = urllib.parse.parse_qs(parsed.query)
    code = (qs.get("code") or [None])[0]
    if not code:
        # Maybe user just pasted the code alone
        if args.redirect and "/" not in args.redirect and "?" not in args.redirect:
            code = args.redirect.strip()
    if not code:
        print("ERROR: No 'code' parameter found in --redirect", file=sys.stderr)
        return 1

    data = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tok = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"ERROR: token exchange failed: {e.code} {e.reason}", file=sys.stderr)
        print(e.read().decode("utf-8", "replace"), file=sys.stderr)
        return 1

    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        print("ERROR: No refresh_token in response. Revoke prior consent at "
              "https://myaccount.google.com/permissions and re-run STEP 1.",
              file=sys.stderr)
        print("Response was:", tok, file=sys.stderr)
        return 1

    print()
    print("=" * 72)
    print("SUCCESS. Add these THREE GitHub secrets:")
    print("=" * 72)
    print()
    print("DRIVE_OAUTH_CLIENT_ID")
    print(f"  {client_id}")
    print()
    print("DRIVE_OAUTH_CLIENT_SECRET")
    print(f"  {client_secret}")
    print()
    print("DRIVE_OAUTH_REFRESH_TOKEN")
    print(f"  {refresh_token}")
    print("=" * 72)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("url", help="Print the Google auth URL")
    u.add_argument("--client-secrets", required=True)
    u.set_defaults(func=cmd_url)

    e = sub.add_parser("exchange", help="Exchange the pasted redirect URL for tokens")
    e.add_argument("--client-secrets", required=True)
    e.add_argument("--redirect", required=True,
                   help="The full URL from your browser's address bar (or just the code value)")
    e.set_defaults(func=cmd_exchange)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
