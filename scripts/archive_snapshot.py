#!/usr/bin/env python3
"""Archive one end-of-day market snapshot per VİOP trading day.

Writes `history/<YYYY-MM-DD>.json` (write-once) and `history/index.json` into a
checkout of the `data-history` branch. Needs only `holidays`, not borsapy.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

if __package__:
    from .market_calendar import ISTANBUL, half_days, public_holidays
else:  # run as `python scripts/archive_snapshot.py`
    from market_calendar import ISTANBUL, half_days, public_holidays


# Borsa İstanbul VİOP trading hours (Istanbul time). The opening session starts
# at 09:20; the evening session, which includes FX and precious-metal futures,
# ends at 23:00. Half days have no evening session and close at 12:40.
SESSION_OPEN = time(9, 20)
SESSION_CLOSE = time(23, 0)
HALF_DAY_CLOSE = time(12, 40)
# VİOP quotes may be delayed by about 15 minutes, so a snapshot only counts as
# end-of-day once the close has had time to reach the data feed.
QUOTE_DELAY = timedelta(minutes=15)

MANIFEST_SCHEMA_VERSION = 1
HISTORY_DIR = "history"
MANIFEST_NAME = "index.json"
DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def is_trading_day(day: date) -> bool:
    """Return whether VİOP holds a session on this day (half days included)."""
    return day.weekday() < 5 and day not in public_holidays(day.year)


def previous_trading_day(day: date) -> date:
    candidate = day - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def session_close(day: date) -> time:
    return HALF_DAY_CLOSE if day in half_days(day.year) else SESSION_CLOSE


def parse_generated_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"generated_at must be an ISO timestamp, got {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ISTANBUL)
    return parsed.astimezone(ISTANBUL)


def trading_date(generated_at: datetime) -> date | None:
    """Return the closed trading session a snapshot represents.

    A snapshot taken after day D's close (plus the quote delay) counts as D, as
    does one taken before the next session opens, including over weekends and
    holidays. A snapshot taken while a session is running returns None.
    """
    local = generated_at.astimezone(ISTANBUL)
    day = local.date()
    if is_trading_day(day):
        opened = datetime.combine(day, SESSION_OPEN, ISTANBUL)
        settled = datetime.combine(day, session_close(day), ISTANBUL) + QUOTE_DELAY
        if local >= settled:
            return day
        if local >= opened:
            return None
    return previous_trading_day(day)


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def manifest_entry(day: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    markets = snapshot.get("market_order") or list(snapshot["markets"])
    return {"date": day, "generated_at": snapshot["generated_at"], "markets": markets}


def build_manifest(history: Path) -> dict[str, Any]:
    """Index every archived day, sorted by date, one entry per file."""
    entries = []
    for path in sorted(history.iterdir()):
        match = DAY_FILE.match(path.name)
        if not match:
            continue
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        entries.append(manifest_entry(match.group(1), snapshot))
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "days": entries}


def update_manifest(history: Path) -> bool:
    """Rewrite history/index.json if it is out of date; return whether it changed."""
    manifest = build_manifest(history)
    content = (
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    path = history / MANIFEST_NAME
    if path.exists() and path.read_bytes() == content:
        return False
    write_atomic(path, content)
    return True


def archive(snapshot_path: Path, checkout: Path) -> str:
    """Archive the snapshot if it is end-of-day; return a log line."""
    raw = snapshot_path.read_bytes()
    snapshot = json.loads(raw)
    if not isinstance(snapshot.get("markets"), dict) or not snapshot["markets"]:
        raise ValueError(f"{snapshot_path} has no markets")
    generated_at = parse_generated_at(snapshot.get("generated_at"))

    history = checkout / HISTORY_DIR
    history.mkdir(parents=True, exist_ok=True)
    day = trading_date(generated_at)
    if day is None:
        message = (
            f"Skipped: snapshot at {generated_at.isoformat()} was taken during "
            "a VİOP session, not after its close"
        )
    else:
        target = history / f"{day.isoformat()}.json"
        if target.exists():
            message = f"Skipped: {target.name} is already archived; never overwritten"
        else:
            write_atomic(target, raw)
            message = (
                f"Archived end-of-day snapshot {day.isoformat()} "
                f"(generated at {generated_at.isoformat()}) to {target}"
            )
    if update_manifest(history):
        message += f"; updated {HISTORY_DIR}/{MANIFEST_NAME}"
    return message


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="Snapshot from update_data.py")
    parser.add_argument("checkout", type=Path, help="Checkout of the data-history branch")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(archive(args.snapshot, args.checkout))


if __name__ == "__main__":
    main()
