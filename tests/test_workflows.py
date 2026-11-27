"""Structure checks for .github/workflows/pages.yml and ci.yml.

The workflows only run on GitHub, so these tests pin the properties that keep
the live site and its archive safe: every job has a timeout, the end-of-day
archive never waits on the Pages build, permissions stay per job, the Pages
actions run on Node 24, and every data-history fetch goes through
scripts/fetch_history.sh, and every pip install is held to constraints.txt.
CI runs on every push, main included, and once a weekday after the end-of-day
archive, and never gets a write scope. actionlint (run in CI's static job)
checks the syntax; this checks the design.

The repo's dependencies don't include a YAML parser, so the workflows are read
with a small reader for the block layout they use: triggers at two spaces under
`on:`, jobs at two spaces under `jobs:`, job keys at four, and a step per
`      - ` item.
"""

from pathlib import Path
import itertools
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
PIP_INSTALL = re.compile(r"\bpip3?\s+install\b")
CONSTRAINT = "--constraint constraints.txt"


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
        self.triggers = self.top_level_block(lines[:start], "on")
        self.permissions = {
            field.strip(): value.strip()
            for field, _, value in (
                line.strip().partition(":")
                for line in self.top_level_lines(lines[:start], "permissions")
            )
        }
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

    @staticmethod
    def top_level_lines(lines, key):
        """The non-comment lines indented under a top-level `key:`. Comments
        are skipped so they can't end the block or be read as keys."""
        body, inside = [], False
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line.startswith(" "):
                inside = line == f"{key}:"
            elif inside:
                body.append(line)
        return body

    @classmethod
    def top_level_block(cls, lines, key):
        """{name: [its lines, stripped]} for each `  name:` under `key:`."""
        block, current = {}, None
        for line in cls.top_level_lines(lines, key):
            match = re.match(r"^  ([A-Za-z0-9_-]+):(.*)$", line)
            if match:
                current = match.group(1)
                block[current] = [match.group(2).strip()] if match.group(2).strip() else []
            elif current and line.startswith("    "):
                block[current].append(line.strip())
        return block

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


def filter_values(lines, key):
    """The items of a `key: [a, b]` or block `key:` / `- a` list in a
    trigger's lines, or None when the trigger has no such filter."""
    for i, line in enumerate(lines):
        name, sep, value = line.partition(":")
        if not sep or name.strip() != key:
            continue
        value = value.strip()
        if value:
            return [item.strip().strip("'\"") for item in value.strip("[]").split(",") if item.strip()]
        return [
            item[1:].strip().strip("'\"")
            for item in itertools.takewhile(lambda l: l.startswith("-"), lines[i + 1:])
        ]
    return None


def cron_entries(lines):
    return [
        match.group(1)
        for match in (re.fullmatch(r"-\s*cron:\s*[\"']([^\"']+)[\"']", line) for line in lines)
        if match
    ]


def cron_field(field, low, high):
    """The values a cron field covers, for `*`, numbers, ranges and lists;
    None for anything else (steps, names), so a test can refuse it."""
    if field == "*":
        return set(range(low, high + 1))
    values = set()
    for part in field.split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not low <= start <= end <= high:
            return None
        values.update(range(start, end + 1))
    return values


# The end-of-day archive runs at 23:20 TRT, 20:20 UTC, and takes a few
# minutes. The weekday CI run must start after it and before midnight UTC, so
# the day it checks is still the weekday's close.
SCHEDULE_EARLIEST = 20 * 60 + 30
SCHEDULE_LATEST = 23 * 60 + 59
WEEKDAYS = {1, 2, 3, 4, 5}


def weekday_evening_run(cron):
    """Why `cron` isn't a single run each weekday between 20:30 and 23:59
    UTC, or None when it is."""
    fields = cron.split()
    if len(fields) != 5:
        return "a cron has five fields"
    minute, hour, day, month, weekday = fields
    if day != "*" or month != "*":
        return "runs on every day of the month, in every month"
    if cron_field(weekday, 0, 7) != WEEKDAYS:
        return "runs Monday to Friday, and only then"
    minutes, hours = cron_field(minute, 0, 59), cron_field(hour, 0, 23)
    if not minutes or not hours or len(minutes) != 1 or len(hours) != 1:
        return "runs once a day, at one fixed time"
    at = hours.pop() * 60 + minutes.pop()
    if not SCHEDULE_EARLIEST <= at <= SCHEDULE_LATEST:
        return "starts between 20:30 and 23:59 UTC, after the end-of-day archive"
    return None


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

    def test_reader_finds_every_trigger(self):
        """The trigger checks below read `on:` as a block; a flow-style
        `on: [push]` or a renamed trigger would hide from them."""
        self.assertEqual(set(PAGES.triggers), {"push", "schedule", "workflow_dispatch"})
        self.assertEqual(
            set(CI.triggers), {"pull_request", "push", "workflow_dispatch", "schedule"}
        )
        self.assertEqual(PAGES.triggers["push"], ["branches: [main]"])
        self.assertEqual(cron_entries(PAGES.triggers["schedule"]), ["20 * * * *"])
        self.assertEqual(PAGES.permissions, {"contents": "read"})


class TimeoutTests(unittest.TestCase):
    def test_every_job_has_a_timeout(self):
        for workflow, label in ((PAGES, "pages.yml"), (CI, "ci.yml")):
            for name, job in workflow.jobs.items():
                with self.subTest(workflow=label, job=name):
                    self.assertIsNotNone(job.timeout, "missing timeout-minutes")
                    self.assertGreater(job.timeout, 0)
                    self.assertLessEqual(job.timeout, 30)


