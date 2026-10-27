from datetime import date, datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts.archive_snapshot import archive, build_manifest, trading_date


ISTANBUL = ZoneInfo("Europe/Istanbul")
ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "market.json"


def at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ISTANBUL)


class TradingDateTests(unittest.TestCase):
    # Wednesday 7 Oct 2026 is an ordinary trading day.
    def test_weekday_after_close_is_that_day(self):
        self.assertEqual(trading_date(at(2026, 10, 7, 23, 15)), date(2026, 10, 7))
        self.assertEqual(trading_date(at(2026, 10, 7, 23, 59)), date(2026, 10, 7))

    def test_close_waits_for_delayed_quotes(self):
        self.assertIsNone(trading_date(at(2026, 10, 7, 23, 5)))

    def test_early_morning_before_open_is_previous_day(self):
        self.assertEqual(trading_date(at(2026, 10, 8, 0, 30)), date(2026, 10, 7))
        self.assertEqual(trading_date(at(2026, 10, 8, 9, 19)), date(2026, 10, 7))

    def test_weekend_maps_to_friday(self):
        friday = date(2026, 10, 9)
        self.assertEqual(trading_date(at(2026, 10, 10, 12)), friday)
        self.assertEqual(trading_date(at(2026, 10, 11, 20)), friday)
        self.assertEqual(trading_date(at(2026, 10, 12, 8)), friday)

    def test_public_holiday_maps_to_previous_trading_day(self):
        # Wednesday 15 Jul 2026 is Democracy and National Unity Day.
        self.assertEqual(trading_date(at(2026, 7, 15, 14)), date(2026, 7, 14))
        # Kurban Bayramı runs 27-30 May 2026 after the 26 May half day.
        self.assertEqual(trading_date(at(2026, 6, 1, 7)), date(2026, 5, 26))

    def test_mid_session_is_skipped(self):
        self.assertIsNone(trading_date(at(2026, 10, 7, 9, 20)))
        self.assertIsNone(trading_date(at(2026, 10, 7, 14, 6)))
        # Between the normal and evening sessions the trading day is not over.
        self.assertIsNone(trading_date(at(2026, 10, 7, 18, 30)))

    def test_half_day_closes_at_midday(self):
        # Wednesday 28 Oct 2026 is the half day before Republic Day: one
        # session until 12:40 and no evening session.
        half_day = date(2026, 10, 28)
        self.assertIsNone(trading_date(at(2026, 10, 28, 12, 50)))
        self.assertEqual(trading_date(at(2026, 10, 28, 12, 55)), half_day)
        self.assertEqual(trading_date(at(2026, 10, 29, 15)), half_day)
        self.assertEqual(trading_date(at(2026, 10, 30, 9)), half_day)

    def test_accepts_other_time_zones(self):
        utc = datetime.fromisoformat("2026-10-07T20:20:00+00:00")
        self.assertEqual(trading_date(utc), date(2026, 10, 7))


