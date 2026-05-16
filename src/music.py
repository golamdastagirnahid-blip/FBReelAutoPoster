"""Jamendo music client — picks a commercial-safe (CC0 / CC-BY) royalty-free
track matching configured mood tags and downloads it as a local MP3.

Why Jamendo:
  - 600k+ tracks, proper JSON REST API, free dev tier.
  - License URL per track lets us filter to commercial-allowed only.

We *only* return tracks under CC0 or CC-BY-3.0/4.0 (NOT -NC, NOT -ND).
The caller is expected to append a one-line attribution to the post caption
to satisfy the BY clause:

    Music: "<track_name>" by <artist_name> via Jamendo (CC-BY)

Env:
  JAMENDO_CLIENT_ID   required
  MUSIC_TAGS          optional, comma-separated mood/genre tags
                      default: "ambient,chill,cinematic,instrumental,calm"
  MUSIC_MIN_DURATION  optional, seconds (default 30)
  MUSIC_MAX_DURATION  optional, seconds (default 240)
"""
from __future__ import annotations
import os
import random
import urllib.request
from dataclasses import dataclass

import requests


JAMENDO_API = "https://api.jamendo.com/v3.0/tracks"

# License URLs we accept (commercial use OK):
# - creativecommons.org/publicdomain/zero/1.0/   (CC0)
# - creativecommons.org/licenses/by/3.0/         (CC-BY 3.0)
# - creativecommons.org/licenses/by/4.0/         (CC-BY 4.0)
# - creativecommons.org/licenses/by-sa/...       (CC-BY-SA, attribution + share-alike, also commercial OK)
_COMMERCIAL_OK_FRAGMENTS = (
    "publicdomain/zero",
    "/by/",
    "/by-sa/",
)


@dataclass
class Track:
    id: str
    name: str
    artist: str
    license_url: str
    audio_url: str
    duration: int  # seconds

    @property
    def license_short(self) -> str:
        u = self.license_url.lower()
        if "publicdomain/zero" in u:
            return "CC0"
        if "/by-sa/" in u:
            return "CC-BY-SA"
        return "CC-BY"

    def attribution(self) -> str:
        return f'Music: "{self.name}" by {self.artist} via Jamendo ({self.license_short})'


def _is_commercial_ok(license_url: str | None) -> bool:
    if not license_url:
        return False
    u = license_url.lower()
    if "/by-nc" in u or "/by-nd" in u or "/nc-" in u or "/nd-" in u:
        return False
    return any(frag in u for frag in _COMMERCIAL_OK_FRAGMENTS)


def search_tracks(
    client_id: str,
    tags: str,
    *,
    limit: int = 50,
    min_duration: int = 30,
    max_duration: int = 240,
) -> list[Track]:
    """Search Jamendo for commercial-OK tracks matching ``tags``.

    ``tags`` is a comma-separated string like "ambient,chill,cinematic".
    """
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": str(limit),
        "fuzzytags": tags,
        "audioformat": "mp32",
        "include": "licenses musicinfo",
        "order": "popularity_total_desc",
        "durationbetween": f"{min_duration}_{max_duration}",
    }
    r = requests.get(JAMENDO_API, params=params, timeout=20)
    r.raise_for_status()
    payload = r.json()
    results = payload.get("results", [])
    out: list[Track] = []
    for t in results:
        license_url = t.get("license_ccurl") or t.get("license_url") or ""
        if not _is_commercial_ok(license_url):
            continue
        audio_url = t.get("audio") or t.get("audiodownload")
        if not audio_url:
            continue
        out.append(Track(
            id=str(t.get("id", "")),
            name=t.get("name", "Untitled"),
            artist=t.get("artist_name", "Unknown"),
            license_url=license_url,
            audio_url=audio_url,
            duration=int(t.get("duration", 0)),
        ))
    return out


def pick_track(client_id: str, tags: str, *, seed: str | None = None) -> Track | None:
    """Search and return one commercial-OK track. ``seed`` (e.g. video
    filename) makes the pick deterministic per video for reruns."""
    tracks = search_tracks(client_id, tags)
    if not tracks:
        return None
    rng = random.Random(seed) if seed else random.Random()
    return rng.choice(tracks)


def download_track(track: Track, dst_path: str) -> str:
    """Download the track's MP3 to ``dst_path``. Returns the path."""
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
    with requests.get(track.audio_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
    return dst_path


def fetch_music_for_video(
    client_id: str,
    tags: str,
    dst_path: str,
    *,
    seed: str | None = None,
) -> Track | None:
    """High-level: pick a track + download it. Returns the Track on success
    (so caller can read attribution), or None if no commercial-OK track
    matched. Network errors are caught and logged; we never let music
    fetching fail the whole post."""
    try:
        track = pick_track(client_id, tags, seed=seed)
    except Exception as e:  # noqa: BLE001
        print(f"[music] search failed: {e}")
        return None
    if track is None:
        print(f"[music] no commercial-OK tracks found for tags={tags!r}")
        return None
    try:
        download_track(track, dst_path)
    except Exception as e:  # noqa: BLE001
        print(f"[music] download failed for track={track.id}: {e}")
        return None
    print(f"[music] picked id={track.id} '{track.name}' by {track.artist} "
          f"license={track.license_short} duration={track.duration}s")
    return track
