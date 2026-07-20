from datetime import date, datetime
import unittest
from zoneinfo import ZoneInfo

from scripts.update_data import (
    maturity_date,
    normalize_contract,
    parse_contract_symbol,
    symbol_from_table_code,
)


class ContractParsingTests(unittest.TestCase):
    def test_parses_tradingview_symbol(self):
        self.assertEqual(parse_contract_symbol("USDTRYQ2026"), (2026, 8))

    def test_converts_viop_table_code(self):
        self.assertEqual(symbol_from_table_code("F_USDTRY1226"), "USDTRYZ2026")

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
        self.assertEqual(result["status"], "available")


if __name__ == "__main__":
    unittest.main()
