#!/usr/bin/env python3
"""Build the static TRY-denominated VIOP market snapshot used by the dashboard."""

from __future__ import annotations

import argparse
import calendar
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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
TROY_OUNCE_GRAMS = 31.1034768


@dataclass(frozen=True)
class MarketConfig:
    """Describe one spot/futures market and its upstream symbol conventions."""

    key: str
    pair: str
    futures_symbol: str
    table_symbol: str
    spot_asset: str
    base_asset: str
    spot_unit: str
    curve_unit: str
    price_digits: int
    intraday_spot: bool = True


MARKET_CONFIGS = (
    MarketConfig(
        key="USDTRY",
        pair="USD/TRY",
        futures_symbol="USDTRY",
        table_symbol="USDTRY",
        spot_asset="USD",
        base_asset="USD",
        spot_unit="TRY per USD",
        curve_unit="TRY / USD",
        price_digits=4,
    ),
    MarketConfig(
        key="EURTRY",
        pair="EUR/TRY",
        futures_symbol="EURTRY",
        table_symbol="EURTRY",
        spot_asset="EUR",
        base_asset="EUR",
        spot_unit="TRY per EUR",
        curve_unit="TRY / EUR",
        price_digits=4,
    ),
    MarketConfig(
        key="XAUTRY",
        pair="XAU/TRY",
        futures_symbol="XAUTRY",
        table_symbol="XAUTRYM",
        spot_asset="gram-altin",
        base_asset="XAU",
        spot_unit="TRY per gram",
        curve_unit="TRY / GR XAU",
        price_digits=2,
        intraday_spot=False,
    ),
)
MARKETS = {market.key: market for market in MARKET_CONFIGS}
DEFAULT_MARKET = MARKETS["USDTRY"]
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


@lru_cache(maxsize=None)
def contract_pattern(futures_symbol: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(futures_symbol)}([FGHJKMNQUVXZ])(\d{{4}})$")


@lru_cache(maxsize=None)
def table_code_pattern(table_symbol: str) -> re.Pattern[str]:
    return re.compile(rf"^F_{re.escape(table_symbol)}(\d{{2}})(\d{{2}})$")


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


def parse_contract_symbol(
    symbol: str,
    market: MarketConfig = DEFAULT_MARKET,
) -> tuple[int, int]:
    match = contract_pattern(market.futures_symbol).fullmatch(symbol.upper())
    if not match:
        raise ValueError(f"Unsupported {market.pair} contract symbol: {symbol}")
    month_code, year_text = match.groups()
    return int(year_text), MONTH_CODES[month_code]


def symbol_from_table_code(
    code: str,
    market: MarketConfig = DEFAULT_MARKET,
) -> str:
    match = table_code_pattern(market.table_symbol).fullmatch(code.upper())
    if not match:
        raise ValueError(f"Unsupported {market.pair} VİOP table code: {code}")
    month_text, short_year = match.groups()
    month = int(month_text)
    if month not in MONTH_TO_CODE:
        raise ValueError(f"Invalid month in VİOP table code: {code}")
    return f"{market.futures_symbol}{MONTH_TO_CODE[month]}20{short_year}"


def daily_yield_factor(
    contract_price: float | None,
    spot_price: float,
    days_left: int,
) -> float | None:
    """Return the gross daily factor: (contract / spot) ** (1 / days)."""
    if contract_price is None or contract_price <= 0 or spot_price <= 0 or days_left <= 0:
        return None
    return (contract_price / spot_price) ** (1 / days_left)


def compounded_yield_percent(factor: float | None, period_days: int) -> float | None:
    """Compound a gross daily factor and express the net result as a percentage."""
    if factor is None or factor <= 0 or period_days <= 0:
        return None
    return (factor**period_days - 1) * 100


def gold_try_per_gram(
    gold_usd_per_ounce: float | None,
    usd_try: float | None,
) -> float | None:
    """Convert ounce gold in USD to gram gold in TRY."""
    gold = finite_number(gold_usd_per_ounce)
    currency = finite_number(usd_try)
    if gold is None or currency is None or gold <= 0 or currency <= 0:
        return None
    return gold * currency / TROY_OUNCE_GRAMS


