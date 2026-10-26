import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "_site" / "data" / "history"


def build(history_dir):
    env = {**os.environ, "HISTORY_DIR": str(history_dir)}
    subprocess.run(
        ["bash", "scripts/build_site.sh"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


class BuildSiteHistoryTests(unittest.TestCase):
    def test_without_history_ships_an_empty_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp) / "missing")
        self.assertEqual(sorted(path.name for path in HISTORY.iterdir()), ["index.json"])
        manifest = json.loads((HISTORY / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["days"], [])

    def test_directory_without_manifest_counts_as_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "2026-10-07.json").write_text("{}", encoding="utf-8")
            build(tmp)
        self.assertEqual(sorted(path.name for path in HISTORY.iterdir()), ["index.json"])

    def test_copies_manifest_and_day_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            manifest = {"schema_version": 1, "days": [{"date": "2026-10-07"}]}
            (source / "index.json").write_text(json.dumps(manifest), encoding="utf-8")
            (source / "2026-10-07.json").write_text('{"day": 7}', encoding="utf-8")
            (source / "index.json.tmp").write_text("partial", encoding="utf-8")
            (source / "notes.txt").write_text("not site data", encoding="utf-8")
            (source / "nested").mkdir()
            (source / "nested" / "2026-10-08.json").write_text("{}", encoding="utf-8")
            build(source)
        self.assertEqual(
            sorted(path.name for path in HISTORY.iterdir()),
            ["2026-10-07.json", "index.json"],
        )
        self.assertEqual(
            json.loads((HISTORY / "index.json").read_text(encoding="utf-8")),
            manifest,
        )


if __name__ == "__main__":
    unittest.main()
