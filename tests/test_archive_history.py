"""End-to-end tests for scripts/archive_history.sh against a local origin.

Each test copies scripts/ into a fresh temporary git repo whose `origin` is a
local bare repo, much like the Pages workflow's archive job runs it from a fresh
checkout of main. Nothing reads or writes this repository's git state, and
nothing uses the network.
"""

from datetime import date
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.archive_snapshot import parse_generated_at, trading_date


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "market.json"
BRANCH = "data-history"
BRANCH_REF = f"refs/heads/{BRANCH}"

# Dates as in test_archive_snapshot.py. Wednesday 7 Oct 2026 is an ordinary
# trading day; 06:00 the next morning still counts as its close.
DAY_ONE = "2026-10-07"
DAY_ONE_CLOSE = "2026-10-07T23:20:00+03:00"
DAY_ONE_NEXT_MORNING = "2026-10-08T06:00:00+03:00"
DAY_TWO = "2026-10-08"
DAY_TWO_CLOSE = "2026-10-08T23:30:00+03:00"
MID_SESSION = "2026-10-09T14:06:12+03:00"

# `git worktree add --orphan` arrived in git 2.42.
MINIMUM_GIT = (2, 42)


def git_version():
    if shutil.which("git") is None or shutil.which("bash") is None:
        return None
    output = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, check=True
    ).stdout
    match = re.search(r"(\d+)\.(\d+)", output)
    return (int(match.group(1)), int(match.group(2))) if match else None


