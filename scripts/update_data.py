#!/usr/bin/env python3
"""Build the static USD/TRY VIOP market snapshot used by the dashboard."""

from __future__ import annotations

import argparse
import calendar
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import borsapy as bp
import holidays
from holidays.constants import HALF_DAY, PUBLIC


ISTANBUL = ZoneInfo("Europe/Istanbul")
BASE_SYMBOL = "USDTRY"
CONTRACT_PATTERN = re.compile(r"^USDTRY([FGHJKMNQUVXZ])(\d{4})$")
TABLE_CODE_PATTERN = re.compile(r"^F_USDTRY(\d{2})(\d{2})$")
MONTH_CODES = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}
MONTH_TO_CODE = {month: code for code, month in MONTH_CODES.items()}


def finite_number(value: Any) -> float | None:
    """Return a JSON-safe float or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def iso_timestamp(value: Any) -> str | None:
    """Normalize borsapy datetime and Unix timestamp values to Istanbul ISO time."""
    if value is None:
        return None

    parsed: datetime
    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ISTANBUL)
    return parsed.astimezone(ISTANBUL).isoformat(timespec="seconds")


@lru_cache(maxsize=None)
def market_closures(year: int) -> frozenset[date]:
    """Return Türkiye public and half-day holidays that close VİOP for expiry."""
    public_days = holidays.country_holidays(
        "TR", years=year, observed=False, categories=PUBLIC
    )
    half_days = holidays.country_holidays(
        "TR", years=year, observed=False, categories=HALF_DAY
    )
    return frozenset((*public_days.keys(), *half_days.keys()))


def maturity_date(year: int, month: int) -> date:
    """Calculate VİOP expiry: the month's final full domestic business day."""
    candidate = date(year, month, calendar.monthrange(year, month)[1])
    closures = market_closures(year)
    while candidate.weekday() >= 5 or candidate in closures:
        candidate -= timedelta(days=1)
    return candidate


def parse_contract_symbol(symbol: str) -> tuple[int, int]:
    match = CONTRACT_PATTERN.fullmatch(symbol.upper())
    if not match:
        raise ValueError(f"Unsupported USD/TRY contract symbol: {symbol}")
    month_code, year_text = match.groups()
    return int(year_text), MONTH_CODES[month_code]


def symbol_from_table_code(code: str) -> str:
    match = TABLE_CODE_PATTERN.fullmatch(code.upper())
    if not match:
        raise ValueError(f"Unsupported VİOP table code: {code}")
    month_text, short_year = match.groups()
    month = int(month_text)
    if month not in MONTH_TO_CODE:
        raise ValueError(f"Invalid month in VİOP table code: {code}")
    return f"USDTRY{MONTH_TO_CODE[month]}20{short_year}"


def fetch_spot() -> dict[str, Any]:
    fx = bp.FX("USD")
    try:
        frame = fx.history(period="1g", interval="15m")
        if not frame.empty:
            latest_at = frame.index[-1]
            latest_day = latest_at.date()
            day_frame = frame[frame.index.date == latest_day]
            last = finite_number(day_frame.iloc[-1]["Close"])
            open_price = finite_number(day_frame.iloc[0]["Open"])
            high = finite_number(day_frame["High"].max())
            low = finite_number(day_frame["Low"].min())
            if last is not None:
                change = last - open_price if open_price is not None else None
                change_percent = (
                    change / open_price * 100
                    if change is not None and open_price
                    else None
                )
                return {
                    "symbol": "USD/TRY",
                    "last": last,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "change": change,
                    "change_percent": change_percent,
                    "updated_at": iso_timestamp(latest_at),
                    "source": "TradingView intraday via borsapy FX",
                }
    except Exception as exc:
        print(f"Warning: intraday USD/TRY spot unavailable: {exc}")

    raw = fx.current
    last = finite_number(raw.get("last"))
    if last is None:
        raise RuntimeError("borsapy returned no USD/TRY spot price")

    open_price = finite_number(raw.get("open"))
    change = last - open_price if open_price is not None else None
    change_percent = (
        change / open_price * 100 if change is not None and open_price else None
    )
    return {
        "symbol": "USD/TRY",
        "last": last,
        "open": open_price,
        "high": finite_number(raw.get("high")),
        "low": finite_number(raw.get("low")),
        "change": change,
        "change_percent": change_percent,
        "updated_at": iso_timestamp(raw.get("update_time")),
        "source": raw.get("source") or "borsapy FX current fallback",
    }


