"""One-time helper: exchange a short-lived User token for a NEVER-expiring
Page token, given a short-lived USER access token from Graph API Explorer.

Usage:
    python scripts/get_long_lived_token.py \
        --app-id <APP_ID> --app-secret <APP_SECRET> \
        --short-user-token <SHORT_USER_TOKEN> --page-id <PAGE_ID>

Steps it performs:
  1) short-user-token -> long-lived USER token (60 days)
  2) long-lived USER token -> PAGE token list -> picks your page
     The page token returned from a long-lived user token does NOT expire
     as long as you use it at least once every 60 days.
"""
from __future__ import annotations
import argparse
import sys
import requests

GRAPH = "https://graph.facebook.com/v21.0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", required=True)
    ap.add_argument("--app-secret", required=True)
    ap.add_argument("--short-user-token", required=True)
    ap.add_argument("--page-id", required=True)
    args = ap.parse_args()

    r = requests.get(f"{GRAPH}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": args.app_id,
        "client_secret": args.app_secret,
        "fb_exchange_token": args.short_user_token,
    }, timeout=60)
    r.raise_for_status()
    long_user = r.json()["access_token"]
    print(f"[ok] long-lived user token acquired")

    r = requests.get(f"{GRAPH}/me/accounts", params={
        "access_token": long_user,
        "fields": "id,name,access_token",
    }, timeout=60)
    r.raise_for_status()
    pages = r.json().get("data", [])
    match = next((p for p in pages if p["id"] == args.page_id), None)
    if not match:
        print(f"[err] page {args.page_id} not in your /me/accounts. Found: "
              f"{[p['id'] + ':' + p['name'] for p in pages]}", file=sys.stderr)
        return 2
    print(f"[ok] page: {match['name']} ({match['id']})")
    print("\n=== PAGE ACCESS TOKEN (save as FB_PAGE_TOKEN secret) ===")
    print(match["access_token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