def fetch_gold_try_intraday(
    market: MarketConfig,
    usd_spot: dict[str, Any],
) -> dict[str, Any]:
    """Build a fresh TRY-per-gram gold reference from XAUUSD and USDTRY."""
    frame = bp.FX("XAU").history(period="1g", interval="15m")
    if frame.empty:
        raise RuntimeError("borsapy returned no XAUUSD history")

    latest_at = frame.index[-1]
    latest_day = latest_at.date()
    day_frame = frame[frame.index.date == latest_day]
    latest = day_frame.iloc[-1]
    first = day_frame.iloc[0]
    usd_last = finite_number(usd_spot.get("last"))
    usd_open = finite_number(usd_spot.get("open")) or usd_last
    usd_high = finite_number(usd_spot.get("high")) or usd_last
    usd_low = finite_number(usd_spot.get("low")) or usd_last
    last = gold_try_per_gram(latest["Close"], usd_last)
    open_price = gold_try_per_gram(first["Open"], usd_open)
    high = finite_number(
        day_frame["High"].max() * usd_high / TROY_OUNCE_GRAMS
    )
    low = finite_number(
        day_frame["Low"].min() * usd_low / TROY_OUNCE_GRAMS
    )
    if last is None:
        raise RuntimeError("borsapy returned no usable intraday gold reference")

    change = last - open_price if open_price is not None else None
    change_percent = (
        change / open_price * 100 if change is not None and open_price else None
    )
    updated_candidates = [
        value
        for value in (iso_timestamp(latest_at), usd_spot.get("updated_at"))
        if value
    ]
    return {
        "symbol": market.pair,
        "last": last,
        "open": open_price,
        "high": high,
        "low": low,
        "change": change,
        "change_percent": change_percent,
        "updated_at": min(updated_candidates) if updated_candidates else None,
        "source": "Synthetic XAUUSD × USDTRY via borsapy FX",
    }


def fetch_spot(
    market: MarketConfig = DEFAULT_MARKET,
    usd_spot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch the spot reference that matches one futures underlying."""
    if market.key == "XAUTRY" and usd_spot:
        try:
            return fetch_gold_try_intraday(market, usd_spot)
        except Exception as exc:
            print(f"Warning: intraday {market.pair} spot unavailable: {exc}")

    fx = bp.FX(market.spot_asset)
    if market.intraday_spot:
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
                        "symbol": market.pair,
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
            print(f"Warning: intraday {market.pair} spot unavailable: {exc}")

    raw = fx.current
    last = finite_number(raw.get("last"))
    if last is None:
        raise RuntimeError(f"borsapy returned no {market.pair} spot price")

    open_price = finite_number(raw.get("open"))
    change = last - open_price if open_price is not None else None
    change_percent = (
        change / open_price * 100 if change is not None and open_price else None
    )
    return {
        "symbol": market.pair,
        "last": last,
        "open": open_price,
        "high": finite_number(raw.get("high")),
        "low": finite_number(raw.get("low")),
        "change": change,
        "change_percent": change_percent,
        "updated_at": iso_timestamp(raw.get("update_time")),
        "source": raw.get("source") or "borsapy FX current fallback",
    }


def fetch_viop_tables() -> dict[str, dict[str, dict[str, Any]]]:
    """Fetch VİOP table fallbacks keyed by market and stream symbol."""
    rows = {market.key: {} for market in MARKET_CONFIGS}
    try:
        frame = bp.VIOP().futures
    except Exception as exc:  # Streaming remains the primary data path.
        print(f"Warning: VİOP table fallback unavailable: {exc}")
        return rows

    if frame.empty:
        return rows

    for raw in frame.to_dict(orient="records"):
        code = str(raw.get("code") or "").upper()
        for market in MARKET_CONFIGS:
            if not table_code_pattern(market.table_symbol).fullmatch(code):
                continue
            try:
                symbol = symbol_from_table_code(code, market)
            except ValueError:
                continue

            # In borsapy 0.10.2 these normalized names map to the live table as:
            # volume_tl -> absolute price change, volume_qty -> TRY turnover.
            rows[market.key][symbol] = {
                "symbol": symbol,
                "code": code,
                "description": str(raw.get("contract") or ""),
                "last": finite_number(raw.get("price")),
                "change": finite_number(raw.get("volume_tl")),
                "change_percent": finite_number(raw.get("change")),
                "turnover_try": finite_number(raw.get("volume_qty")),
            }
            break
    return rows


def discover_contracts(
    market: MarketConfig,
    table_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Union TradingView contract discovery with the published VİOP table."""
    discovered: dict[str, dict[str, Any]] = {}
    try:
        raw_contracts = bp.viop_contracts(market.futures_symbol, full_info=True)
    except Exception as exc:
        print(f"Warning: {market.pair} contract discovery unavailable: {exc}")
        raw_contracts = []

    for raw in raw_contracts:
        symbol = str(raw.get("symbol") or "").upper()
        if (
            raw.get("is_continuous")
            or not contract_pattern(market.futures_symbol).fullmatch(symbol)
        ):
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
        raise RuntimeError(f"borsapy returned no dated {market.pair} futures contracts")

    return sorted(
        discovered.values(),
        key=lambda item: parse_contract_symbol(item["symbol"], market),
    )


def fetch_stream_quotes(symbols: list[str], timeout: float = 10.0) -> dict[str, dict[str, Any]]:
    """Fetch every contract concurrently, reconnecting once for missing quotes."""
    quotes: dict[str, dict[str, Any]] = {}
    for pass_number in range(1, 3):
        pending = [symbol for symbol in symbols if symbol not in quotes]
        if not pending:
            break

        stream = bp.TradingViewStream()
        try:
            stream.connect(timeout=timeout)
            for symbol in pending:
                stream.subscribe(symbol)

            with ThreadPoolExecutor(max_workers=min(16, len(pending))) as executor:
                futures = {
                    executor.submit(stream.wait_for_quote, symbol, timeout): symbol
                    for symbol in pending
                }
                for future in as_completed(futures):
                    symbol = futures[future]
                    try:
                        quote = future.result()
                        if finite_number(quote.get("last")) is not None:
                            quotes[symbol] = quote
                    except Exception as exc:
                        print(
                            f"Warning: no streaming quote for {symbol} "
                            f"(pass {pass_number}): {exc}"
                        )
        except Exception as exc:
            print(f"Warning: TradingView stream unavailable (pass {pass_number}): {exc}")
        finally:
            for symbol in pending:
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
    market: MarketConfig = DEFAULT_MARKET,
) -> dict[str, Any]:
    symbol = metadata["symbol"]
    year, month = parse_contract_symbol(symbol, market)
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
    yield_factor = daily_yield_factor(last, spot_last, days_left)

    return {
        "symbol": symbol,
        "code": table_row.get("code")
        or f"F_{market.table_symbol}{month:02d}{str(year)[2:]}",
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
        "daily_yield_factor": yield_factor,
        "daily_yield_percent": compounded_yield_percent(yield_factor, 1),
        "monthly_yield_percent": compounded_yield_percent(yield_factor, 30),
        "annualized_yield_percent": compounded_yield_percent(yield_factor, 365),
        "updated_at": iso_timestamp(quote.get("timestamp"))
        or generated_at.isoformat(timespec="seconds"),
        "status": "available" if last is not None else "unavailable",
        "source": source,
    }


