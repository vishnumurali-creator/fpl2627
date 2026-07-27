#!/usr/bin/env python3
"""
Daily FPL snapshot.

Stores raw API responses (never transformed) plus a flat per-player CSV
time series of prices, ownership and transfer flow.

Run once a day. Safe to run more often; each raw dump is timestamped and
CSV rows are deduplicated on (snapshot_date, player_id).

Usage:  python snapshot.py [--data-dir ./data]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE = "https://fantasy.premierleague.com/api"

ENDPOINTS = {
    "bootstrap": f"{BASE}/bootstrap-static/",
    "fixtures": f"{BASE}/fixtures/",
}

# FPL rejects some default clients; a plain browser UA is reliable.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Fields lifted from each element (player) into the CSV time series.
# Keep this list append-only so historical columns stay stable.
PLAYER_FIELDS = [
    "id",
    "web_name",
    "team",
    "element_type",
    "now_cost",
    "cost_change_event",
    "cost_change_start",
    "selected_by_percent",
    "transfers_in_event",
    "transfers_out_event",
    "transfers_in",
    "transfers_out",
    "status",
    "chance_of_playing_this_round",
    "chance_of_playing_next_round",
    "form",
    "total_points",
    "event_points",
    "minutes",
    "starts",
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "defensive_contribution",
    "bps",
    "bonus",
    "news",
]

CSV_COLUMNS = ["snapshot_ts", "snapshot_date"] + PLAYER_FIELDS


def fetch(url: str, *, retries: int = 3, backoff: float = 3.0) -> dict | list:
    """GET with a small retry. Raises on final failure — a silent miss is
    worse than a loud one, because the day's data is unrecoverable."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = httpx.get(url, headers=HEADERS, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001 - want everything retried
            last = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last}")


def write_raw(payload, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))


def existing_dates(csv_path: Path) -> set[str]:
    """Dates already present, so re-running the same day is a no-op."""
    if not csv_path.exists():
        return set()
    with csv_path.open(newline="", encoding="utf-8") as fh:
        return {row["snapshot_date"] for row in csv.DictReader(fh)}


def append_players(bootstrap: dict, csv_path: Path, ts: datetime) -> int:
    date_str = ts.date().isoformat()
    if date_str in existing_dates(csv_path):
        print(f"  players.csv already has {date_str}, skipping append")
        return 0

    elements = bootstrap.get("elements")
    if not elements:
        raise RuntimeError("bootstrap payload had no 'elements' — schema change?")

    is_new = not csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        for el in elements:
            row = {f: el.get(f) for f in PLAYER_FIELDS}
            row["snapshot_ts"] = ts.isoformat()
            row["snapshot_date"] = date_str
            writer.writerow(row)
    return len(elements)


def check_schema(bootstrap: dict) -> None:
    """Warn loudly if expected fields vanish. FPL adjusts the payload
    between seasons and occasionally mid-season."""
    sample = (bootstrap.get("elements") or [{}])[0]
    missing = [f for f in PLAYER_FIELDS if f not in sample]
    if missing:
        print(f"  WARNING: fields absent from API response: {missing}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="./data", type=Path)
    args = ap.parse_args()

    ts = datetime.now(timezone.utc)
    stamp = ts.strftime("%Y%m%d_%H%M%S")
    raw_dir = args.data_dir / "raw" / ts.date().isoformat()

    print(f"FPL snapshot {ts.isoformat()}")

    payloads = {}
    for name, url in ENDPOINTS.items():
        print(f"  fetching {name}...")
        payloads[name] = fetch(url)
        write_raw(payloads[name], raw_dir / f"{stamp}_{name}.json.gz")

    check_schema(payloads["bootstrap"])

    n = append_players(
        payloads["bootstrap"], args.data_dir / "derived" / "players.csv", ts
    )

    events = payloads["bootstrap"].get("events", [])
    nxt = next((e for e in events if e.get("is_next")), None)
    if nxt:
        print(f"  next: {nxt['name']} — deadline {nxt['deadline_time']}")

    print(f"  raw -> {raw_dir}")
    print(f"  appended {n} player rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
