from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.update_data import (
    MARKETS,
    TROY_OUNCE_GRAMS,
    align_spot,
    build_snapshot,
    compounded_yield_percent,
    daily_yield_factor,
    fetch_spot_trend,
    fetch_stream_quotes,
    futures_clock,
    gold_try_per_gram,
    main,
    maturity_date,
    normalize_contract,
    parse_contract_symbol,
    projected_spot,
    short_expected_profit,
    symbol_from_table_code,
)
from scripts.snapshot_schema import validate_snapshot


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "market.json"


class ContractParsingTests(unittest.TestCase):
    def test_parses_tradingview_symbol(self):
        self.assertEqual(parse_contract_symbol("USDTRYQ2026"), (2026, 8))

    def test_parses_each_supported_market(self):
        self.assertEqual(
            parse_contract_symbol("EURTRYZ2026", MARKETS["EURTRY"]),
            (2026, 12),
        )
        self.assertEqual(
            parse_contract_symbol("XAUTRYV2027", MARKETS["XAUTRY"]),
            (2027, 10),
        )

    def test_converts_viop_table_code(self):
        self.assertEqual(symbol_from_table_code("F_USDTRY1226"), "USDTRYZ2026")

    def test_converts_gold_table_code_to_stream_symbol(self):
        self.assertEqual(
            symbol_from_table_code("F_XAUTRYM1026", MARKETS["XAUTRY"]),
            "XAUTRYV2026",
        )

    def test_rejects_non_usdtry_contract(self):
        with self.assertRaises(ValueError):
            parse_contract_symbol("EURTRYQ2026")


class MaturityDateTests(unittest.TestCase):
    def test_regular_month_end(self):
        self.assertEqual(maturity_date(2026, 7), date(2026, 7, 31))

    def test_weekend_month_end(self):
        self.assertEqual(maturity_date(2027, 1), date(2027, 1, 29))

    def test_public_holiday_and_half_day_are_excluded(self):
        # 31 Oct 2027 is Sunday, 29 Oct is Republic Day, and 28 Oct is half-day.
        self.assertEqual(maturity_date(2027, 10), date(2027, 10, 27))


class YieldCalculationTests(unittest.TestCase):
    def test_compounds_requested_daily_factor(self):
        factor = daily_yield_factor(48.0, 47.0, 42)
        expected = (48.0 / 47.0) ** (1 / 42)

        self.assertAlmostEqual(factor, expected, places=12)
        self.assertAlmostEqual(
            compounded_yield_percent(factor, 30),
            (expected**30 - 1) * 100,
            places=12,
        )
        self.assertAlmostEqual(
            compounded_yield_percent(factor, 365),
            (expected**365 - 1) * 100,
            places=12,
        )

    def test_mature_contract_has_no_daily_yield(self):
        self.assertIsNone(daily_yield_factor(48.0, 47.0, 0))

    def test_compounding_that_overflows_has_no_yield(self):
        # Past about 7x spot at 1 day left, factor ** 365 overflows a float.
        factor = daily_yield_factor(8 * 47.0, 47.0, 1)
        self.assertIsNone(compounded_yield_percent(factor, 365))
        self.assertAlmostEqual(compounded_yield_percent(factor, 30), (8.0**30 - 1) * 100)
        self.assertAlmostEqual(compounded_yield_percent(factor, 1), 700.0)
        # Just below that, factor ** 365 is finite but times 100 is not.
        self.assertIsNone(compounded_yield_percent(daily_yield_factor(6.95 * 47.0, 47.0, 1), 365))

    def test_converts_ounce_gold_and_usdtry_to_try_per_gram(self):
        expected = 4_027.445 * 47.18152 / 31.1034768
        self.assertAlmostEqual(
            gold_try_per_gram(4_027.445, 47.18152),
            expected,
            places=12,
        )
        self.assertIsNone(gold_try_per_gram(None, 47.18152))


ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")