def snapshot_bytes(generated_at):
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["generated_at"] = generated_at
    return (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "data-history"
        self.history = self.checkout / "history"

    def write_snapshot(self, generated_at, name="market.json"):
        path = self.root / name
        path.write_bytes(snapshot_bytes(generated_at))
        return path

    def manifest(self):
        return json.loads((self.history / "index.json").read_text(encoding="utf-8"))

    def test_archives_end_of_day_snapshot_verbatim(self):
        snapshot = self.write_snapshot("2026-10-07T23:20:00+03:00")

        message = archive(snapshot, self.checkout)

        self.assertIn("Archived end-of-day snapshot 2026-10-07", message)
        archived = self.history / "2026-10-07.json"
        self.assertEqual(archived.read_bytes(), snapshot.read_bytes())
        self.assertEqual(
            self.manifest(),
            {
                "schema_version": 1,
                "days": [
                    {
                        "date": "2026-10-07",
                        "generated_at": "2026-10-07T23:20:00+03:00",
                        "markets": ["USDTRY", "EURTRY", "XAUTRY"],
                    }
                ],
            },
        )
        self.assertEqual(list(self.history.glob("*.tmp")), [])

    def test_never_overwrites_an_archived_day(self):
        first = self.write_snapshot("2026-10-07T23:20:00+03:00", "first.json")
        later = self.write_snapshot("2026-10-08T06:00:00+03:00", "later.json")
        archive(first, self.checkout)
        archived = (self.history / "2026-10-07.json").read_bytes()
        manifest = (self.history / "index.json").read_bytes()

        message = archive(later, self.checkout)

        self.assertIn("already archived", message)
        self.assertEqual((self.history / "2026-10-07.json").read_bytes(), archived)
        self.assertEqual((self.history / "index.json").read_bytes(), manifest)
        self.assertNotIn("updated", message)

    def test_skips_mid_session_snapshot(self):
        snapshot = self.write_snapshot("2026-10-07T14:06:12+03:00")

        message = archive(snapshot, self.checkout)

        self.assertIn("Skipped", message)
        self.assertEqual(sorted(p.name for p in self.history.iterdir()), ["index.json"])
        self.assertEqual(self.manifest()["days"], [])

    def test_manifest_is_sorted_deduplicated_and_rebuilt_from_files(self):
        self.history.mkdir(parents=True)
        for day in ("2026-10-09", "2026-10-05"):
            (self.history / f"{day}.json").write_bytes(
                snapshot_bytes(f"{day}T23:30:00+03:00")
            )
        (self.history / "index.json").write_text(
            json.dumps({"schema_version": 1, "days": [{"date": "2026-10-09"}] * 2}),
            encoding="utf-8",
        )
        (self.history / "notes.txt").write_text("not a day", encoding="utf-8")
        snapshot = self.write_snapshot("2026-10-08T00:10:00+03:00")

        message = archive(snapshot, self.checkout)

        self.assertIn("updated history/index.json", message)
        manifest = self.manifest()
        dates = [entry["date"] for entry in manifest["days"]]
        self.assertEqual(dates, ["2026-10-05", "2026-10-07", "2026-10-09"])
        self.assertEqual(manifest, build_manifest(self.history))

    def test_rejects_snapshot_without_markets(self):
        snapshot = self.root / "broken.json"
        snapshot.write_text(
            json.dumps({"generated_at": "2026-10-07T23:20:00+03:00", "markets": {}}),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            archive(snapshot, self.checkout)

    def test_rejects_invalid_snapshot_without_writing(self):
        good = self.write_snapshot("2026-10-07T23:20:00+03:00", "good.json")
        archive(good, self.checkout)
        before = {path.name: path.read_bytes() for path in self.history.iterdir()}
        snapshot = json.loads(snapshot_bytes("2026-10-08T23:20:00+03:00"))
        snapshot["markets"]["EURTRY"]["contracts"][0]["last"] = "55.1"
        broken = self.root / "broken.json"
        broken.write_text(json.dumps(snapshot), encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            archive(broken, self.checkout)

        self.assertIn("markets.EURTRY.contracts[0].last", str(caught.exception))
        after = {path.name: path.read_bytes() for path in self.history.iterdir()}
        self.assertEqual(after, before)

    def test_invalid_snapshot_creates_no_history(self):
        snapshot = json.loads(snapshot_bytes("2026-10-07T23:20:00+03:00"))
        snapshot["schema_version"] = 3
        broken = self.root / "broken.json"
        broken.write_text(json.dumps(snapshot), encoding="utf-8")

        with self.assertRaises(ValueError):
            archive(broken, self.checkout)

        self.assertFalse(self.checkout.exists())

    def test_command_line_rejects_invalid_snapshot(self):
        snapshot = json.loads(snapshot_bytes("2026-10-07T23:20:00+03:00"))
        snapshot["schema_version"] = 3
        broken = self.root / "broken.json"
        broken.write_text(json.dumps(snapshot), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "archive_snapshot.py"),
             str(broken), str(self.checkout)],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version", result.stderr)
        self.assertFalse(self.checkout.exists())

    def test_command_line_exits_zero_and_logs(self):
        snapshot = self.write_snapshot("2026-10-10T10:00:00+03:00")
        command = [sys.executable, str(ROOT / "scripts" / "archive_snapshot.py")]

        first = subprocess.run(
            [*command, str(snapshot), str(self.checkout)],
            capture_output=True,
            check=True,
            text=True,
        )
        second = subprocess.run(
            [*command, str(snapshot), str(self.checkout)],
            capture_output=True,
            check=True,
            text=True,
        )

        self.assertIn("Archived end-of-day snapshot 2026-10-09", first.stdout)
        self.assertIn("already archived", second.stdout)


if __name__ == "__main__":
    unittest.main()
