"""Humanized daily scheduling.

For each local-time date, generate N random posting slots within the
posting window. The schedule is reproducible within the day (so multiple
cron ticks see identical slots) but **unpredictable to outside observers**:

  - On first creation we generate a 128-bit cryptographic nonce and store
    it inside the schedule file alongside the slots. The slots are seeded
    by sha256(date | page_id | nonce) \u2014 so even someone who knows the
    formula and your page id cannot predict tomorrow's posting times
    without reading your private state file.
  - Slots are drawn from human-active engagement bands (morning, lunch,
    afternoon, prime evening) instead of uniformly across 24h, so we
    never post at 4am which both screams 'bot' to FB and reaches no
    audience.
  - A minimum gap (default 3 hours, auto-shrunk if window is tight) is
    enforced so we never burst.
"""
from __future__ import annotations
import hashlib
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import state


# Human-active engagement bands (local time, minutes from midnight) with
# weights. Higher weight = more likely a slot is drawn from that band.
# Tuned for general FB Reels consumption (mobile-heavy evenings).
_BANDS: tuple[tuple[int, int, float], ...] = (
    (8 * 60, 11 * 60, 1.0),    # late morning      (commute, breakfast scroll)
    (12 * 60, 14 * 60, 1.5),   # lunch break       (high mobile usage)
    (15 * 60, 17 * 60, 0.8),   # afternoon dip
    (18 * 60, 23 * 60, 2.5),   # prime evening     (peak Reels time)
)


def _seed(date_str: str, salt: str, nonce: str) -> int:
    h = hashlib.sha256(f"{date_str}|{salt}|{nonce}".encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def _intersect_band(
    start: int, end: int, b_start: int, b_end: int
) -> tuple[int, int] | None:
    lo, hi = max(start, b_start), min(end, b_end)
    return (lo, hi) if hi > lo else None


def _weighted_pick(
    rng: random.Random, bands: list[tuple[int, int, float]]
) -> int:
    """Pick a minute within bands proportional to (band_length * weight)."""
    weights = [(hi - lo) * w for lo, hi, w in bands]
    total = sum(weights)
    r = rng.uniform(0, total)
    acc = 0.0
    for (lo, hi, _w), weight in zip(bands, weights):
        acc += weight
        if r <= acc:
            return rng.randint(lo, hi - 1)
    lo, hi, _ = bands[-1]
    return rng.randint(lo, hi - 1)


def _generate_slots(
    date_str: str,
    salt: str,
    nonce: str,
    count: int,
    window_start_h: int,
    window_end_h: int,
    min_gap_min: int = 180,
) -> list[str]:
    """Return list of 'HH:MM' strings spaced by at least min_gap_min,
    drawn from human-active engagement bands clipped to the window."""
    rng = random.Random(_seed(date_str, salt, nonce))
    win_start = window_start_h * 60
    win_end = window_end_h * 60
    if win_end - win_start <= 0 or count <= 0:
        return []

    # Clip bands to the configured posting window.
    bands: list[tuple[int, int, float]] = []
    for b_start, b_end, w in _BANDS:
        clipped = _intersect_band(win_start, win_end, b_start, b_end)
        if clipped:
            bands.append((clipped[0], clipped[1], w))
    # Fallback: if user configured a window with no overlap with the bands
    # (e.g. only nights), use the raw window uniformly.
    if not bands:
        bands = [(win_start, win_end, 1.0)]

    # Auto-shrink min_gap if too tight to fit ``count`` slots.
    span = max(b[1] for b in bands) - min(b[0] for b in bands)
    while count * min_gap_min > span and min_gap_min > 30:
        min_gap_min -= 15

    picked: list[int] = []
    attempts = 0
    while len(picked) < count and attempts < 4000:
        attempts += 1
        m = _weighted_pick(rng, bands)
        if all(abs(m - p) >= min_gap_min for p in picked):
            picked.append(m)
    picked.sort()
    return [f"{m // 60:02d}:{m % 60:02d}" for m in picked]


@dataclass
class DueSlot:
    index: int
    slot_time: str        # "HH:MM" local
    schedule: dict        # the loaded/created schedule dict
    date_str: str         # YYYY-MM-DD local
    tz: ZoneInfo


def ensure_today_schedule(
    tz_name: str,
    salt: str,
    posts_min: int,
    posts_max: int,
    window_start_h: int,
    window_end_h: int,
) -> tuple[str, dict, ZoneInfo]:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    date_str = now_local.strftime("%Y-%m-%d")
    sched = state.load_schedule(date_str)
    if sched is None:
        # 128-bit cryptographic nonce, persisted with the schedule. This is
        # what makes today's slots unguessable from outside.
        nonce = secrets.token_hex(16)
        rng = random.Random(_seed(date_str, salt + ":count", nonce))
        count = rng.randint(posts_min, posts_max)
        slots = _generate_slots(
            date_str, salt, nonce, count, window_start_h, window_end_h
        )
        sched = {"slots": slots, "done": [], "tz": tz_name, "nonce": nonce}
        state.save_schedule(date_str, sched)
    return date_str, sched, tz


def due_slot(
    sched: dict,
    date_str: str,
    tz: ZoneInfo,
    tolerance_min: int,
) -> DueSlot | None:
    """Return earliest pending slot whose time <= now_local (within tolerance).

    A slot fires if ``now`` is within ``tolerance_min`` minutes BEFORE the
    slot OR up to 12 hours AFTER it (catch-up window for missed cron ticks).
    """
    now_local = datetime.now(tz)
    print(f"[scheduler] now_local={now_local.strftime('%Y-%m-%d %H:%M:%S %Z')} "
          f"tolerance={tolerance_min}min catch_up_window=12h")
    done = set(sched.get("done", []))
    for idx, hhmm in enumerate(sched.get("slots", [])):
        if hhmm in done:
            print(f"[scheduler]   slot {hhmm} -> SKIP (already done)")
            continue
        h, m = (int(x) for x in hhmm.split(":"))
        slot_dt = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        earliest = slot_dt - timedelta(minutes=tolerance_min)
        latest = slot_dt + timedelta(hours=12)
        in_window = earliest <= now_local <= latest
        delta_min = (now_local - slot_dt).total_seconds() / 60
        print(f"[scheduler]   slot {hhmm} window=[{earliest.strftime('%H:%M')}..{latest.strftime('%H:%M')}] "
              f"delta_from_slot={delta_min:+.1f}min in_window={in_window}")
        if in_window:
            return DueSlot(
                index=idx, slot_time=hhmm, schedule=sched,
                date_str=date_str, tz=tz,
            )
    return None


def mark_slot_done(date_str: str, sched: dict, slot_time: str) -> None:
    done = list(sched.get("done", []))
    if slot_time not in done:
        done.append(slot_time)
    sched["done"] = done
    state.save_schedule(date_str, sched)