def bars(rows):
    """A borsapy-shaped 15-minute frame from ``(start, open, high, low, close)`` rows."""
    return pd.DataFrame(
        [row[1:] for row in rows],
        columns=["Open", "High", "Low", "Close"],
        index=pd.DatetimeIndex(
            [pd.Timestamp(row[0], tz="Europe/Istanbul") for row in rows], name="Date"
        ),
    )


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=ISTANBUL_TZ)


class FuturesClockTests(unittest.TestCase):
    def test_newest_trade_among_priced_contracts(self):
        contracts = [
            {"last": 52.0, "updated_at": "2026-11-20T18:09:00+03:00"},
            {"last": 53.0, "updated_at": "2026-11-20T17:59:00+03:00"},
            {"last": 54.0, "updated_at": None},
            # A trade time without a price never sets the clock.
            {"last": None, "updated_at": "2026-11-20T22:20:00+03:00"},
        ]
        self.assertEqual(futures_clock(contracts), at("2026-11-20T18:09"))

    def test_no_trade_time_is_unknown(self):
        self.assertIsNone(futures_clock([{"last": 52.0, "updated_at": None}]))
        self.assertIsNone(futures_clock([]))


class SpotAlignmentTests(unittest.TestCase):
    """align_spot() against small hand-made frames; the replay tests use real bars."""

    USD = bars([
        ("2026-11-19 23:45", 50.40, 50.41, 50.39, 50.40),
        ("2026-11-20 00:00", 50.44, 50.45, 50.43, 50.44),
        ("2026-11-20 18:00", 50.44, 50.60, 50.44, 50.57),
        ("2026-11-20 18:15", 50.57, 50.70, 50.50, 50.58),
        ("2026-11-20 23:15", 50.58, 50.59, 50.30, 50.55),
    ])
    XAU = bars([
        ("2026-11-20 00:00", 3600.0, 3610.0, 3590.0, 3605.0),
        ("2026-11-20 18:00", 3605.0, 3620.0, 3550.0, 3558.0),
        ("2026-11-20 23:15", 3558.0, 3560.0, 3500.0, 3515.0),
    ])
    LATEST_USD = {
        "symbol": "USD/TRY",
        "last": 50.55,
        "open": 50.44,
        "high": 50.70,
        "low": 50.30,
        "change": 0.11,
        "change_percent": 0.2,
        "updated_at": "2026-11-20T23:15:00+03:00",
        "source": "TradingView intraday via borsapy FX",
    }

    def align(self, spot, clock, frames, market="USDTRY"):
        with patch("builtins.print") as mock_print:
            result = align_spot(MARKETS[market], dict(spot), clock, frames)
        self.printed = [call.args[0] for call in mock_print.call_args_list]
        return result

    def test_fresh_spot_is_unchanged(self):
        spot = self.align(self.LATEST_USD, at("2026-11-20T23:05"), {"USD": self.USD})
        self.assertEqual(spot, self.LATEST_USD)
        self.assertEqual(self.printed, [])

    def test_stale_spot_uses_the_bar_that_holds_the_trade(self):
        spot = self.align(self.LATEST_USD, at("2026-11-20T18:09"), {"USD": self.USD})

        self.assertEqual(spot["last"], 50.57)
        self.assertEqual(spot["open"], 50.44)  # the day's first bar, not 19 Nov's
        self.assertEqual(spot["high"], 50.60)  # up to 18:00 only
        self.assertEqual(spot["low"], 50.43)
        self.assertAlmostEqual(spot["change"], 50.57 - 50.44)
        self.assertAlmostEqual(spot["change_percent"], (50.57 - 50.44) / 50.44 * 100)
        self.assertEqual(spot["updated_at"], "2026-11-20T18:00:00+03:00")
        self.assertEqual(spot["source"], self.LATEST_USD["source"])
        self.assertEqual(spot["aligned_to"], "2026-11-20T18:09:00+03:00")
        self.assertEqual(
            spot["latest"], {"last": 50.55, "updated_at": "2026-11-20T23:15:00+03:00"}
        )
        self.assertEqual(self.printed, [])

    def test_trade_on_a_bar_boundary_uses_the_bar_it_starts(self):
        spot = self.align(self.LATEST_USD, at("2026-11-20T18:15"), {"USD": self.USD})
        self.assertEqual(spot["updated_at"], "2026-11-20T18:15:00+03:00")
        self.assertEqual(spot["last"], 50.58)

    def test_no_bar_holds_the_trade(self):
        # 17:30 falls in the gap between the 00:00 and 18:00 bars.
        spot = self.align(self.LATEST_USD, at("2026-11-20T17:30"), {"USD": self.USD})
        self.assertEqual(spot, self.LATEST_USD)
        self.assertEqual(
            self.printed,
            ["Warning: no 15-minute USD/TRY spot bar holds the futures' last trade "
             "at 2026-11-20T17:30:00+03:00; keeping the latest spot"],
        )

    def test_trade_before_the_first_bar(self):
        spot = self.align(self.LATEST_USD, at("2026-11-19T12:00"), {"USD": self.USD})
        self.assertEqual(spot, self.LATEST_USD)
        self.assertEqual(len(self.printed), 1)

    def test_no_frame_for_the_market(self):
        spot = self.align(self.LATEST_USD, at("2026-11-20T18:09"), {})
        self.assertEqual(spot, self.LATEST_USD)
        self.assertIn("no 15-minute USD/TRY spot bar", self.printed[0])

    def test_unknown_clock(self):
        spot = self.align(self.LATEST_USD, None, {"USD": self.USD})
        self.assertEqual(spot, self.LATEST_USD)
        self.assertEqual(
            self.printed,
            ["Warning: USD/TRY futures have no last-trade time; keeping the latest spot"],
        )

    def test_gold_combines_both_legs_at_the_same_time(self):
        latest = {
            "symbol": "XAU/TRY",
            "last": 3515.0 * 50.55 / TROY_OUNCE_GRAMS,
            "updated_at": "2026-11-20T23:15:00+03:00",
            "source": "Synthetic XAUUSD × USDTRY via borsapy FX",
        }
        spot = self.align(
            latest, at("2026-11-20T18:09"), {"USD": self.USD, "XAU": self.XAU}, "XAUTRY"
        )

        self.assertAlmostEqual(spot["last"], 3558.0 * 50.57 / TROY_OUNCE_GRAMS)
        self.assertAlmostEqual(spot["open"], 3600.0 * 50.44 / TROY_OUNCE_GRAMS)
        self.assertAlmostEqual(spot["high"], 3620.0 * 50.60 / TROY_OUNCE_GRAMS)
        self.assertAlmostEqual(spot["low"], 3550.0 * 50.43 / TROY_OUNCE_GRAMS)
        self.assertEqual(spot["updated_at"], "2026-11-20T18:00:00+03:00")
        self.assertEqual(spot["aligned_to"], "2026-11-20T18:09:00+03:00")
        self.assertEqual(spot["latest"]["last"], latest["last"])

    def test_gold_without_its_usd_leg_keeps_the_latest_spot(self):
        latest = {
            "symbol": "XAU/TRY",
            "last": 5711.0,
            "updated_at": "2026-11-20T23:15:00+03:00",
            "source": "Synthetic XAUUSD × USDTRY via borsapy FX",
        }
        spot = self.align(latest, at("2026-11-20T18:09"), {"XAU": self.XAU}, "XAUTRY")
        self.assertEqual(spot, latest)
        self.assertIn("XAU/TRY spot bar", self.printed[0])

    def test_after_midnight_the_trade_day_still_has_its_bar(self):
        frame = bars([
            ("2026-11-20 00:00", 50.44, 50.45, 50.43, 50.44),
            ("2026-11-20 18:00", 50.44, 50.60, 50.44, 50.57),
            ("2026-11-21 00:30", 50.60, 50.61, 50.59, 50.60),
        ])
        latest = dict(self.LATEST_USD, last=50.60, updated_at="2026-11-21T00:30:00+03:00")
        spot = self.align(latest, at("2026-11-20T18:09"), {"USD": frame})

        self.assertEqual(spot["last"], 50.57)
        self.assertEqual(spot["open"], 50.44)  # Friday's open, not Saturday's bar
        self.assertEqual(spot["latest"]["updated_at"], "2026-11-21T00:30:00+03:00")


