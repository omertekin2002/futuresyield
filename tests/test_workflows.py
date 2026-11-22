"""Structure checks for .github/workflows/pages.yml and ci.yml.

The workflows only run on GitHub, so these tests pin the properties that keep
the live site and its archive safe: every job has a timeout, the end-of-day
archive never waits on the Pages build, permissions stay per job, the Pages
actions run on Node 24, and every data-history fetch goes through
scripts/fetch_history.sh. actionlint (run in CI's static job) checks the
syntax; this checks the design.

The repo's dependencies don't include a YAML parser, so the workflows are read
with a small reader for the block layout they use: jobs at two spaces under
`jobs:`, job keys at four, and a step per `      - ` item.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# The first major of each action that runs on Node 24 rather than Node 20.
NODE24_MAJOR = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "actions/upload-artifact": 6,
    "actions/download-artifact": 7,
    "actions/configure-pages": 6,
    "actions/upload-pages-artifact": 5,  # composite around upload-artifact@v7
    "actions/deploy-pages": 5,
}

FETCH_STEP = 'bash scripts/fetch_history.sh "$RUNNER_TEMP/data-history"'


class Job:
    def __init__(self, name, lines):
        self.name = name
        self.text = "\n".join(lines)
        self.keys = {}
        self.blocks = {}
        current = None
        for line in lines:
            match = re.match(r"^    ([A-Za-z0-9_-]+):(.*)$", line)
            if match:
                current = match.group(1)
                self.keys[current] = match.group(2).strip()
                self.blocks[current] = []
            elif current and line.startswith("      "):
                self.blocks[current].append(line.strip())

    @property
    def needs(self):
        value = self.keys.get("needs", "")
        return {item.strip() for item in value.strip("[]").split(",") if item.strip()}

    @property
    def timeout(self):
        value = self.keys.get("timeout-minutes")
        return int(value) if value and value.isdigit() else None

    @property
    def permissions(self):
        return self.block_fields("permissions")

    def block_fields(self, key):
        fields = {}
        for line in self.blocks.get(key, []):
            field, _, value = line.partition(":")
            fields[field.strip()] = value.strip()
        return fields

    def block_value(self, key, name):
        return self.block_fields(key).get(name)

    @property
    def steps(self):
        """Each step's text, split on its `      - ` item."""
        steps, current = [], None
        for line in self.text.splitlines():
            if line.startswith("      - "):
                current = [line]
                steps.append(current)
            elif current is not None and line.startswith("        "):
                current.append(line)
        return ["\n".join(step) for step in steps]

    @property
    def uses(self):
        return re.findall(r"uses:\s*([^\s#]+)", self.text)


class Workflow:
    def __init__(self, path):
        self.text = path.read_text(encoding="utf-8")
        lines = self.text.splitlines()
        start = lines.index("jobs:")
        self.header = "\n".join(lines[:start])
        self.code = "\n".join(
            line for line in lines if not line.lstrip().startswith("#")
        )
        self.jobs = {}
        name, body = None, []
        for line in lines[start + 1:]:
            match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
            if match:
                if name:
                    self.jobs[name] = Job(name, body)
                name, body = match.group(1), []
            elif name:
                body.append(line)
        if name:
            self.jobs[name] = Job(name, body)

    def ancestors(self, job):
        """Every job `job` waits on, directly or through others."""
        seen, pending = set(), list(self.jobs[job].needs)
        while pending:
            name = pending.pop()
            if name not in seen:
                seen.add(name)
                pending.extend(self.jobs[name].needs)
        return seen


PAGES = Workflow(WORKFLOWS / "pages.yml")
CI = Workflow(WORKFLOWS / "ci.yml")


def steps_matching(workflow, needle):
    return {
        name for name, job in workflow.jobs.items()
        if any(needle in step for step in job.steps)
    }


class ReaderTests(unittest.TestCase):
    def test_reader_finds_every_job(self):
        """Guards the reader itself: a layout change that hides a job from it
        would otherwise make the checks below pass vacuously."""
        self.assertEqual(set(PAGES.jobs), {"refresh", "build", "deploy", "archive"})
        self.assertEqual(set(CI.jobs), {"tests", "static", "smoke", "archive"})
        for workflow in (PAGES, CI):
            for name, job in workflow.jobs.items():
                with self.subTest(job=name):
                    self.assertEqual(job.keys["runs-on"], "ubuntu-latest")
                    self.assertTrue(job.steps, "the reader found no steps")


class TimeoutTests(unittest.TestCase):
    def test_every_job_has_a_timeout(self):
        for workflow, label in ((PAGES, "pages.yml"), (CI, "ci.yml")):
            for name, job in workflow.jobs.items():
                with self.subTest(workflow=label, job=name):
                    self.assertIsNotNone(job.timeout, "missing timeout-minutes")
                    self.assertGreater(job.timeout, 0)
                    self.assertLessEqual(job.timeout, 30)


