from datetime import date, datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.update_data import (
    MARKETS,
    build_snapshot,
    compounded_yield_percent,
    daily_yield_factor,
    fetch_spot_trend,
    fetch_stream_quotes,
    gold_try_per_gram,
    maturity_date,
    normalize_contract,
    parse_contract_symbol,
    projected_spot,
    short_expected_profit,
    symbol_from_table_code,
)


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

    def test_converts_ounce_gold_and_usdtry_to_try_per_gram(self):
        expected = 4_027.445 * 47.18152 / 31.1034768
        self.assertAlmostEqual(
            gold_try_per_gram(4_027.445, 47.18152),
            expected,
            places=12,
        )
        self.assertIsNone(gold_try_per_gram(None, 47.18152))


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
            generated_at=now,
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

    def test_projects_usdtry_short_at_bid(self):
        now = datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("Europe/Istanbul"))
        result = normalize_contract(
            {"symbol": "USDTRYQ2026"},
            {"last": 48.0, "bid": 47.99, "ask": 48.01},
            None,
            spot_last=47.0,
            today=now.date(),
            generated_at=now,
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
            generated_at=now,
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
            generated_at=now,
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
            generated_at=now,
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


class SnapshotTests(unittest.TestCase):
    def build_with_fakes(self, trend):
        now = datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("Europe/Istanbul"))
        spot_values = {"USDTRY": 47.0, "EURTRY": 54.0, "XAUTRY": 6_100.0}

        def fake_spot(market, _usd_spot=None):
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

    def test_spot_trend_failure_keeps_snapshot(self):
        def failing_trend(*_args):
            raise RuntimeError("history down")

        snapshot = self.build_with_fakes(failing_trend)

        usd = snapshot["markets"]["USDTRY"]
        self.assertIsNone(usd["spot_trend"])
        self.assertEqual(usd["contracts"][0]["last"], 48.0)
        self.assertIsNone(usd["contracts"][0]["expected_short_profit_try"])


if __name__ == "__main__":
    unittest.main()