class SpotTrendTests(unittest.TestCase):
    @staticmethod
    def fake_fx(closes):
        class FakeFX:
            def __init__(self, asset):
                self.asset = asset

            def history(self, start, end):
                frame = pd.DataFrame(
                    {"Close": list(closes.values())},
                    index=pd.to_datetime(list(closes)),
                )
                return frame[(frame.index >= start) & (frame.index <= end)]

        return FakeFX

    def test_uses_close_exactly_180_days_before_spot_date(self):
        closes = {"2026-04-09": 44.5749, "2026-04-10": 44.6304}
        spot = {"last": 49.19, "updated_at": "2026-10-07T13:45:00+03:00"}
        with patch("scripts.update_data.bp.FX", self.fake_fx(closes)):
            trend = fetch_spot_trend(MARKETS["USDTRY"], spot, date(2026, 10, 7))

        self.assertEqual(trend["start_date"], "2026-04-10")
        self.assertEqual(trend["end_date"], "2026-10-07")
        self.assertEqual(trend["days"], 180)
        expected = (49.19 / 44.6304) ** (1 / 180)
        self.assertAlmostEqual(trend["daily_factor"], expected, places=12)
        self.assertAlmostEqual(trend["daily_percent"], (expected - 1) * 100, places=12)

    def test_weekend_target_falls_back_to_prior_close(self):
        # 180 days before Thu 8 Oct 2026 is Sat 11 Apr; Friday 10 Apr is used.
        closes = {"2026-04-09": 44.5749, "2026-04-10": 44.6304, "2026-04-13": 44.7}
        spot = {"last": 49.2, "updated_at": "2026-10-08T10:00:00+03:00"}
        with patch("scripts.update_data.bp.FX", self.fake_fx(closes)):
            trend = fetch_spot_trend(MARKETS["USDTRY"], spot, date(2026, 10, 8))

        self.assertEqual(trend["start_date"], "2026-04-10")
        self.assertEqual(trend["start_spot"], 44.6304)
        self.assertEqual(trend["days"], 181)
        self.assertAlmostEqual(
            trend["daily_factor"], (49.2 / 44.6304) ** (1 / 181), places=12
        )

    def test_missing_history_raises(self):
        spot = {"last": 49.19, "updated_at": "2026-10-07T13:45:00+03:00"}
        with patch("scripts.update_data.bp.FX", self.fake_fx({})):
            with self.assertRaises(RuntimeError):
                fetch_spot_trend(MARKETS["USDTRY"], spot, date(2026, 10, 7))

    def test_projects_spot_and_short_profit(self):
        expected_spot = projected_spot(47.0, 1.0005, 42)
        self.assertAlmostEqual(expected_spot, 47.0 * 1.0005**42, places=12)
        self.assertAlmostEqual(
            short_expected_profit(48.0, expected_spot, 1_000),
            (48.0 - expected_spot) * 1_000,
            places=9,
        )
        self.assertIsNone(short_expected_profit(None, expected_spot, 1_000))
        self.assertIsNone(projected_spot(47.0, None, 42))


