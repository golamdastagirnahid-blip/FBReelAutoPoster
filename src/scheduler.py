"""Humanized daily scheduling.

For each local-time date, deterministically generate N random posting slots
within the posting window (seeded by the date string + page id, so reruns
produce identical schedules and no double-posts happen if state is lost).

Each workflow run calls `due_slot()` which returns the index of the slot
whose target time has passed (within tolerance) and is still pending.
"""
from __future__ import annotations
import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import state


def _seed(date_str: str, salt: str) -> int:
    h = hashlib.sha256(f"{date_str}|{salt}".encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def _generate_slots(
    date_str: str,
    salt: str,
    count: int,
    window_start_h: int,
    window_end_h: int,
    min_gap_min: int = 60,
) -> list[str]:
    """Return list of 'HH:MM' strings spaced by at least min_gap_min."""
    rng = random.Random(_seed(date_str, salt))
    start = window_start_h * 60
    end = window_end_h * 60
    total = end - start
    if total <= 0 or count <= 0:
        return []
    # Ensure feasibility of min_gap; otherwise shrink gap
    while count * min_gap_min > total and min_gap_min > 10:
        min_gap_min -= 10

    picked: list[int] = []
    attempts = 0
    while len(picked) < count and attempts < 2000:
        attempts += 1
        m = rng.randint(start, end - 1)
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
        rng = random.Random(_seed(date_str, salt + ":count"))
        count = rng.randint(posts_min, posts_max)
        slots = _generate_slots(
            date_str, salt, count, window_start_h, window_end_h
        )
        sched = {"slots": slots, "done": [], "tz": tz_name}
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