class PagesGraphTests(unittest.TestCase):
    def test_job_graph(self):
        self.assertEqual(PAGES.jobs["refresh"].needs, set())
        self.assertEqual(PAGES.jobs["build"].needs, {"refresh"})
        self.assertEqual(PAGES.jobs["deploy"].needs, {"build"})
        self.assertEqual(PAGES.jobs["archive"].needs, {"refresh"})

    def test_archive_does_not_wait_on_the_pages_build(self):
        """No job the archive waits on fetches history, builds the site or
        touches Pages, so none of them failing can skip the day's close."""
        ancestors = PAGES.ancestors("archive")
        self.assertEqual(ancestors, {"refresh"})
        for needle in (
            "fetch_history.sh",
            "build_site.sh",
            "actions/configure-pages",
            "actions/upload-pages-artifact",
            "actions/deploy-pages",
        ):
            with self.subTest(step=needle):
                self.assertFalse(steps_matching(PAGES, needle) & (ancestors | {"archive"}))

    def test_refresh_tests_and_uploads_the_snapshot(self):
        """The archive and the site both depend on the job that runs the tests
        and the updater, and take its snapshot as an artifact."""
        refresh = PAGES.jobs["refresh"].steps
        order = [
            next(i for i, step in enumerate(refresh) if needle in step)
            for needle in (
                "python -m unittest discover -s tests",
                "python scripts/update_data.py",
                "actions/upload-artifact",
            )
        ]
        self.assertEqual(order, sorted(order), "tests, then updater, then upload")
        upload = refresh[order[-1]]
        self.assertIn("name: market-snapshot", upload)
        self.assertIn("path: data/market.json", upload)
        for name in ("build", "archive"):
            with self.subTest(job=name):
                downloads = [
                    step for step in PAGES.jobs[name].steps
                    if "actions/download-artifact" in step
                ]
                self.assertEqual(len(downloads), 1)
                self.assertIn("name: market-snapshot", downloads[0])
        # The site is built from this run's snapshot, put where build_site.sh
        # reads it.
        self.assertIn("path: data\n", PAGES.jobs["build"].text + "\n")

    def test_build_fetches_history_before_building_and_uploading(self):
        build = PAGES.jobs["build"].steps
        order = [
            next(i for i, step in enumerate(build) if needle in step)
            for needle in (
                "actions/download-artifact",
                FETCH_STEP,
                "bash scripts/build_site.sh",
                "actions/configure-pages",
                "actions/upload-pages-artifact",
            )
        ]
        self.assertEqual(order, sorted(order))

    def test_concurrency(self):
        """A newer run may cancel an older refresh, build or deploy, but an
        archive push in progress is never cancelled."""
        groups = {
            name: (
                job.block_value("concurrency", "group"),
                job.block_value("concurrency", "cancel-in-progress"),
            )
            for name, job in PAGES.jobs.items()
        }
        self.assertEqual(groups["archive"], ("data-history", "false"))
        for name in ("refresh", "build", "deploy"):
            with self.subTest(job=name):
                self.assertEqual(groups[name][1], "true")
        self.assertEqual(
            len({group for group, _ in groups.values()}), len(groups),
            "each job has its own group",
        )
        self.assertNotIn("concurrency:", PAGES.header)


class PermissionTests(unittest.TestCase):
    def test_workflow_defaults_to_read_only(self):
        for workflow, label in ((PAGES, "pages.yml"), (CI, "ci.yml")):
            with self.subTest(workflow=label):
                self.assertIn("permissions:\n  contents: read", workflow.header)

    def test_write_scopes_are_per_job(self):
        expected = {
            "refresh": {},
            "build": {},
            "deploy": {"pages": "write", "id-token": "write"},
            "archive": {"contents": "write"},
        }
        for name, job in PAGES.jobs.items():
            with self.subTest(job=name):
                self.assertEqual(job.permissions, expected[name])
        for name, job in CI.jobs.items():
            with self.subTest(job=name):
                self.assertEqual(job.permissions, {})


class ActionVersionTests(unittest.TestCase):
    def test_pages_actions_are_current(self):
        uses = set(PAGES.jobs["build"].uses + PAGES.jobs["deploy"].uses)
        self.assertIn("actions/configure-pages@v6", uses)
        self.assertIn("actions/upload-pages-artifact@v5", uses)
        self.assertIn("actions/deploy-pages@v5", uses)

    def test_no_node20_actions(self):
        for workflow, label in ((PAGES, "pages.yml"), (CI, "ci.yml")):
            for job in workflow.jobs.values():
                for action in job.uses:
                    repo, _, ref = action.partition("@")
                    with self.subTest(workflow=label, job=job.name, action=action):
                        self.assertIn(repo, NODE24_MAJOR, "add this action's Node 24 major")
                        match = re.fullmatch(r"v(\d+)", ref)
                        self.assertIsNotNone(match, "pin actions to a major tag")
                        self.assertGreaterEqual(int(match.group(1)), NODE24_MAJOR[repo])


class HistoryFetchTests(unittest.TestCase):
    def test_every_history_fetch_uses_the_shared_script(self):
        """pages.yml and both CI jobs that read the archive call
        fetch_history.sh, so the exit-2 rule cannot drift between copies."""
        self.assertEqual(steps_matching(PAGES, FETCH_STEP), {"build"})
        self.assertEqual(steps_matching(CI, FETCH_STEP), {"smoke", "archive"})
        for workflow in (PAGES, CI):
            self.assertNotIn("ls-remote", workflow.code)
            self.assertNotIn("git worktree", workflow.code)

    def test_ci_fetch_step_writes_the_history_dir_output(self):
        for name in ("smoke", "archive"):
            with self.subTest(job=name):
                step = next(
                    step for step in CI.jobs[name].steps if FETCH_STEP in step
                )
                self.assertIn("id: archive", step)
                self.assertIn(f"dir=$({FETCH_STEP})", step)
                self.assertIn('echo "dir=$dir" >> "$GITHUB_OUTPUT"', step)

    def test_pages_build_reads_the_fetched_history(self):
        self.assertIn(
            "HISTORY_DIR: ${{ runner.temp }}/data-history/history",
            PAGES.jobs["build"].text,
        )


if __name__ == "__main__":
    unittest.main()
