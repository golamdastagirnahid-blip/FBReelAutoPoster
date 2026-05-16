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
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .music_match import MusicProfile

import requests


JAMENDO_API = "https://api.jamendo.com/v3.0/tracks"

# Network resiliency: Jamendo occasionally returns 502/503; retry with backoff.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
REQUEST_TIMEOUT_SEARCH = 20
REQUEST_TIMEOUT_DOWNLOAD = 180
MAX_HTTP_ATTEMPTS = 4

# Minimum acceptable MP3 size (catches truncated downloads / CDN errors).
MIN_MP3_BYTES = 64 * 1024  # 64 KB

# In-process cache of search results: avoid repeating the same search
# multiple times in a single workflow run (rare but possible if main.py
# is extended later).
_SEARCH_CACHE: dict[str, list["Track"]] = {}

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
    # Extra Jamendo metadata used by the matcher (filled when present).
    vartags: list[str] = field(default_factory=list)
    musicinfo_tags: list[str] = field(default_factory=list)
    bpm: float | None = None
    speed: str = ""
    vocal_instrumental: str = ""  # "instrumental" | "vocal" | ""

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

    def to_dict(self) -> dict[str, Any]:
        """Serializable form for the audit trail in state/posted.json."""
        return {
            "id": self.id,
            "name": self.name,
            "artist": self.artist,
            "license": self.license_short,
            "license_url": self.license_url,
            "audio_url": self.audio_url,
            "duration": self.duration,
            "bpm": self.bpm,
            "speed": self.speed,
            "vocal_instrumental": self.vocal_instrumental,
            "vartags": self.vartags,
            "musicinfo_tags": self.musicinfo_tags,
            "attribution": self.attribution(),
        }


def _is_commercial_ok(license_url: str | None) -> bool:
    if not license_url:
        return False
    u = license_url.lower()
    if "/by-nc" in u or "/by-nd" in u or "/nc-" in u or "/nd-" in u:
        return False
    return any(frag in u for frag in _COMMERCIAL_OK_FRAGMENTS)