class NormalizationTests(unittest.TestCase):
    def test_computes_days_and_spot_premium(self):
        now = datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("Europe/Istanbul"))
        result = normalize_contract(
            {"symbol": "USDTRYQ2026", "description": "Aug 2026"},
            {
                "last": 48.0,
                "change": 0.1,
                "change_percent": 0.21,
                "bid": 47.99,
                "ask": 48.01,
            },
            {"code": "F_USDTRY0826", "turnover_try": 123_000},
            spot_last=47.0,
            today=now.date(),
        )

        self.assertEqual(result["maturity_date"], "2026-08-31")
        self.assertEqual(result["days_to_maturity"], 42)
        self.assertAlmostEqual(result["premium_percent"], 2.127659574, places=6)
        expected_factor = (48.0 / 47.0) ** (1 / 42)
        self.assertAlmostEqual(result["daily_yield_factor"], expected_factor, places=12)
        self.assertAlmostEqual(
            result["monthly_yield_percent"],
            (expected_factor**30 - 1) * 100,
            places=12,
        )
        self.assertEqual(result["status"], "available")
        self.assertIsNone(result["expected_spot_at_maturity"])
        self.assertIsNone(result["expected_short_profit_try"])

    def test_keeps_the_quote_trade_time(self):
        result = normalize_contract(
            {"symbol": "USDTRYQ2026"},
            {"last": 48.0, "timestamp": datetime(2026, 7, 17, 18, 7, tzinfo=timezone.utc)},
            None,
            spot_last=47.0,
            today=date(2026, 7, 20),
        )

        self.assertEqual(result["updated_at"], "2026-07-17T21:07:00+03:00")

    def test_quote_without_a_trade_time_has_null_updated_at(self):
        # Neither the stream quote nor the VİOP table row carries a time.
        for quote in ({"last": 48.0}, {"last": 48.0, "timestamp": None}, None):
            with self.subTest(quote=quote):
                result = normalize_contract(
                    {"symbol": "USDTRYQ2026"},
                    quote,
                    {"code": "F_USDTRY0826", "last": 48.1},
                    spot_last=47.0,
                    today=date(2026, 7, 20),
                )
                self.assertIsNone(result["updated_at"])

    def test_projects_usdtry_short_at_bid(self):
        now = datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("Europe/Istanbul"))
        result = normalize_contract(
            {"symbol": "USDTRYQ2026"},
            {"last": 48.0, "bid": 47.99, "ask": 48.01},
            None,
            spot_last=47.0,
            today=now.date(),
            trend_factor=1.0005,
        )

        expected_spot = 47.0 * 1.0005**42
        self.assertAlmostEqual(
            result["expected_spot_at_maturity"], expected_spot, places=12
        )
        self.assertAlmostEqual(
            result["expected_short_profit_try"],
            (47.99 - expected_spot) * 1_000,
            places=9,
        )

    def test_short_profit_needs_a_bid(self):
        now = datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("Europe/Istanbul"))
        result = normalize_contract(
            {"symbol": "USDTRYQ2026"},
            {"last": 48.0},
            None,
            spot_last=47.0,
            today=now.date(),
            trend_factor=1.0005,
        )

        self.assertIsNotNone(result["expected_spot_at_maturity"])
        self.assertIsNone(result["expected_short_profit_try"])

    def test_markets_without_contract_size_skip_projection(self):
        now = datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("Europe/Istanbul"))
        result = normalize_contract(
            {"symbol": "EURTRYQ2026"},
            {"last": 55.0, "bid": 54.99},
            None,
            spot_last=54.0,
            today=now.date(),
            market=MARKETS["EURTRY"],
            trend_factor=1.0005,
        )

        self.assertIsNone(result["expected_spot_at_maturity"])
        self.assertIsNone(result["expected_short_profit_try"])

    def test_normalizes_gold_contract_with_commodity_table_code(self):
        now = datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("Europe/Istanbul"))
        result = normalize_contract(
            {"symbol": "XAUTRYQ2026", "description": "Aug 2026"},
            {"last": 6_350.0},
            None,
            spot_last=6_100.0,
            today=now.date(),
            market=MARKETS["XAUTRY"],
        )

        self.assertEqual(result["code"], "F_XAUTRYM0826")
        self.assertEqual(result["maturity_date"], "2026-08-31")
        self.assertAlmostEqual(result["premium_percent"], 4.0983606557, places=6)