def fetch_viop_table() -> dict[str, dict[str, Any]]:
    """Fetch the borsapy VİOP table and key USD/TRY rows by stream symbol."""
    rows: dict[str, dict[str, Any]] = {}
    try:
        frame = bp.VIOP().currency_futures
    except Exception as exc:  # Streaming remains the primary data path.
        print(f"Warning: VİOP table fallback unavailable: {exc}")
        return rows

    if frame.empty:
        return rows

    for raw in frame.to_dict(orient="records"):
        code = str(raw.get("code") or "").upper()
        if not TABLE_CODE_PATTERN.fullmatch(code):
            continue
        try:
            symbol = symbol_from_table_code(code)
        except ValueError:
            continue

        # In borsapy 0.10.2 these normalized names map to the live table as:
        # volume_tl -> absolute price change, volume_qty -> TRY turnover.
        rows[symbol] = {
            "symbol": symbol,
            "code": code,
            "description": str(raw.get("contract") or ""),
            "last": finite_number(raw.get("price")),
            "change": finite_number(raw.get("volume_tl")),
            "change_percent": finite_number(raw.get("change")),
            "turnover_try": finite_number(raw.get("volume_qty")),
        }
    return rows


def discover_contracts(table_rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Union TradingView contract discovery with the published VİOP table."""
    discovered: dict[str, dict[str, Any]] = {}
    try:
        raw_contracts = bp.viop_contracts(BASE_SYMBOL, full_info=True)
    except Exception as exc:
        print(f"Warning: TradingView contract discovery unavailable: {exc}")
        raw_contracts = []

    for raw in raw_contracts:
        symbol = str(raw.get("symbol") or "").upper()
        if raw.get("is_continuous") or not CONTRACT_PATTERN.fullmatch(symbol):
            continue
        discovered[symbol] = {
            "symbol": symbol,
            "description": raw.get("description") or "",
        }

    for symbol, row in table_rows.items():
        discovered.setdefault(
            symbol,
            {"symbol": symbol, "description": row.get("description") or ""},
        )

    if not discovered:
        raise RuntimeError("borsapy returned no dated USD/TRY futures contracts")

    return sorted(discovered.values(), key=lambda item: parse_contract_symbol(item["symbol"]))


def fetch_stream_quotes(symbols: list[str], timeout: float = 10.0) -> dict[str, dict[str, Any]]:
    """Subscribe once, then wait for every contract quote concurrently."""
    quotes: dict[str, dict[str, Any]] = {}
    stream = bp.TradingViewStream()
    try:
        stream.connect(timeout=timeout)
        for symbol in symbols:
            stream.subscribe(symbol)

        with ThreadPoolExecutor(max_workers=min(16, len(symbols))) as executor:
            futures = {
                executor.submit(stream.wait_for_quote, symbol, timeout): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    quotes[symbol] = future.result()
                except Exception as exc:
                    print(f"Warning: no streaming quote for {symbol}: {exc}")
    except Exception as exc:
        print(f"Warning: TradingView stream unavailable: {exc}")
    finally:
        for symbol in symbols:
            if symbol in quotes:
                continue
            late_quote = stream.get_quote(symbol)
            if late_quote and finite_number(late_quote.get("last")) is not None:
                quotes[symbol] = late_quote
        stream.disconnect()
    return quotes


def normalize_contract(
    metadata: dict[str, Any],
    quote: dict[str, Any] | None,
    table_row: dict[str, Any] | None,
    spot_last: float,
    today: date,
    generated_at: datetime,
) -> dict[str, Any]:
    symbol = metadata["symbol"]
    year, month = parse_contract_symbol(symbol)
    expiry = maturity_date(year, month)
    quote = quote or {}
    table_row = table_row or {}

    last = finite_number(quote.get("last"))
    source = "TradingView via borsapy"
    if last is None:
        last = finite_number(table_row.get("last"))
        source = "İş Yatırım via borsapy" if last is not None else "borsapy"

    change = finite_number(quote.get("change"))
    if change is None:
        change = finite_number(table_row.get("change"))
    change_percent = finite_number(quote.get("change_percent"))
    if change_percent is None:
        change_percent = finite_number(table_row.get("change_percent"))

    days_left = (expiry - today).days
    premium_percent = ((last / spot_last) - 1) * 100 if last is not None else None
    annualized_premium = (
        premium_percent * 365 / days_left
        if premium_percent is not None and days_left > 0
        else None
    )

    return {
        "symbol": symbol,
        "code": table_row.get("code") or f"F_USDTRY{month:02d}{str(year)[2:]}",
        "label": f"{calendar.month_abbr[month]} {year}",
        "maturity_date": expiry.isoformat(),
        "days_to_maturity": days_left,
        "last": last,
        "change": change,
        "change_percent": change_percent,
        "open": finite_number(quote.get("open")),
        "high": finite_number(quote.get("high")),
        "low": finite_number(quote.get("low")),
        "bid": finite_number(quote.get("bid")),
        "ask": finite_number(quote.get("ask")),
        "volume": finite_number(quote.get("volume")),
        "turnover_try": finite_number(table_row.get("turnover_try")),
        "premium_percent": premium_percent,
        "annualized_premium_percent": annualized_premium,
        "updated_at": iso_timestamp(quote.get("timestamp"))
        or generated_at.isoformat(timespec="seconds"),
        "status": "available" if last is not None else "unavailable",
        "source": source,
    }


def build_snapshot(now: datetime | None = None) -> dict[str, Any]:
    generated_at = (now or datetime.now(ISTANBUL)).astimezone(ISTANBUL)
    today = generated_at.date()
    spot = fetch_spot()
    table_rows = fetch_viop_table()
    metadata = discover_contracts(table_rows)
    quotes = fetch_stream_quotes([item["symbol"] for item in metadata])

    contracts = [
        normalize_contract(
            item,
            quotes.get(item["symbol"]),
            table_rows.get(item["symbol"]),
            spot["last"],
            today,
            generated_at,
        )
        for item in metadata
    ]
    contracts = [item for item in contracts if item["days_to_maturity"] >= 0]
    if not contracts or not any(item["last"] is not None for item in contracts):
        raise RuntimeError("borsapy returned no usable USD/TRY futures prices")

    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "market_date": today.isoformat(),
        "timezone": "Europe/Istanbul",
        "refresh_interval_minutes": 15,
        "spot": spot,
        "contracts": contracts,
        "sources": {
            "library": f"borsapy {version('borsapy')}",
            "futures": "Borsa İstanbul / TradingView and İş Yatırım via borsapy",
            "spot": "borsapy FX",
            "maturity_rule": "Last full Turkish business day of the contract month",
        },
    }


def write_snapshot(snapshot: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/market.json"),
        help="Snapshot destination (default: data/market.json)",
    )
    parser.add_argument("--attempts", type=int, default=3, help="Critical fetch attempts")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.attempts < 1:
        raise SystemExit("--attempts must be at least 1")

    error: Exception | None = None
    for attempt in range(1, args.attempts + 1):
        try:
            snapshot = build_snapshot()
            write_snapshot(snapshot, args.output)
            available = sum(c["status"] == "available" for c in snapshot["contracts"])
            print(
                f"Wrote {args.output}: {available}/{len(snapshot['contracts'])} "
                f"contracts with prices at {snapshot['generated_at']}"
            )
            return
        except Exception as exc:
            error = exc
            print(f"Attempt {attempt}/{args.attempts} failed: {exc}")
            if attempt < args.attempts:
                time.sleep(attempt * 3)
    raise SystemExit(f"Market snapshot update failed; keeping last deployment: {error}")


if __name__ == "__main__":
    main()
