#!/usr/bin/env python3
"""Archive one end-of-day market snapshot per VİOP trading day.

Writes `history/<YYYY-MM-DD>.json` (write-once) and `history/index.json` into a
checkout of the `data-history` branch. Needs only `holidays`, not borsapy.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__:
    from .market_calendar import ISTANBUL, trading_date
    from .snapshot_schema import validate_snapshot
else:  # run as `python scripts/archive_snapshot.py`
    from market_calendar import ISTANBUL, trading_date
    from snapshot_schema import validate_snapshot


MANIFEST_SCHEMA_VERSION = 1
HISTORY_DIR = "history"
MANIFEST_NAME = "index.json"
DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def parse_generated_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"generated_at must be an ISO timestamp, got {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ISTANBUL)
    return parsed.astimezone(ISTANBUL)


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
    # Archived days are never overwritten, so check before writing anything.
    try:
        validate_snapshot(snapshot)
    except ValueError as exc:
        raise ValueError(f"{snapshot_path} is not archived: {exc}") from exc
    generated_at = parse_generated_at(snapshot.get("generated_at"))

    history = checkout / HISTORY_DIR
    history.mkdir(parents=True, exist_ok=True)
    day = trading_date(generated_at)
    market_date = snapshot.get("market_date")
    if day is None:
        message = (
            f"Skipped: snapshot at {generated_at.isoformat()} was taken during "
            "a VİOP session, not after its close"
        )
    elif market_date != day.isoformat():
        # Its days left and yields are counted from market_date, so filing it
        # under another session would misdate them.
        message = (
            f"Skipped: snapshot at {generated_at.isoformat()} closes the "
            f"{day.isoformat()} session, but its market_date is {market_date}, "
            "so its days left and yields count from another day"
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
