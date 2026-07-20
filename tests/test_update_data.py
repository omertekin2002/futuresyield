from datetime import date, datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.update_data import (
    MARKETS,
    build_snapshot,
    compounded_yield_percent,
    daily_yield_factor,
    gold_try_per_gram,
    maturity_date,
    normalize_contract,
    parse_contract_symbol,
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


class SnapshotTests(unittest.TestCase):
    def test_builds_all_three_market_snapshots(self):
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
            return {
                symbol: {
                    "last": next(
                        value
                        for key, value in values.items()
                        if symbol.startswith(key)
                    )
                }
                for symbol in symbols
            }

        empty_tables = {key: {} for key in MARKETS}
        with (
            patch("scripts.update_data.fetch_spot", side_effect=fake_spot),
            patch("scripts.update_data.fetch_viop_tables", return_value=empty_tables),
            patch("scripts.update_data.discover_contracts", side_effect=fake_discovery),
            patch("scripts.update_data.fetch_stream_quotes", side_effect=fake_quotes),
        ):
            snapshot = build_snapshot(now)

        self.assertEqual(snapshot["schema_version"], 3)
        self.assertEqual(snapshot["market_order"], ["USDTRY", "EURTRY", "XAUTRY"])
        self.assertEqual(set(snapshot["markets"]), set(MARKETS))
        self.assertEqual(snapshot["markets"]["XAUTRY"]["price_digits"], 2)
        self.assertEqual(
            snapshot["markets"]["EURTRY"]["contracts"][0]["symbol"],
            "EURTRYQ2026",
        )


if __name__ == "__main__":
    unittest.main()
