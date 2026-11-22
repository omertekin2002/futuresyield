"""Offline checks that constraints.txt still matches the requirements files.

constraints.txt pins every package requirements.txt and requirements-dev.txt
install, directly or transitively, and every pip install in the workflows
passes it with --constraint. scripts/lock_dependencies.sh regenerates it with
uv, which needs the network, so CI never runs it. These checks catch the
mistakes it would have fixed: a direct pin bumped without re-locking, a line
pip would not read as an exact pin, a package pinned twice for the same
environments, and a lock script that drifted from the command recorded in the
file's header.

Stdlib only, so the checks run before any dependency is installed.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent
CONSTRAINTS = ROOT / "constraints.txt"
REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt")
LOCK_SCRIPT = ROOT / "scripts" / "lock_dependencies.sh"

# An exact pin with an optional environment marker, as uv writes them:
# `numpy==2.2.6 ; python_full_version < '3.11'`. No extras (pip refuses them
# in a constraints file), no ranges, no URLs.
PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9.+!_-]*)"
    r"(?:\s*;\s*(?P<marker>\S.*))?$"
)
UV_COMMAND = re.compile(r"(uv pip compile [^\n]*?-o constraints\.txt)(?![\w.])")


def normalize(name):
    """PEP 503 name normalization: case and runs of -, _ and . don't matter."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_lines(text):
    """Each requirement or pin with its comment stripped, skipping blanks."""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            yield line


def parse_pins(lines):
    """[(normalized name, version, marker or None)] for each exact pin, and the
    lines that aren't one."""
    pins, bad = [], []
    for line in lines:
        match = PIN.match(line)
        if match:
            pins.append((
                normalize(match["name"]),
                match["version"],
                match["marker"].strip() if match["marker"] else None,
            ))
        else:
            bad.append(line)
    return pins, bad


def direct_pins():
    pins = []
    for name in REQUIREMENT_FILES:
        found, bad = parse_pins(
            requirement_lines((ROOT / name).read_text(encoding="utf-8"))
        )
        if bad:
            raise AssertionError(f"{name} has lines that are not exact pins: {bad}")
        pins.extend((name, pin) for pin in found)
    return pins


def constraint_pins():
    return parse_pins(requirement_lines(CONSTRAINTS.read_text(encoding="utf-8")))


def mismatches(direct, constraints):
    """Direct pins constraints.txt lacks, or pins at another version in any
    marker branch, as readable strings."""
    problems = []
    for source, (name, version, _) in direct:
        entries = [(v, m) for n, v, m in constraints if n == name]
        if not entries:
            problems.append(f"{name}=={version} ({source}) is not in constraints.txt")
        for other, marker in entries:
            if other != version:
                where = f" ; {marker}" if marker else ""
                problems.append(
                    f"{name}=={version} ({source}) but constraints.txt has "
                    f"{name}=={other}{where}"
                )
    return problems


class ConstraintsFormatTests(unittest.TestCase):
    def test_every_line_is_an_exact_pin(self):
        pins, bad = constraint_pins()
        self.assertEqual(bad, [], "every line must be name==version [; marker]")
        self.assertGreater(len(pins), 20, "the reader found too few pins")

    def test_no_name_is_pinned_twice_for_the_same_marker(self):
        pins, _ = constraint_pins()
        seen = {}
        for name, version, marker in pins:
            with self.subTest(package=name, marker=marker):
                self.assertNotIn(
                    (name, marker), seen,
                    f"{name} is pinned to both {seen.get((name, marker))} and {version}",
                )
                seen[(name, marker)] = version

    def test_a_package_pinned_more_than_once_has_a_marker_on_each(self):
        """Two versions of one package only make sense for different
        environments; an unmarked line would apply everywhere."""
        pins, _ = constraint_pins()
        counts = {}
        for name, _, _ in pins:
            counts[name] = counts.get(name, 0) + 1
        for name, version, marker in pins:
            if counts[name] > 1:
                with self.subTest(package=name, version=version):
                    self.assertIsNotNone(marker)


class DirectPinTests(unittest.TestCase):
    def test_every_direct_pin_is_locked_at_its_version(self):
        """Bumping a pin in requirements.txt or requirements-dev.txt without
        running scripts/lock_dependencies.sh fails here."""
        pins, _ = constraint_pins()
        direct = direct_pins()
        self.assertTrue(direct, "the reader found no direct pins")
        self.assertEqual(mismatches(direct, pins), [])

    def test_a_bumped_pin_is_caught(self):
        """Guards the check above against passing vacuously."""
        pins, _ = constraint_pins()
        bumped = [
            (source, (name, version + ".1", marker))
            for source, (name, version, marker) in direct_pins()
        ]
        self.assertEqual(len(mismatches(bumped, pins)), len(bumped))
        missing = [("requirements.txt", ("not-a-real-package", "1.0", None))]
        self.assertEqual(len(mismatches(missing, pins)), 1)

    def test_marker_branches_are_all_checked(self):
        pins = [
            ("numpy", "2.2.6", "python_full_version < '3.11'"),
            ("numpy", "2.5.5", "python_full_version >= '3.12'"),
        ]
        direct = [("requirements.txt", ("numpy", "2.2.6", None))]
        self.assertEqual(len(mismatches(direct, pins)), 1)


class LockCommandTests(unittest.TestCase):
    def test_header_records_the_lock_scripts_command(self):
        """constraints.txt's header and scripts/lock_dependencies.sh name the
        same uv command, so the file is what the script regenerates."""
        header = UV_COMMAND.search(CONSTRAINTS.read_text(encoding="utf-8"))
        script = UV_COMMAND.search(LOCK_SCRIPT.read_text(encoding="utf-8"))
        self.assertIsNotNone(header, "constraints.txt has no uv header")
        self.assertIsNotNone(script, "lock_dependencies.sh runs no uv compile")
        self.assertEqual(header.group(1).split(), script.group(1).split())

    def test_lock_covers_both_requirement_files_universally(self):
        command = UV_COMMAND.search(LOCK_SCRIPT.read_text(encoding="utf-8")).group(1)
        for needle in (*REQUIREMENT_FILES, "--universal", "--python-version 3.10"):
            with self.subTest(argument=needle):
                self.assertIn(needle, command)


if __name__ == "__main__":
    unittest.main()
