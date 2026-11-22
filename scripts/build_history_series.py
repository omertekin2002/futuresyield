"""Build one History file per market from the archived closes in a site.

Usage: python3 scripts/build_history_series.py HISTORY_DIR

HISTORY_DIR is the site's copy of the archive (`_site/data/history/`):
`index.json` and one `<YYYY-MM-DD>.json` close per listed day. For each market
key found in those closes, this writes `HISTORY_DIR/markets/<KEY>.json`:

    {
      "schema_version": 1,
      "market": "USDTRY",
      "days": [
        {
          "date": "2026-10-26",
          "schema_version": 4,
          "generated_at": "...",
          "market_date": "...",
          "market": { ...that close's USDTRY object, unchanged... } or null
        },
        ...
      ]
    }

with one entry per day of `index.json`, in index order, so the dashboard's
History reads one file per market instead of one per close. Only days the index
lists are read. A listed close that is missing or unreadable fails the build,
so a bundle never silently disagrees with the index. With no closes listed,
nothing is written. Standard library only: the Pages build runs it with the
runner's python3.
"""

import json
from pathlib import Path
import re
import sys

BUNDLE_SCHEMA_VERSION = 1
HISTORY_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MARKET_KEY = re.compile(r"^[A-Z0-9]+$")


class BundleError(Exception):
    """The archive can't be turned into bundles that match its index."""


def read_json(path, what):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BundleError(f"{what} is missing: {path}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleError(f"{what} could not be read: {path}: {error}") from None


def listed_days(index):
    """The index's dates, in its order, each checked to name a close file."""
    entries = index.get("days") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        raise BundleError("index.json has no days list")
    days = []
    for position, entry in enumerate(entries):
        day = entry.get("date") if isinstance(entry, dict) else None
        if not isinstance(day, str) or not HISTORY_DATE.match(day):
            raise BundleError(f"index.json days[{position}] has no valid date: {entry!r}")
        if day in days:
            raise BundleError(f"index.json lists {day} twice")
        days.append(day)
    return days


def build_bundles(history_dir):
    """Return {market key: bundle} for the closes history_dir's index lists."""
    history_dir = Path(history_dir)
    days = listed_days(read_json(history_dir / "index.json", "History index"))
    closes = {}
    keys = []
    for day in days:
        close = read_json(history_dir / f"{day}.json", f"Archived close {day}")
        markets = close.get("markets") if isinstance(close, dict) else None
        if not isinstance(markets, dict):
            raise BundleError(f"Archived close {day} has no markets object")
        for field in ("generated_at", "market_date"):
            if not isinstance(close.get(field), str):
                raise BundleError(f"Archived close {day} has no {field}")
        order = close.get("market_order")
        for key in [*(order if isinstance(order, list) else []), *markets]:
            if key in markets and key not in keys:
                if not isinstance(key, str) or not MARKET_KEY.match(key):
                    raise BundleError(f"Archived close {day} has an invalid market key {key!r}")
                keys.append(key)
        closes[day] = close

    return {
        key: {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "market": key,
            "days": [
                {
                    "date": day,
                    "schema_version": closes[day].get("schema_version"),
                    "generated_at": closes[day]["generated_at"],
                    "market_date": closes[day]["market_date"],
                    "market": closes[day]["markets"].get(key),
                }
                for day in days
            ],
        }
        for key in keys
    }


def write_bundles(history_dir):
    """Write each market's bundle under history_dir/markets/. Returns the keys."""
    bundles = build_bundles(history_dir)
    if not bundles:
        return []
    directory = Path(history_dir) / "markets"
    directory.mkdir(exist_ok=True)
    for key, bundle in bundles.items():
        (directory / f"{key}.json").write_text(
            json.dumps(bundle, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    return list(bundles)


def main(argv):
    if len(argv) != 2:
        print(f"Usage: {argv[0]} HISTORY_DIR", file=sys.stderr)
        return 2
    try:
        keys = write_bundles(argv[1])
    except BundleError as error:
        print(f"History bundles not built: {error}", file=sys.stderr)
        return 1
    if keys:
        print(f"Built History bundles for {', '.join(keys)}")
    else:
        print("No archived closes listed; no History bundles built")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