def build_snapshot(now: datetime | None = None) -> dict[str, Any]:
    generated_at = (now or datetime.now(ISTANBUL)).astimezone(ISTANBUL)
    today = generated_at.date()
    spots: dict[str, dict[str, Any]] = {}
    currency_markets = tuple(
        market for market in MARKET_CONFIGS if market.key != "XAUTRY"
    )
    with ThreadPoolExecutor(max_workers=len(currency_markets)) as executor:
        futures = {
            executor.submit(fetch_spot, market): market
            for market in currency_markets
        }
        for future in as_completed(futures):
            market = futures[future]
            try:
                spots[market.key] = future.result()
            except Exception as exc:
                raise RuntimeError(f"Could not fetch {market.pair} spot: {exc}") from exc
    try:
        spots["XAUTRY"] = fetch_spot(MARKETS["XAUTRY"], spots["USDTRY"])
    except Exception as exc:
        raise RuntimeError(f"Could not fetch XAU/TRY spot: {exc}") from exc

    table_rows = fetch_viop_tables()
    metadata_by_market = {
        market.key: discover_contracts(market, table_rows[market.key])
        for market in MARKET_CONFIGS
    }
    symbols = [
        item["symbol"]
        for market in MARKET_CONFIGS
        for item in metadata_by_market[market.key]
    ]
    quotes = fetch_stream_quotes(symbols)

    market_snapshots: dict[str, dict[str, Any]] = {}
    for market in MARKET_CONFIGS:
        spot = spots[market.key]
        contracts = [
            normalize_contract(
                item,
                quotes.get(item["symbol"]),
                table_rows[market.key].get(item["symbol"]),
                spot["last"],
                today,
                generated_at,
                market,
            )
            for item in metadata_by_market[market.key]
        ]
        contracts = [item for item in contracts if item["days_to_maturity"] >= 0]
        if not contracts or not any(item["last"] is not None for item in contracts):
            raise RuntimeError(
                f"borsapy returned no usable {market.pair} futures prices"
            )

        market_snapshots[market.key] = {
            "key": market.key,
            "pair": market.pair,
            "base_asset": market.base_asset,
            "quote_asset": "TRY",
            "spot_unit": market.spot_unit,
            "curve_unit": market.curve_unit,
            "price_digits": market.price_digits,
            "spot": spot,
            "contracts": contracts,
        }

    return {
        "schema_version": 3,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "market_date": today.isoformat(),
        "timezone": "Europe/Istanbul",
        "refresh_interval_minutes": 15,
        "default_market": DEFAULT_MARKET.key,
        "market_order": [market.key for market in MARKET_CONFIGS],
        "markets": market_snapshots,
        "sources": {
            "library": f"borsapy {version('borsapy')}",
            "futures": "Borsa İstanbul / TradingView and İş Yatırım via borsapy",
            "spot": "borsapy FX currencies and gram gold",
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
            availability = ", ".join(
                f"{key} "
                f"{sum(c['status'] == 'available' for c in market['contracts'])}/"
                f"{len(market['contracts'])}"
                for key, market in snapshot["markets"].items()
            )
            print(
                f"Wrote {args.output}: {availability} contracts with prices "
                f"at {snapshot['generated_at']}"
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