def _http_get_with_retry(url: str, *, params: dict | None = None,
                        timeout: int, label: str) -> requests.Response:
    """GET with exponential backoff for transient HTTP / network errors."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.ok:
                return r
            if r.status_code not in RETRYABLE_STATUS:
                r.raise_for_status()
            last_exc = RuntimeError(f"{label} HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            last_exc = e
        if attempt == MAX_HTTP_ATTEMPTS:
            break
        sleep = 2.0 * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
        print(f"[music] {label} attempt {attempt}/{MAX_HTTP_ATTEMPTS} failed: "
              f"{last_exc}; retrying in {sleep:.1f}s")
        time.sleep(sleep)
    raise RuntimeError(f"{label} exhausted retries: {last_exc}")


def search_tracks(
    client_id: str,
    tags: str,
    *,
    limit: int = 200,
    min_duration: int = 20,
    max_duration: int = 600,
) -> list[Track]:
    """Search Jamendo for commercial-OK tracks matching ``tags``.

    ``tags`` is a comma-separated string like "ambient,chill,cinematic".
    Results are cached per (tags, limit, duration window) within the process.
    """
    cache_key = f"{tags}|{limit}|{min_duration}|{max_duration}"
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]

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
    # If empty tags supplied, skip the fuzzytags param entirely so we
    # browse the most popular catalog-wide (last-resort fallback path).
    if not tags.strip():
        params.pop("fuzzytags", None)

    r = _http_get_with_retry(
        JAMENDO_API, params=params,
        timeout=REQUEST_TIMEOUT_SEARCH, label="jamendo-search",
    )
    payload = r.json()
    results = payload.get("results", [])
    out: list[Track] = []
    rejected_licenses: dict[str, int] = {}
    for t in results:
        license_url = t.get("license_ccurl") or t.get("license_url") or ""
        if not _is_commercial_ok(license_url):
            # Tally the most common rejected license substrings (helps
            # debug "why no tracks?" issues without hammering the API).
            key = (license_url or "<empty>")[:80]
            rejected_licenses[key] = rejected_licenses.get(key, 0) + 1
            continue
        audio_url = t.get("audio") or t.get("audiodownload")
        if not audio_url:
            continue
        mi = t.get("musicinfo") or {}
        mi_tags = mi.get("tags") or {}
        vartags = list(mi_tags.get("vartags") or [])
        info_tags = list(mi_tags.get("genres") or []) + list(mi_tags.get("instruments") or [])
        try:
            bpm_val = float(mi.get("bpm")) if mi.get("bpm") else None
        except (TypeError, ValueError):
            bpm_val = None
        out.append(Track(
            id=str(t.get("id", "")),
            name=t.get("name", "Untitled"),
            artist=t.get("artist_name", "Unknown"),
            license_url=license_url,
            audio_url=audio_url,
            duration=int(t.get("duration", 0)),
            vartags=vartags,
            musicinfo_tags=info_tags,
            bpm=bpm_val,
            speed=str(mi.get("speed") or ""),
            vocal_instrumental=str(mi.get("vocalinstrumental") or ""),
        ))
    print(
        f"[music] search tags={tags!r}: total={len(results)} "
        f"commercial-OK={len(out)} rejected={sum(rejected_licenses.values())}"
    )
    if not out and rejected_licenses:
        # Print top 3 rejected license URLs to make the cause obvious.
        top = sorted(rejected_licenses.items(), key=lambda kv: -kv[1])[:3]
        print(f"[music]   top rejected licenses: {top}")
    _SEARCH_CACHE[cache_key] = out
    return out


def pick_track(client_id: str, tags: str, *, seed: str | None = None) -> Track | None:
    """Search and return one commercial-OK track. ``seed`` (e.g. video
    filename) makes the pick deterministic per video for reruns."""
    tracks = search_tracks(client_id, tags)
    if not tracks:
        return None
    rng = random.Random(seed) if seed else random.Random()
    return rng.choice(tracks)


def _validate_mp3(path: str) -> tuple[bool, str]:
    """Return (ok, detail). Uses ffprobe to confirm the file is a real audio
    stream with non-zero duration. Catches truncated downloads, HTML error
    pages saved as .mp3, and other CDN edge cases.
    """
    if not os.path.exists(path):
        return False, "file not found"
    size = os.path.getsize(path)
    if size < MIN_MP3_BYTES:
        return False, f"too small ({size} bytes)"
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type,duration",
                "-of", "default=nw=1:nk=1", path,
            ],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        # ffprobe missing is unusual but not fatal — accept based on size.
        return True, f"ffprobe unavailable ({e}); accepted by size"
    if r.returncode != 0:
        return False, f"ffprobe failed: {r.stderr.strip()[:200]}"
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if "audio" not in lines:
        return False, f"no audio stream (ffprobe: {lines})"
    # Look for a parseable duration > 0.
    for ln in lines:
        try:
            if float(ln) > 0:
                return True, f"ok ({size} bytes, dur={float(ln):.1f}s)"
        except ValueError:
            continue
    return False, "no positive duration"


def download_track(track: Track, dst_path: str) -> str:
    """Download the track's MP3 to ``dst_path`` and validate it.

    Raises ``RuntimeError`` on download failure or invalid file.
    Returns the path on success.
    """
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            with requests.get(track.audio_url, stream=True,
                              timeout=REQUEST_TIMEOUT_DOWNLOAD) as r:
                if not r.ok:
                    raise RuntimeError(
                        f"download HTTP {r.status_code}: {r.text[:200]}"
                    )
                with open(dst_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
            ok, detail = _validate_mp3(dst_path)
            if ok:
                return dst_path
            last_exc = RuntimeError(f"invalid mp3: {detail}")
        except requests.RequestException as e:
            last_exc = e
        if attempt == MAX_HTTP_ATTEMPTS:
            break
        sleep = 2.0 * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
        print(f"[music] download attempt {attempt}/{MAX_HTTP_ATTEMPTS} failed: "
              f"{last_exc}; retrying in {sleep:.1f}s")
        time.sleep(sleep)
    raise RuntimeError(f"download exhausted retries: {last_exc}")


class MusicUnavailable(RuntimeError):
    """Raised when no commercial-OK music can be obtained. Caller decides
    whether to fail the post (strict mode) or continue (degraded mode)."""


# ---------------------------------------------------------------------------
# Multi-criteria scoring of candidate tracks against a MusicProfile.
# Higher score = better match.
# ---------------------------------------------------------------------------
def score_track(track: Track, profile: "MusicProfile",
                video_duration: float = 0.0) -> tuple[float, list[str]]:
    """Return ``(score, reasons)`` for how well ``track`` matches ``profile``.

    Scoring weights (tuned for natural sounding picks):
      +6 per matching vartag (Jamendo curated mood)
      +2 per matching genre/instrument tag
      +4 if speed bucket matches
      +3 if BPM falls in the energy-derived target range
      +3 if instrumental (no competing vocals)
      -5 if vocal
      +2 if duration >= video_duration (no audible loop needed)
      -1 if duration < 0.7 * video_duration (heavy looping)
    """
    score = 0.0
    reasons: list[str] = []

    # vartag overlap
    p_var = set(profile.vartags)
    t_var = set(track.vartags)
    var_hits = p_var & t_var
    if var_hits:
        score += 6 * len(var_hits)
        reasons.append(f"vartags+{len(var_hits)}({','.join(sorted(var_hits))})")

    # genre/instrument overlap (loose: case-insensitive substring)
    t_info = {x.lower() for x in track.musicinfo_tags}
    genre_hits = {g for g in profile.genre_tags if g.lower() in t_info}
    if genre_hits:
        score += 2 * len(genre_hits)
        reasons.append(f"genres+{len(genre_hits)}({','.join(sorted(genre_hits))})")

    # speed
    if track.speed and track.speed in profile.speed:
        score += 4
        reasons.append(f"speed={track.speed}")

    # BPM target from energy
    if track.bpm:
        bpm_min, bpm_max = _bpm_window_for_energy(profile.energy)
        if bpm_min <= track.bpm <= bpm_max:
            score += 3
            reasons.append(f"bpm={track.bpm:.0f} in [{bpm_min},{bpm_max}]")

    # instrumental vs vocal
    vi = track.vocal_instrumental.lower()
    if vi == "instrumental":
        score += 3
        reasons.append("instrumental")
    elif vi == "vocal":
        score -= 5
        reasons.append("vocal-penalty")

    # duration fit
    if video_duration > 0 and track.duration > 0:
        if track.duration >= video_duration:
            score += 2
            reasons.append("dur>=video")
        elif track.duration < 0.7 * video_duration:
            score -= 1
            reasons.append("dur<<video")

    return score, reasons


def _bpm_window_for_energy(energy: float) -> tuple[int, int]:
    """Map normalized scene energy (0..1) to a plausible BPM window."""
    if energy < 0.25:
        return 50, 80
    if energy < 0.45:
        return 65, 95
    if energy < 0.65:
        return 85, 115
    if energy < 0.85:
        return 105, 135
    return 125, 175


def fetch_best_music_for_video(
    client_id: str,
    profile: "MusicProfile",
    dst_path: str,
    *,
    seed: str | None = None,
    video_duration: float = 0.0,
    top_n_consider: int = 12,
) -> tuple[Track, float, list[str]]:
    """Profile-aware producer pick:
      1. Search with ``profile.primary_fuzzytags()``.
      2. Score every commercial-OK candidate.
      3. Try fallback tags + no-tag broad if not enough candidates.
      4. Download the highest-scored track that downloads cleanly.
         If the top pick fails to download, fall back to the next-best.

    Returns (track, score, reasons). Raises ``MusicUnavailable`` if no
    candidate downloads cleanly across all attempts.
    """
    import random as _random

    # Build search ladder
    ladder: list[str] = []
    seen: set[str] = set()

    def _add(t: str) -> None:
        k = t.strip().lower()
        if k not in seen:
            seen.add(k)
            ladder.append(t)

    _add(profile.primary_fuzzytags())
    _add(profile.fallback_fuzzytags())
    for g in profile.genre_tags:
        _add(g)
    _add("instrumental,ambient")
    _add("")  # broad popularity

    # Collect candidates across stages until we have a healthy pool.
    candidates: list[Track] = []
    seen_ids: set[str] = set()
    stage_used: str = ""
    for stage in ladder:
        try:
            tracks = search_tracks(client_id, stage)
        except Exception as e:  # noqa: BLE001
            print(f"[music] search stage {stage!r} failed: {e}")
            continue
        for tr in tracks:
            if tr.id and tr.id not in seen_ids:
                seen_ids.add(tr.id)
                candidates.append(tr)
        if len(candidates) >= top_n_consider:
            stage_used = stage or "<no-tags broad>"
            break
        if tracks:
            stage_used = stage or "<no-tags broad>"

    if not candidates:
        raise MusicUnavailable("no commercial-OK candidates across any stage")

    # Score and rank
    scored: list[tuple[float, list[str], Track]] = []
    for tr in candidates:
        score, reasons = score_track(tr, profile, video_duration)
        # Deterministic per-video tiebreak so reruns pick the same track.
        if seed:
            score += 0.0001 * _random.Random(f"{seed}|{tr.id}").random()
        scored.append((score, reasons, tr))
    scored.sort(key=lambda x: x[0], reverse=True)

    print(f"[music] scored {len(scored)} candidates (top stage used: {stage_used!r})")
    for i, (sc, rs, tr) in enumerate(scored[:5]):
        print(f"[music]   #{i+1} score={sc:.2f} id={tr.id} '{tr.name}' by "
              f"{tr.artist} [{tr.license_short}] reasons={rs}")

    last_err: str = ""
    for sc, rs, tr in scored:
        try:
            download_track(tr, dst_path)
        except Exception as e:  # noqa: BLE001
            last_err = f"download failed for top pick id={tr.id}: {e}"
            print(f"[music] {last_err} — trying next candidate")
            continue
        print(f"[music] selected id={tr.id} '{tr.name}' by {tr.artist} "
              f"license={tr.license_short} score={sc:.2f} reasons={rs}")
        return tr, sc, rs
    raise MusicUnavailable(last_err or "all candidates failed to download")


def fetch_music_for_video(
    client_id: str,
    tags: str,
    dst_path: str,
    *,
    seed: str | None = None,
) -> Track:
    """Pick + download a commercial-OK track. Raises ``MusicUnavailable``
    if nothing usable can be obtained after retries + fallback search.

    Fallback chain (each step tried in order until one yields a usable track):
      1. The caller-supplied ``tags`` (e.g. mood/genre combo).
      2. Each individual tag from ``tags`` as a single-tag search.
      3. A broad fallback of ``"instrumental,ambient"``.
      4. No tags at all — popularity browse of the whole CC catalog.
    """
    # Build attempt list with de-dup, preserving order
    attempts: list[str] = []
    seen: set[str] = set()

    def _add(t: str) -> None:
        key = t.strip().lower()
        if key not in seen:
            seen.add(key)
            attempts.append(t)

    _add(tags)
    for single in tags.split(","):
        single = single.strip()
        if single:
            _add(single)
    _add("instrumental,ambient")
    _add("")   # final no-tag broad popular browse

    last_err: str = ""
    for search_tags in attempts:
        label = search_tags or "<no-tags broad>"
        try:
            track = pick_track(client_id, search_tags, seed=seed)
        except Exception as e:  # noqa: BLE001
            last_err = f"search failed for tags={label!r}: {e}"
            print(f"[music] {last_err}")
            continue
        if track is None:
            last_err = f"no commercial-OK tracks for tags={label!r}"
            continue
        try:
            download_track(track, dst_path)
        except Exception as e:  # noqa: BLE001
            last_err = f"download failed for track={track.id}: {e}"
            print(f"[music] {last_err}")
            continue
        print(f"[music] picked id={track.id} '{track.name}' by {track.artist} "
              f"license={track.license_short} duration={track.duration}s "
              f"(matched stage: {label!r})")
        return track
    raise MusicUnavailable(last_err or "unknown failure")
