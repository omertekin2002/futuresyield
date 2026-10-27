import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.snapshot_schema import SnapshotSchemaError, validate_snapshot


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "market.json"
COMMITTED = ROOT / "data" / "market.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ValidSnapshotTests(unittest.TestCase):
    def test_accepts_fixture_and_committed_snapshot(self):
        for path in (FIXTURE, COMMITTED):
            with self.subTest(path=path.name):
                validate_snapshot(load(path))

    def test_accepts_unpriced_contract_and_missing_trend(self):
        snapshot = load(FIXTURE)
        usd = snapshot["markets"]["USDTRY"]
        usd["spot_trend"] = None
        contract = usd["contracts"][-1]
        for key in ("last", "bid", "ask", "premium_percent", "daily_yield_factor"):
            contract[key] = None
        contract["status"] = "unavailable"
        validate_snapshot(snapshot)

    def test_accepts_contract_on_maturity_day(self):
        snapshot = load(FIXTURE)
        snapshot["markets"]["EURTRY"]["contracts"][0]["days_to_maturity"] = 0
        validate_snapshot(snapshot)


class InvalidSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = load(FIXTURE)
        self.usd = self.snapshot["markets"]["USDTRY"]
        self.front = self.usd["contracts"][0]

    def assert_rejected(self, *paths):
        with self.assertRaises(SnapshotSchemaError) as caught:
            validate_snapshot(self.snapshot)
        error = caught.exception
        self.assertIsInstance(error, ValueError)
        reported = [path for path, _reason in error.problems]
        for path in paths:
            self.assertIn(path, reported)
            self.assertIn(path, str(error))
        return error

    def test_wrong_schema_version(self):
        for version in (3, 5, "4", True, None):
            with self.subTest(version=version):
                self.snapshot["schema_version"] = version
                self.assert_rejected("schema_version")

    def test_bad_timestamps_and_dates(self):
        self.snapshot["generated_at"] = "2026-10-07T14:06:12"
        self.snapshot["market_date"] = "07/10/2026"
        self.front["maturity_date"] = "2026-13-30"
        self.front["updated_at"] = 1_760_000_000
        self.assert_rejected(
            "generated_at",
            "market_date",
            "markets.USDTRY.contracts[0].maturity_date",
            "markets.USDTRY.contracts[0].updated_at",
        )

    def test_market_order_missing_a_market(self):
        self.snapshot["market_order"] = ["USDTRY", "EURTRY"]
        self.assert_rejected("market_order")

    def test_market_order_with_unknown_or_duplicate_key(self):
        for order in (
            ["USDTRY", "EURTRY", "XAUTRY", "GBPTRY"],
            ["USDTRY", "EURTRY", "XAUTRY", "USDTRY"],
            [],
        ):
            with self.subTest(order=order):
                self.snapshot["market_order"] = order
                self.assert_rejected("market_order")

    def test_default_market_outside_market_order(self):
        self.snapshot["default_market"] = "GBPTRY"
        self.assert_rejected("default_market")

    def test_market_key_and_fields(self):
        self.usd["key"] = "EURTRY"
        self.usd["pair"] = ""
        self.usd["price_digits"] = 4.0
        self.usd["contract_size"] = "1000"
        self.assert_rejected(
            "markets.USDTRY.key",
            "markets.USDTRY.pair",
            "markets.USDTRY.price_digits",
            "markets.USDTRY.contract_size",
        )

    def test_spot_needs_a_positive_last(self):
        for last in (0, -49.2, None, "49.19"):
            with self.subTest(last=last):
                self.usd["spot"]["last"] = last
                self.assert_rejected("markets.USDTRY.spot.last")

    def test_spot_trend_needs_a_positive_daily_factor(self):
        for trend in ({"daily_factor": None}, {"daily_factor": -1.0}, {}):
            with self.subTest(trend=trend):
                self.usd["spot_trend"] = trend
                self.assert_rejected("markets.USDTRY.spot_trend.daily_factor")
        self.usd["spot_trend"] = "1.0005"
        self.assert_rejected("markets.USDTRY.spot_trend")

    def test_missing_contract_key(self):
        del self.front["days_to_maturity"]
        del self.front["status"]
        error = self.assert_rejected(
            "markets.USDTRY.contracts[0].days_to_maturity",
            "markets.USDTRY.contracts[0].status",
        )
        self.assertIn("is missing", str(error))

    def test_string_in_numeric_field(self):
        self.front["bid"] = "50.082"
        self.assert_rejected("markets.USDTRY.contracts[0].bid")

    def test_nan_infinity_and_bool_in_numeric_field(self):
        self.front["last"] = float("nan")
        self.front["volume"] = float("inf")
        self.front["premium_percent"] = True
        self.usd["spot"]["high"] = float("-inf")
        self.assert_rejected(
            "markets.USDTRY.contracts[0].last",
            "markets.USDTRY.contracts[0].volume",
            "markets.USDTRY.contracts[0].premium_percent",
            "markets.USDTRY.spot.high",
        )

    def test_nan_parsed_from_json_is_rejected(self):
        raw = FIXTURE.read_text(encoding="utf-8").replace(
            '"ask": 50.083', '"ask": NaN', 1
        )
        self.snapshot = json.loads(raw)
        self.assert_rejected("markets.USDTRY.contracts[0].ask")

    def test_negative_or_fractional_days_to_maturity(self):
        for days in (-1, 2.5, "23", False):
            with self.subTest(days=days):
                self.front["days_to_maturity"] = days
                self.assert_rejected("markets.USDTRY.contracts[0].days_to_maturity")

    def test_markup_in_rendered_contract_strings(self):
        markup = '<img src=x onerror="window.__xss=1">'
        for field in ("symbol", "code", "label"):
            with self.subTest(field=field):
                self.setUp()
                self.front[field] = self.front[field] + markup
                error = self.assert_rejected(f"markets.USDTRY.contracts[0].{field}")
                self.assertIn("must match", str(error))

    def test_contract_strings_in_the_wrong_format(self):
        cases = {
            "symbol": ("usdtryv2026", "USDTRYA2026", "USDTRYV26", "USDTRYV2026\n"),
            "code": ("USDTRY1026", "F_USDTRY1326", "F_USDTRY0026", "f_usdtry1026"),
            "label": ("October 2026", "oct 2026", "Oct 26", "2026-10"),
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.setUp()
                    self.front[field] = value
                    self.assert_rejected(f"markets.USDTRY.contracts[0].{field}")

    def test_source_stays_free_form(self):
        self.front["source"] = "İş Yatırım via borsapy <table>"
        validate_snapshot(self.snapshot)

    def test_non_string_contract_strings_are_still_rejected(self):
        self.front["label"] = ""
        self.front["code"] = None
        error = self.assert_rejected(
            "markets.USDTRY.contracts[0].label",
            "markets.USDTRY.contracts[0].code",
        )
        self.assertIn("non-empty string", str(error))

    def test_unsorted_contracts(self):
        contracts = self.usd["contracts"]
        contracts[0], contracts[1] = contracts[1], contracts[0]
        error = self.assert_rejected("markets.USDTRY.contracts[1].maturity_date")
        self.assertIn("sorted", str(error))

    def test_duplicate_contract_symbol(self):
        self.usd["contracts"].insert(1, copy.deepcopy(self.front))
        self.assert_rejected("markets.USDTRY.contracts[1].symbol")

    def test_status_inconsistent_with_last(self):
        self.front["status"] = "unavailable"
        second = self.usd["contracts"][1]
        second["last"] = None
        third = self.usd["contracts"][2]
        third["status"] = "halted"
        self.assert_rejected(
            "markets.USDTRY.contracts[0].status",
            "markets.USDTRY.contracts[1].status",
            "markets.USDTRY.contracts[2].status",
        )

    def test_market_with_no_priced_contract(self):
        for contract in self.snapshot["markets"]["XAUTRY"]["contracts"]:
            contract["last"] = None
            contract["status"] = "unavailable"
        error = self.assert_rejected("markets.XAUTRY.contracts")
        self.assertIn("priced", str(error))

    def test_market_with_no_contracts(self):
        self.snapshot["markets"]["EURTRY"]["contracts"] = []
        self.assert_rejected("markets.EURTRY.contracts")

    def test_reports_every_problem_at_once(self):
        self.snapshot["schema_version"] = 5
        self.front["bid"] = "50.082"
        self.snapshot["markets"]["EURTRY"]["contracts"][0]["days_to_maturity"] = -3
        error = self.assert_rejected(
            "schema_version",
            "markets.USDTRY.contracts[0].bid",
            "markets.EURTRY.contracts[0].days_to_maturity",
        )
        self.assertIn("3 problems", str(error))

    def test_rejects_non_object(self):
        for value in ([], None, "snapshot"):
            with self.subTest(value=value):
                self.snapshot = value
                self.assert_rejected("$")


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *paths):
        return subprocess.run(
            [sys.executable, "-m", "scripts.snapshot_schema", *map(str, paths)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_accepts_valid_snapshots(self):
        result = self.run_cli(COMMITTED, FIXTURE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("valid schema version 4"), 2)

    def test_rejects_an_invalid_snapshot(self):
        snapshot = load(FIXTURE)
        snapshot["schema_version"] = 5
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text(json.dumps(snapshot), encoding="utf-8")
            result = self.run_cli(FIXTURE, broken)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{broken}: Invalid market snapshot", result.stdout)
        self.assertIn("schema_version: must be 4, got 5", result.stdout)

    def test_reports_markup_in_a_label_by_path(self):
        snapshot = load(FIXTURE)
        snapshot["markets"]["EURTRY"]["contracts"][1]["label"] = "<b>Nov</b> 2026"
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text(json.dumps(snapshot), encoding="utf-8")
            result = self.run_cli(broken)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "markets.EURTRY.contracts[1].label: must match", result.stdout
        )
        self.assertIn("'<b>Nov</b> 2026'", result.stdout)


if __name__ == "__main__":
    unittest.main()
