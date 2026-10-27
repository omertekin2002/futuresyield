#!/usr/bin/env python3
"""Validate a market snapshot against schema version 4.

The rules mirror what `update_data.build_snapshot()` and `normalize_contract()`
write today. Stdlib only, so the archive job can use it without borsapy.

Usage: python -m scripts.snapshot_schema SNAPSHOT [SNAPSHOT ...]
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 4
CONTRACT_STATUSES = {"available", "unavailable"}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MARKET_STRINGS = ("pair", "base_asset", "quote_asset", "spot_unit", "curve_unit")
SPOT_NUMBERS = ("open", "high", "low", "change", "change_percent")
SPOT_TREND_NUMBERS = (
    "start_spot",
    "end_spot",
    "daily_percent",
    "monthly_percent",
    "annualized_percent",
)
CONTRACT_STRINGS = ("symbol", "code", "label", "source")
CONTRACT_NUMBERS = (
    "last",
    "change",
    "change_percent",
    "open",
    "high",
    "low",
    "bid",
    "ask",
    "volume",
    "turnover_try",
    "premium_percent",
    "annualized_premium_percent",
    "daily_yield_factor",
    "daily_yield_percent",
    "monthly_yield_percent",
    "annualized_yield_percent",
    "expected_spot_at_maturity",
    "expected_short_profit_try",
)
CONTRACT_KEYS = (
    *CONTRACT_STRINGS,
    *CONTRACT_NUMBERS,
    "maturity_date",
    "days_to_maturity",
    "updated_at",
    "status",
)


class SnapshotSchemaError(ValueError):
    """A snapshot broke the schema; `problems` lists each as (path, reason)."""

    def __init__(self, problems: list[tuple[str, str]]):
        self.problems = problems
        lines = "\n".join(f"  {path}: {reason}" for path, reason in problems)
        super().__init__(
            f"Invalid market snapshot ({len(problems)} problem"
            f"{'' if len(problems) == 1 else 's'}):\n{lines}"
        )


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    """A finite JSON number; bools, strings, NaN and infinities are not."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def is_iso_date(value: Any) -> bool:
    if not isinstance(value, str) or not ISO_DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_iso_timestamp(value: Any) -> bool:
    """An ISO 8601 timestamp that carries a UTC offset."""
    if not isinstance(value, str) or not ISO_DATE.match(value[:10]):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