def snapshot_bytes(generated_at):
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["generated_at"] = generated_at
    return (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class ArchiveHistoryScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        version = git_version()
        if version is None or version < MINIMUM_GIT:
            raise unittest.SkipTest(
                "archive_history.sh needs bash and git 2.42 or later "
                f"(found git {version})"
            )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = self.make_env()
        self.origin = self.root / "origin.git"
        self.git("init", "--quiet", "--bare", str(self.origin))
        # Any force-push that rewrites the branch is refused by the remote.
        self.git("config", "receive.denyNonFastForwards", "true", cwd=self.origin)
        self.runs = 0
        self.archives = 0

    def make_env(self):
        """A hermetic environment: no user or system git config, and the
        script's `python3` is this interpreter, which has `holidays`."""
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        shim = self.root / "bin"
        shim.mkdir()
        python3 = shim / "python3"
        python3.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
        )
        python3.chmod(0o755)
        env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', os.defpath)}"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        # As the workflow sets them for the archive job.
        env["GIT_AUTHOR_NAME"] = "github-actions[bot]"
        env["GIT_AUTHOR_EMAIL"] = "github-actions[bot]@users.noreply.github.com"
        env["GIT_COMMITTER_NAME"] = "github-actions[bot]"
        env["GIT_COMMITTER_EMAIL"] = "github-actions[bot]@users.noreply.github.com"
        return env

    def git(self, *args, cwd=None, check=True):
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True
        )
        if check and result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()

    def checkout(self, origin=None):
        """A fresh repo holding a copy of scripts/, with `origin` as its remote."""
        self.runs += 1
        repo = self.root / f"checkout-{self.runs}"
        shutil.copytree(
            SCRIPTS, repo / "scripts",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        self.git("init", "--quiet", "-b", "main", str(repo))
        self.git("add", "scripts", cwd=repo)
        self.git("commit", "--quiet", "-m", "main", cwd=repo)
        self.git("remote", "add", "origin", str(origin or self.origin), cwd=repo)
        return repo

    def run_script(self, *args, repo=None):
        repo = repo or self.checkout()
        return subprocess.run(
            ["bash", str(repo / "scripts" / "archive_history.sh"), *map(str, args)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def archive(self, generated_at, content=None, repo=None):
        """Run the script on a snapshot with a worktree inside the temp dir."""
        self.archives += 1
        snapshot = self.root / f"snapshot-{self.archives}.json"
        snapshot.write_bytes(
            content if content is not None else snapshot_bytes(generated_at)
        )
        self.worktree = self.root / f"worktree-{self.archives}"
        return self.run_script(snapshot, self.worktree, repo=repo)

    def assert_succeeded(self, result):
        self.assertEqual(
            result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def origin_tip(self):
        """Origin's data-history commit, or None if the branch is missing."""
        result = self.git(
            "rev-parse", "--verify", "--quiet", BRANCH_REF, cwd=self.origin, check=False
        )
        return result or None

    def parents(self, commit):
        return self.git("rev-list", "--parents", "-n", "1", commit, cwd=self.origin).split()[1:]

    def origin_files(self, commit):
        return sorted(
            self.git("ls-tree", "-r", "--name-only", commit, cwd=self.origin).splitlines()
        )

    def origin_bytes(self, commit, path):
        return subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=self.origin, env=self.env, capture_output=True, check=True,
        ).stdout

    def origin_manifest(self, commit):
        return json.loads(self.origin_bytes(commit, "history/index.json"))

    def subject(self, commit):
        return self.git("log", "-1", "--format=%s", commit, cwd=self.origin)

    def archive_first_day(self):
        result = self.archive(DAY_ONE_CLOSE)
        self.assert_succeeded(result)
        tip = self.origin_tip()
        self.assertIsNotNone(tip)
        return tip

    def test_creates_orphan_branch_on_first_archive(self):
        """Without data-history on origin, the script pushes a new orphan
        branch holding only the day file and the manifest."""
        self.assertIsNone(self.origin_tip())

        result = self.archive(DAY_ONE_CLOSE)

        self.assert_succeeded(result)
        self.assertIn(f"Creating orphan branch {BRANCH}", result.stdout)
        tip = self.origin_tip()
        self.assertIsNotNone(tip)
        self.assertEqual(self.parents(tip), [], "the branch must start as an orphan")
        self.assertEqual(
            self.origin_files(tip), [f"history/{DAY_ONE}.json", "history/index.json"]
        )
        self.assertEqual(
            self.subject(tip), f"data: archive end-of-day snapshot {DAY_ONE}"
        )
        self.assertEqual(
            self.origin_bytes(tip, f"history/{DAY_ONE}.json"),
            snapshot_bytes(DAY_ONE_CLOSE),
        )
        self.assertEqual(
            [entry["date"] for entry in self.origin_manifest(tip)["days"]], [DAY_ONE]
        )

    def test_new_day_fast_forwards_existing_branch(self):
        """With data-history on origin, a new day adds exactly one commit on
        top of origin's tip and leaves earlier days byte-for-byte unchanged."""
        old_tip = self.archive_first_day()
        first_day = self.origin_bytes(old_tip, f"history/{DAY_ONE}.json")

        result = self.archive(DAY_TWO_CLOSE)

        self.assert_succeeded(result)
        self.assertNotIn("Creating orphan branch", result.stdout)
        tip = self.origin_tip()
        self.assertEqual(self.parents(tip), [old_tip], "push must fast-forward")
        self.assertEqual(
            self.subject(tip), f"data: archive end-of-day snapshot {DAY_TWO}"
        )
        self.assertEqual(
            self.origin_files(tip),
            [f"history/{DAY_ONE}.json", f"history/{DAY_TWO}.json", "history/index.json"],
        )
        self.assertEqual(self.origin_bytes(tip, f"history/{DAY_ONE}.json"), first_day)
        self.assertEqual(
            self.origin_bytes(tip, f"history/{DAY_TWO}.json"),
            snapshot_bytes(DAY_TWO_CLOSE),
        )
        self.assertEqual(
            [entry["date"] for entry in self.origin_manifest(tip)["days"]],
            [DAY_ONE, DAY_TWO],
        )

    def test_repeat_run_for_same_day_commits_nothing(self):
        """Archiving the same snapshot again is a no-op: exit 0, a log line,
        and origin's tip does not move."""
        tip = self.archive_first_day()

        result = self.archive(DAY_ONE_CLOSE)

        self.assert_succeeded(result)
        self.assertIn("No history changes to commit", result.stdout)
        self.assertEqual(self.origin_tip(), tip)

    def test_archived_day_is_never_overwritten(self):
        """A different snapshot for a day already on origin leaves that day's
        file with its original bytes: write-once through the whole script."""
        tip = self.archive_first_day()
        original = self.origin_bytes(tip, f"history/{DAY_ONE}.json")
        later = snapshot_bytes(DAY_ONE_NEXT_MORNING)
        self.assertNotEqual(later, original)

        result = self.archive(DAY_ONE_NEXT_MORNING)

        self.assert_succeeded(result)
        self.assertIn("already archived", result.stdout)
        self.assertIn("No history changes to commit", result.stdout)
        self.assertEqual(self.origin_tip(), tip)
        self.assertEqual(
            self.origin_bytes(self.origin_tip(), f"history/{DAY_ONE}.json"), original
        )

    def test_mid_session_snapshot_commits_nothing(self):
        """A snapshot taken while a session is open is not a close: exit 0 and
        no new commit on origin."""
        tip = self.archive_first_day()

        result = self.archive(MID_SESSION)

        self.assert_succeeded(result)
        self.assertIn("Skipped", result.stdout)
        self.assertIn("No history changes to commit", result.stdout)
        self.assertEqual(self.origin_tip(), tip)

    def test_invalid_snapshot_fails_without_commit(self):
        """A snapshot that fails validate_snapshot makes the script exit
        non-zero before committing, and origin is unchanged."""
        tip = self.archive_first_day()
        snapshot = json.loads(snapshot_bytes(DAY_TWO_CLOSE))
        snapshot["schema_version"] = 3
        broken = json.dumps(snapshot).encode("utf-8")

        result = self.archive(DAY_TWO_CLOSE, content=broken)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version", result.stderr)
        self.assertNotIn("Pushed", result.stdout)
        self.assertEqual(self.origin_tip(), tip)
        self.assertEqual(
            self.git("rev-parse", "HEAD", cwd=self.worktree), tip,
            "nothing is committed on top of the fetched tip",
        )

    def test_unreachable_origin_fails_without_creating_branch(self):
        """If origin cannot be listed, the script exits non-zero instead of
        treating the branch as missing and creating an orphan."""
        missing = self.root / "no-such-origin.git"
        repo = self.checkout(origin=missing)

        result = self.archive(DAY_ONE_CLOSE, repo=repo)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.returncode, 2)
        self.assertIn("Could not list origin's branches", result.stderr)
        self.assertNotIn("Creating orphan branch", result.stdout)
        self.assertEqual(
            self.git("branch", "--list", BRANCH, cwd=repo), "",
            "no local data-history branch is created",
        )
        self.assertFalse(missing.exists())
        self.assertFalse(self.worktree.exists())

    def test_wrong_argument_count_prints_usage(self):
        """With no arguments or more than two, the script exits 2 with its
        usage line and touches nothing."""
        for args in ([], ["a.json", "worktree", "extra"]):
            with self.subTest(args=args):
                result = self.run_script(*args)

                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage:", result.stderr)
                self.assertIn("SNAPSHOT [WORKTREE]", result.stderr)
        self.assertIsNone(self.origin_tip())

    def test_concurrent_update_is_never_overwritten(self):
        """If origin's branch moves between the script's fetch and its push,
        the push is rejected and the other commit survives: no force-push.
        This origin accepts non-fast-forward updates, so only the script's own
        refusal to force keeps the other commit."""
        lenient = self.root / "lenient.git"
        self.git("init", "--quiet", "--bare", str(lenient))
        first = self.archive(DAY_ONE_CLOSE, repo=self.checkout(origin=lenient))
        self.assert_succeeded(first)
        # Another writer prepares a commit on top of origin's current tip.
        other = self.root / "other"
        self.git("clone", "--quiet", "--branch", BRANCH, str(lenient), str(other))
        (other / "history" / "note.txt").write_text("other writer\n", encoding="utf-8")
        self.git("add", "history/note.txt", cwd=other)
        self.git("commit", "--quiet", "-m", "other writer", cwd=other)
        racing = self.git("rev-parse", "HEAD", cwd=other)
        # It lands right after the script commits and before the script pushes.
        repo = self.checkout(origin=lenient)
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\n"
            f'exec git -C "{other}" push --quiet origin HEAD:{BRANCH_REF}\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)

        result = self.archive(DAY_TWO_CLOSE, repo=repo)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.git("rev-parse", BRANCH_REF, cwd=lenient), racing,
            "the other writer's commit must survive",
        )

    def test_dates_match_the_end_of_day_rule(self):
        """Guards the fixtures above: the closes count as the days the tests
        expect, and the mid-session time is not a close."""
        def day(value):
            return trading_date(parse_generated_at(value))

        self.assertEqual(day(DAY_ONE_CLOSE), date.fromisoformat(DAY_ONE))
        self.assertEqual(day(DAY_ONE_NEXT_MORNING), date.fromisoformat(DAY_ONE))
        self.assertEqual(day(DAY_TWO_CLOSE), date.fromisoformat(DAY_TWO))
        self.assertIsNone(day(MID_SESSION))


if __name__ == "__main__":
    unittest.main()