class StreamQuoteTests(unittest.TestCase):
    def test_cleanup_failure_does_not_discard_received_quotes(self):
        class CleanupFailureStream:
            def connect(self, timeout):
                pass

            def subscribe(self, symbol):
                pass

            def wait_for_quote(self, symbol, timeout):
                return {"last": 48.25}

            def get_quote(self, symbol):
                return None

            def disconnect(self):
                raise AttributeError("'NoneType' object has no attribute 'close_frame'")

        with (
            patch(
                "scripts.update_data.bp.TradingViewStream",
                return_value=CleanupFailureStream(),
            ),
            patch("builtins.print") as mock_print,
        ):
            quotes = fetch_stream_quotes(["USDTRYQ2026"], timeout=0.01)

        self.assertEqual(quotes["USDTRYQ2026"]["last"], 48.25)
        mock_print.assert_called_once_with(
            "Warning: TradingView stream cleanup failed (pass 1): "
            "'NoneType' object has no attribute 'close_frame'"
        )


class FakeSourcesMixin:
    """Build a snapshot from one faked Aug 2026 contract per market."""

    def build_with_fakes(self, trend, now=None, scale=1.0):
        """scale multiplies every faked futures quote, as a mis-scaled feed would."""
        now = now or datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("Europe/Istanbul"))
        spot_values = {"USDTRY": 47.0, "EURTRY": 54.0, "XAUTRY": 6_100.0}

        def fake_spot(market, _usd_spot=None, frames=None):
            return {"symbol": market.pair, "last": spot_values[market.key]}

        def fake_discovery(market, _table_rows):
            return [
                {
                    "symbol": f"{market.futures_symbol}Q2026",
                    "description": "Aug 2026",
                }
            ]

        def fake_quotes(symbols):
            values = {"USDTRY": 48.0, "EURTRY": 55.0, "XAUTRY": 6_350.0}
            quotes = {}
            for symbol in symbols:
                last = next(
                    value for key, value in values.items() if symbol.startswith(key)
                )
                last *= scale
                quotes[symbol] = {"last": last, "bid": last - 0.01}
            return quotes

        empty_tables = {key: {} for key in MARKETS}
        with (
            patch("scripts.update_data.fetch_spot", side_effect=fake_spot),
            patch("scripts.update_data.fetch_viop_tables", return_value=empty_tables),
            patch("scripts.update_data.discover_contracts", side_effect=fake_discovery),
            patch("scripts.update_data.fetch_stream_quotes", side_effect=fake_quotes),
            patch("scripts.update_data.fetch_spot_trend", side_effect=trend) as mock_trend,
            patch("builtins.print"),
        ):
            snapshot = build_snapshot(now)

        mock_trend.assert_called_once()
        self.assertEqual(mock_trend.call_args.args[0], MARKETS["USDTRY"])
        return snapshot


