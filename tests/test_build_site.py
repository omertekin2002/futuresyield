from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "_site" / "data" / "history"
INDEX = ROOT / "_site" / "index.html"
CLOSE = (ROOT / "tests" / "fixtures" / "market.json").read_text(encoding="utf-8")


def build(history_dir, check=True):
    env = {**os.environ, "HISTORY_DIR": str(history_dir)}
    return subprocess.run(
        ["bash", "scripts/build_site.sh"],
        cwd=ROOT,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


class HeadElements(HTMLParser):
    """Each start tag in <head>, in order, with its attributes."""

    def __init__(self):
        super().__init__()
        self.elements = []
        self.in_head = False

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True
        elif self.in_head:
            self.elements.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


def head_elements(html):
    parser = HeadElements()
    parser.feed(html)
    return parser.elements


def policy_directives(content):
    """A Content-Security-Policy value as {directive: [sources]}."""
    directives = {}
    for part in content.split(";"):
        name, *sources = part.split()
        if name:
            directives[name.lower()] = sources
    return directives


class BuildSiteContentSecurityPolicyTests(unittest.TestCase):
    def built_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp) / "missing")
        elements = head_elements(INDEX.read_text(encoding="utf-8"))
        policies = [
            attrs for tag, attrs in elements
            if tag == "meta"
            and (attrs.get("http-equiv") or "").lower() == "content-security-policy"
        ]
        self.assertEqual(len(policies), 1, "index.html must carry exactly one CSP meta tag")
        return elements, policy_directives(policies[0].get("content") or "")

    def test_policy_is_the_first_element_after_charset(self):
        # A meta policy covers only what comes after it.
        elements, _ = self.built_policy()
        self.assertEqual(elements[0], ("meta", {"charset": "utf-8"}))
        tag, attrs = elements[1]
        self.assertEqual(tag, "meta")
        self.assertEqual(attrs.get("http-equiv"), "Content-Security-Policy")

    def test_scripts_come_only_from_the_page_origin(self):
        _, directives = self.built_policy()
        self.assertEqual(directives.get("script-src"), ["'self'"])
        for source in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "data:", "*"):
            self.assertNotIn(source, directives["script-src"])

    def test_everything_else_is_refused_or_same_origin(self):
        _, directives = self.built_policy()
        self.assertEqual(directives, {
            "default-src": ["'none'"],
            "script-src": ["'self'"],
            "style-src": ["'self'"],
            "img-src": ["'self'"],
            "connect-src": ["'self'"],
            "base-uri": ["'none'"],
            "form-action": ["'none'"],
            "object-src": ["'none'"],
        })


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
            (source / "2026-10-07.json").write_text(CLOSE, encoding="utf-8")
            (source / "index.json.tmp").write_text("partial", encoding="utf-8")
            (source / "notes.txt").write_text("not site data", encoding="utf-8")
            (source / "nested").mkdir()
            (source / "nested" / "2026-10-08.json").write_text("{}", encoding="utf-8")
            build(source)
        self.assertEqual(
            sorted(path.name for path in HISTORY.iterdir()),
            ["2026-10-07.json", "index.json", "markets"],
        )
        self.assertEqual(
            json.loads((HISTORY / "index.json").read_text(encoding="utf-8")),
            manifest,
        )
        self.assertEqual((HISTORY / "2026-10-07.json").read_text(encoding="utf-8"), CLOSE)

    def test_builds_one_history_bundle_per_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            manifest = {"schema_version": 1, "days": [{"date": "2026-10-07"}]}
            (source / "index.json").write_text(json.dumps(manifest), encoding="utf-8")
            (source / "2026-10-07.json").write_text(CLOSE, encoding="utf-8")
            build(source)
        close = json.loads(CLOSE)
        self.assertEqual(
            sorted(path.name for path in (HISTORY / "markets").iterdir()),
            sorted(f"{key}.json" for key in close["market_order"]),
        )
        for key in close["market_order"]:
            bundle = json.loads((HISTORY / "markets" / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(
                bundle["days"],
                [{
                    "date": "2026-10-07",
                    "schema_version": close["schema_version"],
                    "generated_at": close["generated_at"],
                    "market_date": close["market_date"],
                    "market": close["markets"][key],
                }],
            )

    def test_fails_when_the_index_lists_a_missing_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            manifest = {"schema_version": 1, "days": [{"date": "2026-10-07"}]}
            (source / "index.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = build(source, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Archived close 2026-10-07 is missing", result.stderr)

    def test_fails_when_the_index_lists_an_invalid_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            manifest = {"schema_version": 1, "days": [{"date": "2026-10-07"}]}
            (source / "index.json").write_text(json.dumps(manifest), encoding="utf-8")
            (source / "2026-10-07.json").write_text("{", encoding="utf-8")
            result = build(source, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Archived close 2026-10-07 could not be read", result.stderr)


if __name__ == "__main__":
    unittest.main()
