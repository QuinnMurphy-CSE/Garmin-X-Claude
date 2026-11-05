#!/usr/bin/env python3
"""Export your Garmin Connect data to CSV using Garmin's official Personal API.

Reads GARMIN_PERSONAL_API_KEY from .env, exchanges it for a bearer token
(cached by Garmin's client in ~/.config/garmin/), and writes:

    output/garmin_daily_<period>_<dates>.csv      one row per day (steps, resting HR,
                                                calories, stress, sleep, HRV, body
                                                battery, SpO2, weight, ...)
    output/garmin_activities_<period>_<dates>.csv one row per activity
    output/raw/*.json                           untouched API responses

Usage:
    python garmin_export.py day      # all of yesterday + today so far
    python garmin_export.py week     # the last 7 full days + today so far
    python garmin_export.py month    # the last 30 full days + today so far

Today's row is marked day_complete=False and holds data up to the moment you
ran the script (see the as_of column).
    python garmin_export.py --start 2026-01-01 --end 2026-03-31   # custom range
"""

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from garmin_personal_api import (
    GarminPersonalApiClient,
    GarminPersonalApiError,
    PersonalApiAuthenticator,
)

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "output"
RAW_DIR = OUT_DIR / "raw"
CHUNK_DAYS = 28  # Garmin doesn't publish max range per request; stay conservative.
PERIODS = {"day": 1, "week": 7, "month": 30}
DATE_KEYS = ("calendarDate", "summaryDate", "date", "measurementDate", "startDate")


def load_env(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments, optional quotes)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def flatten(obj, prefix="", depth=0, max_depth=3) -> dict:
    """Flatten nested dicts into prefix.key columns; lists are skipped."""
    out = {}
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            if depth < max_depth:
                out.update(flatten(v, f"{key}.", depth + 1, max_depth))
        elif not isinstance(v, list):
            out[key] = v
    return out


def records(resp):
    """Pull the list of per-day records out of a range response, whatever its wrapper."""
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        for v in resp.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return [resp]
    return []


def day_of(rec: dict):
    for k in DATE_KEYS:
        v = rec.get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    for v in rec.values():  # e.g. sleep wraps fields in dailySleepDTO
        if isinstance(v, dict):
            d = day_of(v)
            if d:
                return d
    return None


def chunks(start: date, end: date, size: int = CHUNK_DAYS):
    s = start
    while s <= end:
        e = min(s + timedelta(days=size - 1), end)
        yield s, e
        s = e + timedelta(days=1)


def save_raw(name: str, data) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))


def call(label, fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except GarminPersonalApiError as e:
        print(f"  ! {label}: {e}", file=sys.stderr)
        return None


def merge(days: dict, prefix: str, resp) -> None:
    for rec in records(resp):
        d = day_of(rec)
        if d in days:
            days[d].update(flatten(rec, prefix))


def write_csv(path: Path, rows: list[dict]) -> None:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("period", nargs="?", choices=PERIODS, default="week",
                   help="day = yesterday, week = last 7 days, month = last 30 days; each plus today so far (default week)")
    p.add_argument("--start", help="YYYY-MM-DD (overrides period)")
    p.add_argument("--end", help="YYYY-MM-DD (default today)")
    args = p.parse_args()

    load_env(HERE / ".env")
    api_key = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    if not api_key:
        sys.exit("Set GARMIN_PERSONAL_API_KEY in .env (see .env.example).")

    now = datetime.now()
    today = now.date()
    if args.start or args.end:
        end = date.fromisoformat(args.end) if args.end else today
        start = date.fromisoformat(args.start) if args.start else end - timedelta(days=6)
        label = f"{start}_{end}"
    else:
        end = today
        start = today - timedelta(days=PERIODS[args.period])
        label = f"{args.period}_{start}_to_{now:%Y-%m-%d_%H%M}"

    with PersonalApiAuthenticator() as auth:
        token = auth.obtain_token(api_key)

    all_days = []
    d = start
    while d <= end:
        all_days.append(d.isoformat())
        d += timedelta(days=1)
    as_of = now.strftime("%Y-%m-%d %H:%M")
    days = {x: {"date": x, "day_complete": x < today.isoformat(), "as_of": as_of} for x in all_days}

    with GarminPersonalApiClient(token.access_token) as c:
        print(f"Pulling {start} → {end} (as of {as_of})")

        # Range endpoints, fetched in chunks.
        range_sources = [
            ("", "daily_summaries", c.get_daily_summaries),        # steps, resting HR, calories, stress, intensity mins...
            ("sleep.", "sleep_summaries", c.get_daily_sleep_summaries),
            ("hrv.", "hrv_summaries", c.get_daily_hrv_summaries),
            ("bodybattery.", "body_battery", c.get_daily_body_battery),
            ("stress.", "stress", c.get_daily_stress),
            ("hydration.", "hydration", c.get_daily_hydration),
            ("weight.", "weight", c.get_weight_measurements),
        ]
        for prefix, name, fn in range_sources:
            for s, e in chunks(start, end):
                print(f"  {name} {s}..{e}")
                resp = call(name, fn, s, e)
                if resp is not None:
                    save_raw(f"{name}_{s}_{e}", resp)
                    merge(days, prefix, resp)

        # Per-day endpoints (no range version).
        per_day_sources = [
            ("heartrate.", "heart_rate", c.get_daily_heart_rate),  # resting / min / max HR
            ("spo2.", "spo2", c.get_daily_spo2),
        ]
        for x in all_days:
            for prefix, name, fn in per_day_sources:
                print(f"  {name} {x}")
                resp = call(name, fn, x)
                if resp is not None:
                    save_raw(f"{name}_{x}", resp)
                    days[x].update(flatten(resp, prefix))

        # Activities, paginated.
        activities, offset, page = [], 0, 100
        while True:
            print(f"  activities offset {offset}")
            resp = call("activities", c.search_activities,
                        start=offset, limit=page, start_date=start, end_date=end)
            batch = records(resp) if resp is not None else []
            if resp is not None:
                save_raw(f"activities_{offset}", resp)
            activities.extend(flatten(a) for a in batch)
            if len(batch) < page:
                break
            offset += page

    OUT_DIR.mkdir(exist_ok=True)
    daily_path = OUT_DIR / f"garmin_daily_{label}.csv"
    act_path = OUT_DIR / f"garmin_activities_{label}.csv"
    write_csv(daily_path, [days[x] for x in all_days])
    write_csv(act_path, activities)
    print(f"\nWrote {len(all_days)} days        → {daily_path}")
    print(f"Wrote {len(activities)} activities → {act_path}")
    print(f"Raw API responses          → {RAW_DIR}")


if __name__ == "__main__":
    main()