class Checker:
    def __init__(self) -> None:
        self.problems: list[tuple[str, str]] = []

    def fail(self, path: str, reason: str) -> None:
        self.problems.append((path, reason))

    def string(self, obj: dict[str, Any], key: str, path: str) -> None:
        value = obj.get(key)
        if not isinstance(value, str) or not value:
            self.fail(f"{path}.{key}", f"must be a non-empty string, got {value!r}")

    def number_or_null(self, obj: dict[str, Any], key: str, path: str) -> None:
        value = obj.get(key)
        if value is not None and not is_number(value):
            self.fail(f"{path}.{key}", f"must be a finite number or null, got {value!r}")

    def positive_number(self, obj: dict[str, Any], key: str, path: str) -> None:
        value = obj.get(key)
        if not is_number(value) or value <= 0:
            self.fail(f"{path}.{key}", f"must be a finite positive number, got {value!r}")

    def snapshot(self, snapshot: Any) -> None:
        if not isinstance(snapshot, dict):
            self.fail("$", f"must be an object, got {type(snapshot).__name__}")
            return

        version = snapshot.get("schema_version")
        if not is_int(version) or version != SCHEMA_VERSION:
            self.fail("schema_version", f"must be {SCHEMA_VERSION}, got {version!r}")
        if not is_iso_timestamp(snapshot.get("generated_at")):
            self.fail(
                "generated_at",
                f"must be an ISO timestamp with offset, got {snapshot.get('generated_at')!r}",
            )
        if not is_iso_date(snapshot.get("market_date")):
            self.fail(
                "market_date",
                f"must be an ISO date, got {snapshot.get('market_date')!r}",
            )

        markets = snapshot.get("markets")
        if not isinstance(markets, dict) or not markets:
            self.fail("markets", "must be a non-empty object")
            markets = {}
        order = snapshot.get("market_order")
        if (
            not isinstance(order, list)
            or not order
            or not all(isinstance(key, str) for key in order)
        ):
            self.fail("market_order", f"must be a non-empty list of keys, got {order!r}")
        elif len(set(order)) != len(order) or set(order) != set(markets):
            self.fail(
                "market_order",
                f"must list each key of markets exactly once, got {order!r} "
                f"for markets {sorted(markets)!r}",
            )
        default = snapshot.get("default_market")
        if not isinstance(order, list) or default not in order:
            self.fail("default_market", f"must be in market_order, got {default!r}")

        for key, market in markets.items():
            self.market(key, market, f"markets.{key}")

    def market(self, key: str, market: Any, path: str) -> None:
        if not isinstance(market, dict):
            self.fail(path, "must be an object")
            return
        if market.get("key") != key:
            self.fail(f"{path}.key", f"must be {key!r}, got {market.get('key')!r}")
        for field in MARKET_STRINGS:
            self.string(market, field, path)
        if not is_int(market.get("price_digits")) or market["price_digits"] < 0:
            self.fail(
                f"{path}.price_digits",
                f"must be a non-negative integer, got {market.get('price_digits')!r}",
            )
        size = market.get("contract_size")
        if size is not None and (not is_int(size) or size <= 0):
            self.fail(
                f"{path}.contract_size", f"must be a positive integer or null, got {size!r}"
            )

        spot = market.get("spot")
        if isinstance(spot, dict):
            self.positive_number(spot, "last", f"{path}.spot")
            for field in SPOT_NUMBERS:
                self.number_or_null(spot, field, f"{path}.spot")
        else:
            self.fail(f"{path}.spot", "must be an object")

        trend = market.get("spot_trend")
        if isinstance(trend, dict):
            self.positive_number(trend, "daily_factor", f"{path}.spot_trend")
            for field in SPOT_TREND_NUMBERS:
                self.number_or_null(trend, field, f"{path}.spot_trend")
        elif trend is not None:
            self.fail(f"{path}.spot_trend", "must be an object or null")

        contracts = market.get("contracts")
        if not isinstance(contracts, list) or not contracts:
            self.fail(f"{path}.contracts", "must be a non-empty list")
            return
        symbols: set[str] = set()
        previous_maturity: str | None = None
        priced = False
        for index, contract in enumerate(contracts):
            contract_path = f"{path}.contracts[{index}]"
            if not isinstance(contract, dict):
                self.fail(contract_path, "must be an object")
                continue
            self.contract(contract, contract_path)
            priced = priced or is_number(contract.get("last"))

            symbol = contract.get("symbol")
            if isinstance(symbol, str):
                if symbol in symbols:
                    self.fail(f"{contract_path}.symbol", f"duplicates {symbol!r}")
                symbols.add(symbol)
            maturity = contract.get("maturity_date")
            if is_iso_date(maturity):
                if previous_maturity is not None and maturity < previous_maturity:
                    self.fail(
                        f"{contract_path}.maturity_date",
                        f"{maturity} is before the previous contract's "
                        f"{previous_maturity}; contracts must be sorted by maturity",
                    )
                previous_maturity = maturity
        if not priced:
            self.fail(f"{path}.contracts", "must include at least one priced contract")

    def contract(self, contract: dict[str, Any], path: str) -> None:
        missing = [key for key in CONTRACT_KEYS if key not in contract]
        for key in missing:
            self.fail(f"{path}.{key}", "is missing")

        for field in CONTRACT_STRINGS:
            if field not in missing:
                self.string(contract, field, path)
        for field in CONTRACT_NUMBERS:
            if field not in missing:
                self.number_or_null(contract, field, path)
        if "maturity_date" not in missing and not is_iso_date(contract["maturity_date"]):
            self.fail(
                f"{path}.maturity_date",
                f"must be an ISO date, got {contract['maturity_date']!r}",
            )
        days = contract.get("days_to_maturity")
        if "days_to_maturity" not in missing and (not is_int(days) or days < 0):
            self.fail(
                f"{path}.days_to_maturity", f"must be an integer >= 0, got {days!r}"
            )
        if "updated_at" not in missing and not is_iso_timestamp(contract["updated_at"]):
            self.fail(
                f"{path}.updated_at",
                f"must be an ISO timestamp with offset, got {contract['updated_at']!r}",
            )

        status = contract.get("status")
        if "status" in missing:
            return
        if status not in CONTRACT_STATUSES:
            self.fail(
                f"{path}.status",
                f"must be one of {sorted(CONTRACT_STATUSES)}, got {status!r}",
            )
        elif "last" not in missing:
            expected = "available" if contract["last"] is not None else "unavailable"
            if status != expected:
                self.fail(
                    f"{path}.status",
                    f"is {status!r} but last is {contract['last']!r}; expected {expected!r}",
                )


def validate_snapshot(snapshot: Any) -> None:
    """Raise SnapshotSchemaError (a ValueError) listing every schema problem."""
    checker = Checker()
    checker.snapshot(snapshot)
    if checker.problems:
        raise SnapshotSchemaError(checker.problems)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshots", nargs="+", type=Path, help="Snapshot JSON files")
    return parser.parse_args()


def main() -> None:
    failed = False
    for path in parse_args().snapshots:
        try:
            validate_snapshot(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            failed = True
            print(f"{path}: {exc}")
        else:
            print(f"{path}: valid schema version {SCHEMA_VERSION} snapshot")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