class CITriggerTests(unittest.TestCase):
    """CI checks what reaches the live site: the owner's direct commits to
    main, and each close the end-of-day archive adds."""

    def test_pull_requests_and_manual_runs_are_unfiltered(self):
        self.assertEqual(CI.triggers["pull_request"], [])
        self.assertEqual(CI.triggers["workflow_dispatch"], [])

    def test_push_to_main_runs_ci(self):
        push = CI.triggers["push"]
        self.assertIsNone(filter_values(push, "branches-ignore"), "push must not ignore branches")
        branches = filter_values(push, "branches")
        if branches is not None:
            self.assertTrue({"main", "**"} & set(branches), "push branches must include main")
        if branches is None:
            for key in ("tags", "tags-ignore"):
                with self.subTest(filter=key):
                    # A tags filter alone stops every branch push.
                    self.assertIsNone(filter_values(push, key))

    def test_weekday_run_after_the_end_of_day_archive(self):
        crons = cron_entries(CI.triggers.get("schedule", []))
        self.assertEqual(len(crons), 1, "one scheduled run")
        self.assertIsNone(weekday_evening_run(crons[0]))

    def test_schedule_check_refuses_runs_outside_the_window(self):
        """Guards the cron check itself."""
        self.assertIsNone(weekday_evening_run("50 21 * * 1-5"))
        self.assertIsNone(weekday_evening_run("30 20 * * 1,2,3,4,5"))
        self.assertIsNone(weekday_evening_run("59 23 * * 1-5"))
        for cron in (
            "20 20 * * 1-5",   # before the archive has finished
            "0 0 * * 2-6",     # after midnight UTC
            "50 21 * * *",     # every day
            "50 21 * * 1-4",   # misses Friday's close
            "50 21 * * 0-5",   # adds Sunday
            "*/30 21 * * 1-5",  # more than once a day
            "50 21 1 * 1-5",   # day-of-month filter
            "50 21 * * MON-FRI",  # names this check can't read
            "50 21 * *",       # four fields
        ):
            with self.subTest(cron=cron):
                self.assertIsNotNone(weekday_evening_run(cron))

    def test_filter_reader(self):
        """Guards the push check: both list styles are read."""
        self.assertEqual(filter_values(["branches-ignore: [main]"], "branches-ignore"), ["main"])
        self.assertEqual(
            filter_values(["branches:", "- dev", "- 'main'"], "branches"), ["dev", "main"]
        )
        self.assertIsNone(filter_values(["paths: [app.js]"], "branches"))

    def test_ci_never_writes(self):
        """No trigger, the schedule and pushes to main included, gets a write
        scope: CI only reads the repo and data-history."""
        self.assertEqual(CI.permissions, {"contents": "read"})
        self.assertNotRegex(CI.code, r"\bwrite-all\b")
        self.assertNotRegex(CI.code, r":\s*write\b")
        for name, job in CI.jobs.items():
            with self.subTest(job=name):
                self.assertNotIn("permissions", job.keys)


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


class DependencyPinTests(unittest.TestCase):
    """Every install resolves against constraints.txt, so an upstream release
    reaches the refresh only through scripts/lock_dependencies.sh."""

    @staticmethod
    def code(step):
        return "\n".join(
            line for line in step.splitlines() if not line.lstrip().startswith("#")
        )

    def install_steps(self, workflow):
        return [
            (name, step)
            for name, job in workflow.jobs.items()
            for step in job.steps
            if PIP_INSTALL.search(self.code(step))
        ]

    def test_every_pip_install_passes_the_constraints(self):
        for workflow, label in ((PAGES, "pages.yml"), (CI, "ci.yml")):
            for name, step in self.install_steps(workflow):
                with self.subTest(workflow=label, job=name, step=step.splitlines()[0]):
                    for install in PIP_INSTALL.finditer(self.code(step)):
                        line = self.code(step)[install.start():].splitlines()[0]
                        self.assertIn(CONSTRAINT, line)

    def test_the_reader_finds_every_install(self):
        """Guards the check above: an install it can't see isn't checked."""
        self.assertEqual(
            {name for name, _ in self.install_steps(PAGES)}, {"refresh", "archive"}
        )
        self.assertEqual(
            {name for name, _ in self.install_steps(CI)}, {"tests", "smoke", "archive"}
        )
        for workflow in (PAGES, CI):
            self.assertEqual(
                len(PIP_INSTALL.findall(workflow.code)),
                len(self.install_steps(workflow)),
                "one install per step, each inside a job step",
            )

    def test_pip_caches_are_keyed_on_the_constraints(self):
        """A cached wheel set from before a re-lock is never restored."""
        for workflow, label in ((PAGES, "pages.yml"), (CI, "ci.yml")):
            for name, job in workflow.jobs.items():
                for step in job.steps:
                    if "cache: pip" not in step:
                        continue
                    with self.subTest(workflow=label, job=name):
                        lines = step.splitlines()
                        start = next(
                            (i for i, line in enumerate(lines)
                             if "cache-dependency-path:" in line),
                            None,
                        )
                        self.assertIsNotNone(start, "cache: pip without cache-dependency-path")
                        paths = lines[start].partition(":")[2].strip()
                        if paths == "|":
                            paths = " ".join(
                                line.strip() for line in itertools.takewhile(
                                    lambda line: line.startswith("            "),
                                    lines[start + 1:],
                                )
                            )
                        self.assertIn("constraints.txt", paths.split())


if __name__ == "__main__":
    unittest.main()