class SnapshotTests(FakeSourcesMixin, unittest.TestCase):
    def test_builds_all_three_market_snapshots(self):
        snapshot = self.build_with_fakes(lambda *_args: {"daily_factor": 1.0005})

        self.assertEqual(snapshot["schema_version"], 4)
        self.assertEqual(snapshot["market_order"], ["USDTRY", "EURTRY", "XAUTRY"])
        self.assertEqual(set(snapshot["markets"]), set(MARKETS))
        self.assertEqual(snapshot["markets"]["XAUTRY"]["price_digits"], 2)
        self.assertEqual(
            snapshot["markets"]["EURTRY"]["contracts"][0]["symbol"],
            "EURTRYQ2026",
        )

        usd = snapshot["markets"]["USDTRY"]
        self.assertEqual(usd["contract_size"], 1_000)
        self.assertEqual(usd["spot_trend"], {"daily_factor": 1.0005})
        expected_spot = 47.0 * 1.0005**42
        self.assertAlmostEqual(
            usd["contracts"][0]["expected_short_profit_try"],
            (47.99 - expected_spot) * 1_000,
            places=9,
        )
        eur = snapshot["markets"]["EURTRY"]
        self.assertIsNone(eur["contract_size"])
        self.assertIsNone(eur["spot_trend"])
        self.assertIsNone(eur["contracts"][0]["expected_short_profit_try"])

    def test_built_snapshots_pass_the_schema(self):
        validate_snapshot(self.build_with_fakes(lambda *_args: {"daily_factor": 1.0005}))

        def failing_trend(*_args):
            raise RuntimeError("history down")

        validate_snapshot(self.build_with_fakes(failing_trend))

    def test_a_quote_whose_yield_overflows_still_publishes_the_snapshot(self):
        # Friday 28 Aug 2026 leaves the Aug 2026 contracts 3 days; a quote at
        # 1,000x spot then overflows the 365-day compounding.
        now = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Europe/Istanbul"))
        snapshot = self.build_with_fakes(
            lambda *_args: {"daily_factor": 1.0005}, now, scale=1_000.0,
        )
        validate_snapshot(snapshot)
        usd = snapshot["markets"]["USDTRY"]["contracts"][0]
        self.assertEqual(usd["days_to_maturity"], 3)
        self.assertEqual(usd["last"], 48_000.0)
        self.assertAlmostEqual(usd["premium_percent"], (48_000.0 / 47.0 - 1) * 100)
        self.assertIsNone(usd["annualized_yield_percent"])
        self.assertAlmostEqual(
            usd["daily_yield_percent"], ((48_000.0 / 47.0) ** (1 / 3) - 1) * 100,
        )

    def test_spot_trend_failure_keeps_snapshot(self):
        def failing_trend(*_args):
            raise RuntimeError("history down")

        snapshot = self.build_with_fakes(failing_trend)

        usd = snapshot["markets"]["USDTRY"]
        self.assertIsNone(usd["spot_trend"])
        self.assertEqual(usd["contracts"][0]["last"], 48.0)
        self.assertIsNone(usd["contracts"][0]["expected_short_profit_try"])


