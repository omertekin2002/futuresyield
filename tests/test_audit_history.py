import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.archive_snapshot import update_manifest
from scripts.audit_history import audit, main


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "market.json"
# Three ordinary trading days, each archived from a snapshot taken after its
# close (23:15 Istanbul time or later, or before the next session opens).
CLOSES = {
    "2026-10-05": "2026-10-05T23:20:00+03:00",
    "2026-10-06": "2026-10-07T00:45:00+03:00",
    "2026-10-07": "2026-10-07T23:30:00+03:00",
}


class AuditHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.history = Path(temporary.name) / "history"
        self.history.mkdir()
        for day, generated_at in CLOSES.items():
            self.write_day(day, generated_at=generated_at)
        update_manifest(self.history)

    def write_day(self, day, **changes):
        snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
        snapshot.update(changes)
        path = self.history / f"{day}.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def read_index(self):
        return json.loads((self.history / "index.json").read_text(encoding="utf-8"))

    def write_index(self, index):
        (self.history / "index.json").write_text(json.dumps(index), encoding="utf-8")

    def problems(self):
        return audit(self.history)[1]

    def assert_problem(self, path, *fragments):
        """Assert that some problem names path, with every fragment in its reason."""
        problems = self.problems()
        for where, reason in problems:
            if where == str(path) and all(f in reason for f in fragments):
                return
        self.fail(f"no problem at {path} with {fragments}; got {problems}")

    def test_clean_archive_passes(self):
        self.assertEqual(audit(self.history), (list(CLOSES), []))

    def test_cli_prints_a_summary_and_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audit_history", str(self.history)],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout,
            f"{self.history}: 3 archived closes (2026-10-05 to 2026-10-07), "
            "index consistent\n",
        )

    def test_cli_lists_each_problem_and_exits_nonzero(self):
        (self.history / "2026-10-07.json.tmp").write_text("{}", encoding="utf-8")
        output = io.StringIO()
        argv = ["audit_history", str(self.history)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output), \
                self.assertRaises(SystemExit) as exit:
            main()
        self.assertEqual(exit.exception.code, 1)
        lines = output.getvalue().splitlines()
        self.assertTrue(lines[0].startswith(f"{self.history / '2026-10-07.json.tmp'}: "))
        self.assertEqual(lines[-1], f"1 problem in {self.history} (3 archived closes)")

    def test_empty_archive_with_its_index_passes(self):
        for day in CLOSES:
            (self.history / f"{day}.json").unlink()
        update_manifest(self.history)
        self.assertEqual(audit(self.history), ([], []))

    def test_missing_directory_is_reported(self):
        missing = self.history.parent / "nowhere"
        self.assertEqual(audit(missing), ([], [(str(missing), "is not a directory")]))

    # Day files

    def test_schema_invalid_day_is_reported_with_its_field(self):
        snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
        snapshot["generated_at"] = CLOSES["2026-10-06"]
        snapshot["markets"]["USDTRY"]["contracts"][0]["last"] = "42.5"
        (self.history / "2026-10-06.json").write_text(json.dumps(snapshot), encoding="utf-8")
        self.assert_problem(
            f"{self.history / '2026-10-06.json'}: markets.USDTRY.contracts[0].last",
            "number",
        )

    def test_unsupported_schema_version_is_reported(self):
        self.write_day("2026-10-06", generated_at=CLOSES["2026-10-06"], schema_version=5)
        self.assert_problem(f"{self.history / '2026-10-06.json'}: schema_version")

    def test_day_file_that_is_not_json_is_reported(self):
        (self.history / "2026-10-06.json").write_text("{not json", encoding="utf-8")
        self.assert_problem(self.history / "2026-10-06.json", "not valid JSON")

    def test_generated_at_for_another_day_is_reported(self):
        # A close of 7 Oct saved under 6 Oct's name, as a renamed file would be.
        self.write_day("2026-10-06", generated_at=CLOSES["2026-10-07"])
        update_manifest(self.history)
        self.assert_problem(
            self.history / "2026-10-06.json", "is the close of 2026-10-07, not 2026-10-06",
        )

    def test_renamed_day_file_is_reported(self):
        (self.history / "2026-10-07.json").rename(self.history / "2026-10-08.json")
        self.assert_problem(self.history / "2026-10-08.json", "not 2026-10-08")
        self.assert_problem(f"{self.history / 'index.json'}: days[2]", "does not exist")
        self.assert_problem(self.history / "index.json", "no entry for")

    def test_mid_session_generated_at_is_reported(self):
        self.write_day("2026-10-06", generated_at="2026-10-06T14:06:12+03:00")
        update_manifest(self.history)
        self.assert_problem(
            self.history / "2026-10-06.json", "during a VİOP session", "not an end-of-day",
        )

    def test_unreadable_generated_at_is_reported_once(self):
        self.write_day("2026-10-06", generated_at="yesterday")
        problems = [p for p in self.problems() if p[0].startswith(
            str(self.history / "2026-10-06.json"))]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("generated_at", problems[0][0])

    # Anything else in the directory

    def test_leftover_tmp_file_is_reported(self):
        (self.history / "2026-10-08.json.tmp").write_text("{}", encoding="utf-8")
        self.assert_problem(self.history / "2026-10-08.json.tmp", "unexpected file")

    def test_index_tmp_file_is_reported(self):
        (self.history / "index.json.tmp").write_text("{}", encoding="utf-8")
        self.assert_problem(self.history / "index.json.tmp", "unexpected file")

    def test_unexpected_names_are_reported(self):
        for name in ("notes.txt", "2026-10-8.json", ".DS_Store"):
            (self.history / name).write_text("", encoding="utf-8")
        (self.history / "2026-10-09.json").mkdir()
        for name in ("notes.txt", "2026-10-8.json", ".DS_Store"):
            self.assert_problem(self.history / name, "unexpected file")
        self.assert_problem(self.history / "2026-10-09.json", "unexpected directory")

    # index.json

    def test_missing_index_is_reported(self):
        (self.history / "index.json").unlink()
        self.assertEqual(self.problems(), [(str(self.history / "index.json"), "is missing")])

    def test_unparseable_index_is_reported(self):
        (self.history / "index.json").write_text('{"days": [', encoding="utf-8")
        self.assert_problem(self.history / "index.json", "not valid JSON")

    def test_index_without_a_days_list_is_reported(self):
        self.write_index({"schema_version": 1})
        self.assert_problem(self.history / "index.json", '"days" list')

    def test_index_missing_an_entry_for_a_file_is_reported(self):
        index = self.read_index()
        del index["days"][1]
        self.write_index(index)
        self.assert_problem(
            self.history / "index.json", f"no entry for {self.history / '2026-10-06.json'}",
        )

    def test_index_entry_without_a_file_is_reported(self):
        (self.history / "2026-10-06.json").unlink()
        self.assert_problem(
            f"{self.history / 'index.json'}: days[1]",
            "lists 2026-10-06", f"{self.history / '2026-10-06.json'} does not exist",
        )

    def test_index_entry_with_another_generated_at_is_reported(self):
        index = self.read_index()
        index["days"][2]["generated_at"] = "2026-10-07T23:45:00+03:00"
        self.write_index(index)
        self.assert_problem(
            f"{self.history / 'index.json'}: days[2].generated_at",
            "2026-10-07T23:45:00+03:00", "2026-10-07T23:30:00+03:00",
        )

    def test_index_entry_with_other_markets_is_reported(self):
        index = self.read_index()
        index["days"][0]["markets"] = ["USDTRY", "EURTRY"]
        self.write_index(index)
        self.assert_problem(f"{self.history / 'index.json'}: days[0].markets")

    def test_index_entry_with_an_extra_key_is_reported(self):
        index = self.read_index()
        index["days"][0]["note"] = "hand edited"
        self.write_index(index)
        self.assert_problem(f"{self.history / 'index.json'}: days[0].note")

    def test_reordered_index_is_reported(self):
        index = self.read_index()
        index["days"].reverse()
        self.write_index(index)
        self.assertEqual(
            self.problems(),
            [(f"{self.history / 'index.json'}: days", "are not sorted by date")],
        )

    def test_duplicate_index_entry_is_reported(self):
        index = self.read_index()
        index["days"].append(index["days"][0])
        self.write_index(index)
        self.assert_problem(f"{self.history / 'index.json'}: days[3]", "more than once")

    def test_index_entry_without_a_date_is_reported(self):
        index = self.read_index()
        index["days"].append({"generated_at": CLOSES["2026-10-07"]})
        self.write_index(index)
        self.assert_problem(f"{self.history / 'index.json'}: days[3]", "has no date")

    def test_index_schema_version_and_extra_keys_are_reported(self):
        index = self.read_index()
        index["schema_version"] = 2
        index["updated"] = "today"
        self.write_index(index)
        self.assert_problem(f"{self.history / 'index.json'}: schema_version", "expected 1")
        self.assert_problem(self.history / "index.json", "unexpected keys ['updated']")


if __name__ == "__main__":
    unittest.main()
