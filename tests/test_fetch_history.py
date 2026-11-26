"""End-to-end tests for scripts/fetch_history.sh against a local origin.

As in test_archive_history.py, each test copies scripts/ into a fresh temporary
git repo whose `origin` is a local bare repo, the way the Pages build job and
CI's smoke and archive jobs run it from a fresh checkout. Nothing reads or
writes this repository's git state, and nothing uses the network.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BRANCH = "data-history"
MANIFEST = {"schema_version": 1, "days": [{"date": "2026-10-07"}]}

# The step pages.yml and ci.yml share: the history directory goes to a step
# output, and a failed fetch fails the step (Actions runs bash with -e).
STEP = 'dir=$(bash scripts/fetch_history.sh "$DEST")\necho "dir=$dir"\n'


def tools_available():
    return shutil.which("git") is not None and shutil.which("bash") is not None


class FetchHistoryScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not tools_available():
            raise unittest.SkipTest("fetch_history.sh needs bash and git")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        self.env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="test",
            GIT_AUTHOR_EMAIL="test@example.invalid",
            GIT_COMMITTER_NAME="test",
            GIT_COMMITTER_EMAIL="test@example.invalid",
        )
        self.origin = self.root / "origin.git"
        self.git("init", "--quiet", "--bare", str(self.origin))
        self.dest = self.root / "runner-temp" / "data-history"
        self.runs = 0

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True
        )
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()

    def publish_history(self):
        """Push a data-history branch holding history/ to origin."""
        source = self.root / "history-source"
        (source / "history").mkdir(parents=True)
        (source / "history" / "index.json").write_text(
            json.dumps(MANIFEST), encoding="utf-8"
        )
        (source / "history" / "2026-10-07.json").write_text("{}", encoding="utf-8")
        self.git("init", "--quiet", "-b", BRANCH, str(source))
        self.git("add", "history", cwd=source)
        self.git("commit", "--quiet", "-m", "history", cwd=source)
        self.git("push", "--quiet", str(self.origin), f"HEAD:refs/heads/{BRANCH}", cwd=source)

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
            ["bash", str(repo / "scripts" / "fetch_history.sh"), *map(str, args)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def run_step(self, repo):
        """Run the workflows' step as Actions does: bash -e -o pipefail."""
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", STEP],
            cwd=repo,
            env={**self.env, "DEST": str(self.dest)},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def assert_succeeded(self, result):
        self.assertEqual(
            result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_existing_branch_is_checked_out_with_its_history(self):
        """With data-history on origin, the script checks it out at DEST and
        prints only DEST/history, which holds the branch's manifest."""
        self.publish_history()

        result = self.run_script(self.dest)

        self.assert_succeeded(result)
        self.assertEqual(result.stdout, f"{self.dest}/history\n")
        manifest = self.dest / "history" / "index.json"
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), MANIFEST)
        self.assertTrue((self.dest / "history" / "2026-10-07.json").is_file())

    def test_missing_branch_succeeds_without_history(self):
        """Without data-history on origin (ls-remote exits 2), the script
        succeeds, prints nothing on stdout and creates nothing at DEST."""
        result = self.run_script(self.dest)

        self.assert_succeeded(result)
        self.assertEqual(result.stdout, "")
        self.assertIn("No data-history branch", result.stderr)
        self.assertFalse(self.dest.exists())

    def test_unreachable_origin_fails(self):
        """If origin cannot be listed, the script exits non-zero instead of
        treating the branch as missing, and creates nothing at DEST."""
        missing = self.root / "no-such-origin.git"
        repo = self.checkout(origin=missing)

        result = self.run_script(self.dest, repo=repo)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.returncode, 2)
        self.assertIn("Could not list origin's branches", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.dest.exists())

    def test_wrong_argument_count_prints_usage(self):
        for args in ([], ["one", "two"]):
            with self.subTest(args=args):
                result = self.run_script(*args)

                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage:", result.stderr)

    def test_workflow_step_outputs_history_dir(self):
        """The step in the workflows sets dir= to the history directory when
        the branch exists."""
        self.publish_history()
        result = self.run_step(self.checkout())
        self.assert_succeeded(result)
        self.assertEqual(result.stdout, f"dir={self.dest}/history\n")

    def test_workflow_step_without_branch_outputs_empty_dir(self):
        """Without the branch the step succeeds with an empty dir=, which the
        smoke and audit jobs read as "no archive to check"."""
        result = self.run_step(self.checkout())
        self.assert_succeeded(result)
        self.assertEqual(result.stdout, "dir=\n")

    def test_workflow_step_fails_on_unreachable_origin(self):
        """Under bash -e the failed command substitution fails the step before
        any dir= is written."""
        result = self.run_step(self.checkout(origin=self.root / "no-such-origin.git"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("dir=", result.stdout)


if __name__ == "__main__":
    unittest.main()
