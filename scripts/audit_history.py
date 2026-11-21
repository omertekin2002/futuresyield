#!/usr/bin/env python3
"""Audit an archived history/ directory (as on the data-history branch).

Checks every archived close against the code that reads it, using the rules in
`archive_snapshot` and `snapshot_schema` rather than restating them:

- every `YYYY-MM-DD.json` passes the current snapshot schema;
- its `generated_at` is an end-of-day snapshot for the day in its file name;
- `index.json` exists, parses, and equals `build_manifest()` of the directory;
- the directory holds nothing else, such as a `*.tmp` left by `write_atomic`.

Only reads; it reports problems and never repairs them. Needs `holidays`.

Usage: python -m scripts.audit_history HISTORY_DIR
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

if __package__:
    from .archive_snapshot import (
        DAY_FILE,
        MANIFEST_NAME,
        MANIFEST_SCHEMA_VERSION,
        build_manifest,
        manifest_entry,
        parse_generated_at,
        trading_date,
    )
    from .snapshot_schema import SnapshotSchemaError, validate_snapshot
else:  # run as `python scripts/audit_history.py`
    from archive_snapshot import (
        DAY_FILE,
        MANIFEST_NAME,
        MANIFEST_SCHEMA_VERSION,
        build_manifest,
        manifest_entry,
        parse_generated_at,
        trading_date,
    )
    from snapshot_schema import SnapshotSchemaError, validate_snapshot


Problem = tuple[str, str]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def audit_day(path: Path, day: str, snapshot: Any) -> list[Problem]:
    """Check one archived close against the schema and the end-of-day rule."""
    problems: list[Problem] = []
    try:
        validate_snapshot(snapshot)
    except SnapshotSchemaError as exc:
        problems += [(f"{path}: {where}", reason) for where, reason in exc.problems]
    if not isinstance(snapshot, dict):
        return problems
    try:
        generated_at = parse_generated_at(snapshot.get("generated_at"))
    except ValueError as exc:
        # The schema check has already named a malformed timestamp.
        if not problems:
            problems.append((str(path), f"generated_at can't be read: {exc}"))
        return problems
    closes = trading_date(generated_at)
    if closes is None:
        problems.append((
            str(path),
            f"generated_at {snapshot['generated_at']} was taken during a VİOP "
            "session, so it is not an end-of-day snapshot",
        ))
    elif closes.isoformat() != day:
        problems.append((
            str(path),
            f"generated_at {snapshot['generated_at']} is the close of "
            f"{closes.isoformat()}, not {day}",
        ))
    return problems


def audit_index(history: Path, snapshots: dict[str, Any], files: set[str]) -> list[Problem]:
    """Compare index.json with the index build_manifest() would write."""
    path = history / MANIFEST_NAME
    if not path.is_file():
        return [(str(path), "is missing")]
    try:
        index = read_json(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [(str(path), f"is not valid JSON: {exc}")]
    if not isinstance(index, dict) or not isinstance(index.get("days"), list):
        return [(str(path), 'must be an object with a "days" list')]

    problems: list[Problem] = []
    if index.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        problems.append((
            f"{path}: schema_version",
            f"is {index.get('schema_version')!r}, expected {MANIFEST_SCHEMA_VERSION}",
        ))
    extra = sorted(set(index) - {"schema_version", "days"})
    if extra:
        problems.append((str(path), f"has unexpected keys {extra}"))

    listed: list[str] = []
    for position, entry in enumerate(index["days"]):
        where = f"{path}: days[{position}]"
        day = entry.get("date") if isinstance(entry, dict) else None
        if not isinstance(day, str):
            problems.append((where, f"has no date: {entry!r}"))
            continue
        if day in listed:
            problems.append((where, f"lists {day} more than once"))
            continue
        listed.append(day)
        name = f"{day}.json"
        if name not in files:
            problems.append((where, f"lists {day}, but {history / name} does not exist"))
            continue
        snapshot = snapshots.get(day)
        if snapshot is None:
            continue  # the day file itself is unreadable and already reported
        try:
            expected = manifest_entry(day, snapshot)
        except (KeyError, TypeError, AttributeError):
            continue  # the schema check has already named what is missing
        for key in sorted(set(entry) | set(expected)):
            if entry.get(key) != expected.get(key):
                problems.append((
                    f"{where}.{key}",
                    f"is {entry.get(key)!r} but {history / name} gives "
                    f"{expected.get(key)!r}",
                ))
    for name in sorted(files):
        day = DAY_FILE.match(name).group(1)
        if day not in listed:
            problems.append((str(path), f"has no entry for {history / name}"))
    if listed != sorted(listed):
        problems.append((f"{path}: days", "are not sorted by date"))
    return problems


def audit(history: Path) -> tuple[list[str], list[Problem]]:
    """Return the archived days, oldest first, and every problem found."""
    if not history.is_dir():
        return [], [(str(history), "is not a directory")]
    problems: list[Problem] = []
    snapshots: dict[str, Any] = {}
    files: set[str] = set()
    for path in sorted(history.iterdir()):
        if path.name == MANIFEST_NAME:
            continue
        match = DAY_FILE.match(path.name)
        if not match or not path.is_file():
            kind = "directory" if path.is_dir() else "file"
            problems.append((str(path), f"unexpected {kind}; the archive holds only "
                             f"{MANIFEST_NAME} and YYYY-MM-DD.json closes"))
            continue
        files.add(path.name)
        try:
            snapshot = read_json(path)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            problems.append((str(path), f"is not valid JSON: {exc}"))
            continue
        snapshots[match.group(1)] = snapshot
        problems += audit_day(path, match.group(1), snapshot)
    problems += audit_index(history, snapshots, files)
    # The checks above name each difference; this catches any they don't.
    index = history / MANIFEST_NAME
    if not problems and read_json(index) != build_manifest(history):
        problems.append((str(index), "differs from build_manifest() of the directory"))
    return sorted(name.removesuffix(".json") for name in files), problems


def closes(count: int) -> str:
    return f"{count} archived close{'' if count == 1 else 's'}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("history", type=Path, help="history/ directory of a data-history checkout")
    return parser.parse_args()


def main() -> None:
    history = parse_args().history
    days, problems = audit(history)
    if problems:
        for path, reason in problems:
            print(f"{path}: {reason}")
        print(f"{len(problems)} problem{'' if len(problems) == 1 else 's'} "
              f"in {history} ({closes(len(days))})")
        raise SystemExit(1)
    span = f" ({days[0]} to {days[-1]})" if days else ""
    print(f"{history}: {closes(len(days))}{span}, index consistent")


if __name__ == "__main__":
    main()