class MarketDateTests(FakeSourcesMixin, unittest.TestCase):
    """market_date is the session whose quotes the snapshot holds, as the archive
    files it, and every Aug 2026 contract counts its days left from it."""

    # The fakes' Aug 2026 contracts mature on Monday 31 Aug 2026.
    MATURITY = date(2026, 8, 31)

    def assert_counted_from(self, now, session):
        snapshot = self.build_with_fakes(lambda *_args: {"daily_factor": 1.0005}, now)
        validate_snapshot(snapshot)
        self.assertEqual(snapshot["generated_at"], now.isoformat(timespec="seconds"))
        self.assertEqual(snapshot["market_date"], session.isoformat())
        for key in MARKETS:
            contract = snapshot["markets"][key]["contracts"][0]
            self.assertEqual(contract["days_to_maturity"], (self.MATURITY - session).days)
        usd = snapshot["markets"]["USDTRY"]["contracts"][0]
        days = (self.MATURITY - session).days
        self.assertAlmostEqual(
            usd["daily_yield_factor"], (48.0 / 47.0) ** (1 / days), places=12
        )
        self.assertAlmostEqual(
            usd["expected_spot_at_maturity"], 47.0 * 1.0005**days, places=9
        )

    @staticmethod
    def at(*args):
        return datetime(*args, tzinfo=ZoneInfo("Europe/Istanbul"))

    def test_from_the_open_until_midnight_is_today(self):
        # Monday 20 Jul 2026 is an ordinary trading day.
        monday = date(2026, 7, 20)
        for now in (self.at(2026, 7, 20, 9, 20), self.at(2026, 7, 20, 12),
                    self.at(2026, 7, 20, 23, 30)):
            with self.subTest(now=now):
                self.assert_counted_from(now, monday)

    def test_after_midnight_and_before_the_open_is_the_previous_session(self):
        monday = date(2026, 7, 20)
        for now in (self.at(2026, 7, 21, 0, 30), self.at(2026, 7, 21, 8),
                    self.at(2026, 7, 21, 9, 19)):
            with self.subTest(now=now):
                self.assert_counted_from(now, monday)

    def test_weekend_and_monday_before_the_open_is_friday(self):
        friday = date(2026, 7, 24)
        for now in (self.at(2026, 7, 25, 12), self.at(2026, 7, 26, 23),
                    self.at(2026, 7, 27, 9, 0)):
            with self.subTest(now=now):
                self.assert_counted_from(now, friday)

    def test_day_after_a_public_holiday_before_the_open(self):
        # Wednesday 15 Jul 2026 is Democracy and National Unity Day, so
        # Thursday morning still holds Tuesday's session.
        self.assert_counted_from(self.at(2026, 7, 16, 8), date(2026, 7, 14))
        self.assert_counted_from(self.at(2026, 7, 15, 14), date(2026, 7, 14))

    def test_half_day_after_its_close_is_today(self):
        # Tuesday 26 May 2026 is the half day before Kurban Bayramı: one
        # session until 12:40, then four public holidays.
        half_day = date(2026, 5, 26)
        for now in (self.at(2026, 5, 26, 11), self.at(2026, 5, 26, 14),
                    self.at(2026, 5, 26, 23, 30), self.at(2026, 5, 29, 10),
                    self.at(2026, 6, 1, 7)):
            with self.subTest(now=now):
                self.assert_counted_from(now, half_day)

    def test_spot_trend_keeps_the_spot_observation_day(self):
        # Only days left move to the session; the trend's fallback end date is
        # still the calendar day the snapshot was taken.
        calls = []

        def trend(*args):
            calls.append(args)
            return {"daily_factor": 1.0005}

        self.build_with_fakes(trend, self.at(2026, 7, 21, 0, 30))
        self.assertEqual(calls[0][2], date(2026, 7, 21))



class MainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "data" / "market.json"
        self.snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def run_main(self, snapshot, attempts=2):
        argv = ["update_data.py", "--output", str(self.output), "--attempts", str(attempts)]
        with (
            patch("sys.argv", argv),
            patch("scripts.update_data.build_snapshot", return_value=snapshot) as build,
            patch("scripts.update_data.time.sleep"),
            patch("builtins.print") as mock_print,
        ):
            main()
        return build, mock_print

    def test_writes_a_valid_snapshot(self):
        build, _print = self.run_main(self.snapshot)

        build.assert_called_once()
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), self.snapshot)

    def test_invalid_snapshot_is_a_failed_attempt_and_never_written(self):
        self.snapshot["markets"]["USDTRY"]["contracts"][0]["days_to_maturity"] = -1

        with self.assertRaises(SystemExit) as caught:
            self.run_main(self.snapshot)

        self.assertIn("keeping last deployment", str(caught.exception.code))
        self.assertIn(
            "markets.USDTRY.contracts[0].days_to_maturity", str(caught.exception.code)
        )
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".json.tmp").exists())

    def test_invalid_snapshot_is_retried(self):
        invalid = json.loads(json.dumps(self.snapshot))
        invalid["schema_version"] = 5
        argv = ["update_data.py", "--output", str(self.output), "--attempts", "3"]
        with (
            patch("sys.argv", argv),
            patch(
                "scripts.update_data.build_snapshot",
                side_effect=[invalid, self.snapshot],
            ) as build,
            patch("scripts.update_data.time.sleep"),
            patch("builtins.print") as mock_print,
        ):
            main()

        self.assertEqual(build.call_count, 2)
        self.assertIn("Attempt 1/3 failed", mock_print.call_args_list[0].args[0])
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8"))["schema_version"], 4
        )


if __name__ == "__main__":
    unittest.main()
