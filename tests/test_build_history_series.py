import copy
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

from scripts import build_history_series as series


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "market.json").read_text(encoding="utf-8"))


def close(day, *, generated_at=None, market_date=None, drop=()):
    """A test close for day from the fixture, without the markets in drop."""
    snapshot = copy.deepcopy(FIXTURE)
    snapshot["generated_at"] = generated_at or f"{day}T23:20:00+03:00"
    snapshot["market_date"] = market_date or day
    for key in drop:
        del snapshot["markets"][key]
        snapshot["market_order"].remove(key)
    return snapshot


class HistorySeriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, closes, listed=None):
        """Write closes ({day: snapshot}) and an index listing listed (or all)."""
        for day, snapshot in closes.items():
            (self.dir / f"{day}.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        days = sorted(closes) if listed is None else listed
        index = {"schema_version": 1, "days": [{"date": day} for day in days]}
        (self.dir / "index.json").write_text(json.dumps(index), encoding="utf-8")

    def bundle(self, key):
        return json.loads((self.dir / "markets" / f"{key}.json").read_text(encoding="utf-8"))

    def test_one_bundle_per_market_matching_every_close(self):
        closes = {
            "2026-10-26": close(
                "2026-10-26",
                generated_at="2026-10-27T01:10:00+03:00",
                market_date="2026-10-27",
            ),
            "2026-10-27": close("2026-10-27"),
            "2026-10-28": close("2026-10-28", drop=("XAUTRY",)),
        }
        self.write(closes)
        self.assertEqual(series.write_bundles(self.dir), ["USDTRY", "EURTRY", "XAUTRY"])
        self.assertEqual(
            sorted(path.name for path in (self.dir / "markets").iterdir()),
            ["EURTRY.json", "USDTRY.json", "XAUTRY.json"],
        )
        for key in ("USDTRY", "EURTRY", "XAUTRY"):
            bundle = self.bundle(key)
            self.assertEqual(bundle["schema_version"], series.BUNDLE_SCHEMA_VERSION)
            self.assertEqual(bundle["market"], key)
            self.assertEqual([entry["date"] for entry in bundle["days"]], sorted(closes))
            for entry in bundle["days"]:
                source = json.loads(
                    (self.dir / f"{entry['date']}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(entry["schema_version"], source["schema_version"])
                self.assertEqual(entry["generated_at"], source["generated_at"])
                self.assertEqual(entry["market_date"], source["market_date"])
                self.assertEqual(entry["market"], source["markets"].get(key))
        # A close without the market keeps its entry, with no market.
        self.assertIsNone(self.bundle("XAUTRY")["days"][2]["market"])
        # A legacy close keeps the market_date it was published with.
        self.assertEqual(self.bundle("USDTRY")["days"][0]["market_date"], "2026-10-27")

    def test_follows_the_index_order_and_reads_only_listed_closes(self):
        closes = {day: close(day) for day in ("2026-10-26", "2026-10-27", "2026-10-28")}
        self.write(closes, listed=["2026-10-28", "2026-10-26"])
        # An unlisted close is never read, even when it can't be.
        (self.dir / "2026-10-27.json").write_text("not json", encoding="utf-8")
        series.write_bundles(self.dir)
        self.assertEqual(
            [entry["date"] for entry in self.bundle("USDTRY")["days"]],
            ["2026-10-28", "2026-10-26"],
        )

    def test_market_found_only_in_a_later_close_gets_every_day(self):
        self.write({
            "2026-10-26": close("2026-10-26", drop=("XAUTRY",)),
            "2026-10-27": close("2026-10-27"),
        })
        series.write_bundles(self.dir)
        self.assertEqual(
            [entry["market"] is None for entry in self.bundle("XAUTRY")["days"]],
            [True, False],
        )

    def test_no_listed_closes_writes_nothing(self):
        self.write({})
        self.assertEqual(series.write_bundles(self.dir), [])
        self.assertFalse((self.dir / "markets").exists())

    def test_missing_close_fails(self):
        self.write({"2026-10-26": close("2026-10-26")}, listed=["2026-10-26", "2026-10-27"])
        with self.assertRaisesRegex(series.BundleError, r"Archived close 2026-10-27 is missing"):
            series.write_bundles(self.dir)
        self.assertFalse((self.dir / "markets").exists())

    def test_invalid_close_fails(self):
        self.write({"2026-10-26": close("2026-10-26")})
        (self.dir / "2026-10-26.json").write_text('{"markets": ', encoding="utf-8")
        with self.assertRaisesRegex(
            series.BundleError, r"Archived close 2026-10-26 could not be read"
        ):
            series.write_bundles(self.dir)

    def test_close_without_markets_fails(self):
        self.write({"2026-10-26": {"day": 26}})
        with self.assertRaisesRegex(series.BundleError, r"2026-10-26 has no markets object"):
            series.write_bundles(self.dir)

    def test_invalid_index_fails(self):
        for index, message in (
            ('{"days": ', r"History index could not be read"),
            ('{"days": {}}', r"no days list"),
            ('{"days": [{"date": "../market"}]}', r"no valid date"),
            ('{"days": [{"date": "2026-10-26"}, {"date": "2026-10-26"}]}', r"twice"),
        ):
            with self.subTest(index=index):
                (self.dir / "index.json").write_text(index, encoding="utf-8")
                with self.assertRaisesRegex(series.BundleError, message):
                    series.build_bundles(self.dir)

    def test_main_reports_a_failure_by_exit_status(self):
        self.write({}, listed=["2026-10-26"])
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(series.main(["build_history_series.py", str(self.dir)]), 1)
        self.assertIn("History bundles not built: Archived close 2026-10-26 is missing", stderr.getvalue())

    def test_main_names_the_markets_built(self):
        self.write({"2026-10-26": close("2026-10-26")})
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(series.main(["build_history_series.py", str(self.dir)]), 0)
        self.assertIn("USDTRY, EURTRY, XAUTRY", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
