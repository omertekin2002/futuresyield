"""Headless-browser smoke test of the dashboard against a fixed snapshot.

Run with: python -m unittest tests/e2e/test_dashboard_smoke.py -v

Needs the packages in requirements-dev.txt and a Playwright Chromium
(`python -m playwright install chromium`). This directory has no
__init__.py, so `python -m unittest discover -s tests` never collects it.
"""

import copy
from datetime import date, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - exercised only without Playwright
    sync_playwright = None

try:
    from axe_playwright_python.sync_playwright import Axe
except ImportError:  # pragma: no cover - exercised only without axe
    Axe = None

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "_site"
FIXTURE = ROOT / "tests" / "fixtures" / "market.json"
FORMATTED_NUMBER = re.compile(r"^\d{1,3}(,\d{3})*\.\d+$")

# Test fixture history: archived days derived from FIXTURE (live market date
# 2026-10-07) with every price scaled, so each day renders values that differ
# from the live snapshot. They sit 1 week, 1 month and 3 months before it for
# the comparison quick picks.
HISTORY_FIXTURE = {
    "2026-07-07": 0.90,
    "2026-09-07": 0.95,
    "2026-09-30": 0.97,
    "2026-10-05": 0.98,
    "2026-10-06": 0.99,
}
HISTORY_LABELS = {
    "2026-07-07": "07 Jul 2026",
    "2026-09-07": "07 Sept 2026",
    "2026-09-30": "30 Sept 2026",
    "2026-10-05": "05 Oct 2026",
    "2026-10-06": "06 Oct 2026",
}
LIVE_LABEL = "07 Oct 2026"
# The browser clock for every test unless it sets its own: late on the
# fixture's market date in Istanbul, so the live snapshot keeps its stored
# days left and is still old enough to show as delayed.
FIXTURE_DAY_TIME = datetime.fromisoformat("2026-10-07T23:00:00+03:00")
# The next Istanbul day, when days left move on by one.
NEXT_DAY_TIME = datetime.fromisoformat("2026-10-08T10:00:00+03:00")
# The contract table's yield columns: label, stored field, period in days, digits.
YIELD_COLUMNS = (
    ("Daily yield · 1D", "daily_yield_percent", 1, 4),
    ("Monthly yield · 30D", "monthly_yield_percent", 30, 2),
    ("Annualized · 365D", "annualized_yield_percent", 365, 2),
)
# On this day each market still lists a Sep 2026 contract and not yet its
# last live maturity, so a comparison has contracts missing on either side.
ROLLED_DAY = "2026-07-07"
SPOT_PRICES = ("last", "open", "high", "low", "change")
CONTRACT_PRICES = ("last", "bid", "ask", "change", "expected_spot_at_maturity")

# The accessibility audit: axe-core's WCAG 2.1 A and AA rules, failing on any
# serious or critical violation.
AXE_TAGS = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa")
AXE_FAILING_IMPACTS = ("serious", "critical")
# Rules the audit turns off, each mapped to a one-line reason. None so far.
AXE_DISABLED_RULES = {}
MOBILE_VIEWPORT = {"width": 390, "height": 844}


def set_yields(contract, spot):
    """Recompute a contract's premium and yields as scripts/update_data.py does."""
    last, days = contract.get("last"), contract["days_to_maturity"]
    if not isinstance(last, (int, float)) or days <= 0:
        for key in ("daily_yield_factor", "daily_yield_percent",
                    "monthly_yield_percent", "annualized_yield_percent"):
            contract[key] = None
        return
    factor = (last / spot) ** (1 / days)
    contract["premium_percent"] = (last / spot - 1) * 100
    contract["daily_yield_factor"] = factor
    contract["daily_yield_percent"] = (factor - 1) * 100
    contract["monthly_yield_percent"] = (factor ** 30 - 1) * 100
    contract["annualized_yield_percent"] = (factor ** 365 - 1) * 100


def derive_day(snapshot, day, factor):
    """Return a test-only copy of snapshot, dated day, with prices scaled."""
    derived = copy.deepcopy(snapshot)
    derived["test_fixture"] = (
        f"Test fixture: {FIXTURE.name} with every price scaled by {factor}. "
        "Not market data."
    )
    derived["generated_at"] = f"{day}T23:30:00+03:00"
    derived["market_date"] = day
    # Each contract is this many more days from maturity on the earlier day.
    gap = (date.fromisoformat(snapshot["market_date"]) - date.fromisoformat(day)).days
    for market in derived["markets"].values():
        for key in SPOT_PRICES:
            if isinstance(market["spot"].get(key), (int, float)):
                market["spot"][key] *= factor
        for contract in market["contracts"]:
            for key in CONTRACT_PRICES:
                if isinstance(contract.get(key), (int, float)):
                    contract[key] *= factor
            contract["days_to_maturity"] += gap
        if day == ROLLED_DAY:
            expired = copy.deepcopy(market["contracts"][0])
            expired["code"] = expired["code"].replace("1026", "0926")
            expired["label"] = "Sep 2026"
            expired["maturity_date"] = "2026-09-30"
            expired["days_to_maturity"] = (date(2026, 9, 30) - date.fromisoformat(day)).days
            expired["last"] *= 0.97
            market["contracts"] = [expired] + market["contracts"][:-1]
        for contract in market["contracts"]:
            set_yields(contract, market["spot"]["last"])
    return derived


def build_site(destination, history_dir):
    """Build the site with history from history_dir, then copy it to destination."""
    env = {**os.environ, "HISTORY_DIR": str(history_dir)}
    subprocess.run(["bash", "scripts/build_site.sh"], cwd=ROOT, env=env, check=True)
    shutil.copytree(SITE, destination)
    shutil.copyfile(FIXTURE, destination / "data" / "market.json")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class DashboardSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.history = {
            day: derive_day(cls.snapshot, day, factor)
            for day, factor in HISTORY_FIXTURE.items()
        }
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)

        history_dir = tmp / "data-history" / "history"
        history_dir.mkdir(parents=True)
        for day, snapshot in cls.history.items():
            (history_dir / f"{day}.json").write_text(json.dumps(snapshot), encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "days": [
                {
                    "date": day,
                    "generated_at": snapshot["generated_at"],
                    "markets": snapshot["market_order"],
                }
                for day, snapshot in sorted(cls.history.items())
            ],
        }
        (history_dir / "index.json").write_text(json.dumps(manifest), encoding="utf-8")

        # Two sites: one with the fixture history, one built without any.
        sites = tmp / "sites"
        build_site(sites / "history", history_dir)
        build_site(sites / "live", tmp / "missing")

        handler = partial(QuietHandler, directory=str(sites))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        root = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        cls.base_url = root + "history/"
        cls.no_history_url = root + "live/"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.errors = []
        self.external_requests = []
        self.open_context()

    def open_context(self, timezone_id=None, now=FIXTURE_DAY_TIME, **options):
        """Open a fresh page whose clock reads now, in an optional local zone."""
        self.context = self.browser.new_context(
            **{
                "viewport": {"width": 1280, "height": 900},
                **({"timezone_id": timezone_id} if timezone_id else {}),
                **options,
            }
        )
        self.page = self.context.new_page()
        self.page.clock.set_fixed_time(now)
        self.page.on(
            "console",
            lambda message: message.type == "error"
            and self.errors.append(f"console: {message.text}"),
        )
        self.page.on("pageerror", lambda error: self.errors.append(f"pageerror: {error}"))
        # The test must never reach live data or any other host.
        self.page.route(re.compile(r"^(?!http://127\.0\.0\.1:)"), self.block_external)

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.external_requests, [], "page requested a non-local URL")
        self.assertEqual(self.errors, [], "page logged errors")

    def block_external(self, route):
        self.external_requests.append(route.request.url)
        route.abort()

    def open_dashboard(self, query="", base_url=None):
        self.page.goto((base_url or self.base_url) + query)
        expect(self.page.locator("#data-state")).to_have_class(re.compile(r"\bis-delayed\b"))

    def open_day(self, day, query=""):
        self.page.goto(f"{self.base_url}?date={day}{query}")
        self.assert_historical(day)

    def assert_historical(self, day):
        label = HISTORY_LABELS[day]
        state = self.page.locator("#data-state")
        expect(state).to_have_class(re.compile(r"\bis-historical\b"))
        expect(state).to_contain_text(f"Historical · close of {label}")
        expect(self.page.locator("#history-banner")).to_be_visible()
        expect(self.page.locator("#history-banner-date")).to_have_text(label)
        expect(self.page.locator("#history-select")).to_have_value(day)
        self.assertIn(f"date={day}", self.page.url)

    def assert_live(self):
        state = self.page.locator("#data-state")
        expect(state).to_have_class(re.compile(r"\bis-delayed\b"))
        expect(state).to_contain_text("Update delayed")
        expect(self.page.locator("#history-banner")).to_be_hidden()
        self.assertNotIn("date=", self.page.url)

    def assert_market(self, key, snapshot=None):
        market = (snapshot or self.snapshot)["markets"][key]
        page = self.page

        expect(page.locator("body")).to_have_attribute("data-market", key)
        pressed = page.locator('.market-options [data-market][aria-pressed="true"]')
        expect(pressed).to_have_count(1)
        expect(pressed).to_have_attribute("data-market", key)

        spot = page.locator("#spot-value")
        expect(spot).to_have_text(FORMATTED_NUMBER)
        spot_text = spot.inner_text().strip()
        self.assertAlmostEqual(
            float(spot_text.replace(",", "")),
            market["spot"]["last"],
            delta=10 ** -market["price_digits"],
        )

        rows = page.locator("#contract-rows > tr")
        expect(rows).to_have_count(len(market["contracts"]))
        expect(rows.first.locator("strong")).to_have_text(market["contracts"][0]["label"])
        front_last = market["contracts"][0].get("last")
        if isinstance(front_last, (int, float)):
            last_text = rows.first.locator(".last-price").inner_text().strip()
            self.assertAlmostEqual(
                float(last_text.replace(",", "")),
                front_last,
                delta=10 ** -market["price_digits"],
            )

        # app.js only sets these labels after drawing each chart.
        for canvas in ("#curve-chart", "#yield-chart"):
            expect(page.locator(canvas)).to_have_attribute(
                "aria-label", re.compile(re.escape(market["pair"])),
            )

        carry = page.locator("#carry")
        if isinstance(market.get("contract_size"), (int, float)):
            expect(carry).to_be_visible()
            expect(page.locator("#carry-rows > tr")).to_have_count(len(market["contracts"]))
        else:
            expect(carry).to_be_hidden()

    def test_snapshot_shows_delayed_state(self):
        self.open_dashboard()
        expect(self.page.locator("#data-state")).to_contain_text("Update delayed")

    # Data status around the weekday 09:20–23:20 Istanbul refresh window

    def open_live_at(self, now, generated_at=None, timezone_id=None):
        """Open live data at now, optionally as if generated at generated_at."""
        self.reopen_at(datetime.fromisoformat(now), timezone_id)
        if generated_at:
            snapshot = copy.deepcopy(self.snapshot)
            snapshot["generated_at"] = generated_at
            self.serve_snapshot(re.compile(r"/data/market\.json"), snapshot)
        self.page.goto(self.base_url)

    def assert_status(self, state, text):
        status = self.page.locator("#data-state")
        expect(status).to_have_class(re.compile(rf"\b{state}\b"))
        expect(status).to_have_text(text)
        if state != "is-delayed":
            expect(status).not_to_have_class(re.compile(r"\bis-delayed\b"))

    def test_status_inside_the_refresh_window_keeps_the_45_minute_rule(self):
        # The fixture was generated on Wednesday 7 Oct at 14:06:12 Istanbul time.
        for now, state, text in (
            ("2026-10-07T14:30:00+03:00", "is-fresh", "Latest snapshot"),
            ("2026-10-07T14:51:12+03:00", "is-fresh", "Latest snapshot"),
            ("2026-10-07T15:00:00+03:00", "is-delayed", "Update delayed · 53m old"),
            ("2026-10-07T23:20:00+03:00", "is-delayed", "Update delayed · 553m old"),
        ):
            with self.subTest(now=now):
                self.open_live_at(now)
                self.assert_status(state, text)

    def test_status_after_hours_is_closed_once_the_last_refresh_landed(self):
        # The last scheduled refresh before each clock is 23:20 on the most
        # recent weekday, so a snapshot from 22:35 or later is on schedule.
        for now, generated_at in (
            ("2026-10-10T12:00:00+03:00", "2026-10-09T22:50:00+03:00"),  # Saturday
            ("2026-10-11T23:59:00+03:00", "2026-10-09T22:35:00+03:00"),  # Sunday
            ("2026-10-12T09:19:00+03:00", "2026-10-09T23:20:00+03:00"),  # Monday morning
            ("2026-10-08T02:00:00+03:00", "2026-10-07T23:05:00+03:00"),  # weeknight
            ("2026-10-07T23:59:00+03:00", "2026-10-07T23:05:00+03:00"),  # after 23:20
        ):
            with self.subTest(now=now):
                self.open_live_at(now, generated_at)
                label = datetime.fromisoformat(generated_at).strftime("%d %b, %H:%M")
                self.assert_status("is-closed", f"Market closed · snapshot {label} TRT")

    def test_status_after_hours_still_flags_a_missed_last_refresh(self):
        for now, generated_at, minutes in (
            ("2026-10-10T12:00:00+03:00", None, 4193),  # Saturday, Wednesday's snapshot
            ("2026-10-10T12:00:00+03:00", "2026-10-09T22:34:59+03:00", 805),
            ("2026-10-08T02:00:00+03:00", None, 713),  # weeknight, afternoon snapshot
            ("2026-10-12T09:20:00+03:00", "2026-10-09T23:20:00+03:00", 3480),  # window opens
        ):
            with self.subTest(now=now, generated_at=generated_at):
                self.open_live_at(now, generated_at)
                self.assert_status("is-delayed", f"Update delayed · {minutes}m old")

    def test_status_window_uses_istanbul_time(self):
        # 06:00 UTC on Saturday is 09:00 in Istanbul but still Friday 23:00 in
        # Los Angeles; 10:30 UTC on Friday is 13:30 in Istanbul but already
        # Saturday 00:30 in Kiritimati.
        for now, zone, generated_at, state, text in (
            ("2026-10-10T06:00:00+00:00", "America/Los_Angeles",
             "2026-10-09T22:50:00+03:00", "is-closed",
             "Market closed · snapshot 09 Oct, 22:50 TRT"),
            ("2026-10-09T10:30:00+00:00", "Pacific/Kiritimati",
             "2026-10-08T22:50:00+03:00", "is-delayed", "Update delayed · 880m old"),
        ):
            with self.subTest(zone=zone):
                self.open_live_at(now, generated_at, zone)
                self.assert_status(state, text)

    def test_each_market_via_query(self):
        for key in self.snapshot["market_order"]:
            with self.subTest(market=key):
                self.open_dashboard(f"?market={key}")
                self.assert_market(key)

    def test_each_market_via_button(self):
        default = self.snapshot["default_market"]
        self.open_dashboard()
        self.assert_market(default)
        # Visit the default market last so every market, including the
        # default, is reached by a click from a different market.
        order = [key for key in self.snapshot["market_order"] if key != default]
        for key in order + [default]:
            with self.subTest(market=key):
                self.page.locator(f'.market-options [data-market="{key}"]').click()
                self.assert_market(key)
                self.assertIn(f"market={key}", self.page.url)

    def test_yield_period_toggle(self):
        self.open_dashboard()
        labels = {
            "daily": "Daily · 1D",
            "monthly": "Monthly · 30D",
            "annualized": "Annualized · 365D",
        }
        for period in ("monthly", "annualized", "daily"):
            with self.subTest(period=period):
                self.page.locator(f'[data-yield-period="{period}"]').click()
                pressed = self.page.locator('[data-yield-period][aria-pressed="true"]')
                expect(pressed).to_have_count(1)
                expect(pressed).to_have_attribute("data-yield-period", period)
                expect(self.page.locator("#yield-period-label")).to_have_text(labels[period])

    def test_history_control_lists_days_newest_first(self):
        self.open_dashboard()
        control = self.page.get_by_role("combobox", name="View", exact=True)
        expect(control).to_be_visible()
        expect(control).to_have_value("")
        expect(control.locator("option")).to_have_text(
            ["Live"] + [f"Close · {HISTORY_LABELS[day]}" for day in sorted(HISTORY_FIXTURE, reverse=True)]
        )

    def test_selecting_a_day_renders_it_for_every_market(self):
        self.open_dashboard()
        for day, snapshot in self.history.items():
            with self.subTest(day=day):
                self.page.locator("#history-select").select_option(day)
                self.assert_historical(day)
                for key in self.snapshot["market_order"]:
                    with self.subTest(market=key):
                        self.page.locator(f'.market-options [data-market="{key}"]').click()
                        self.assert_market(key, snapshot)
                        self.assertIn(f"date={day}", self.page.url)
                        self.assertIn(f"market={key}", self.page.url)
                expect(self.page.locator("#market-date")).to_have_text(HISTORY_LABELS[day])

    def test_history_control_is_keyboard_operable(self):
        self.open_dashboard()
        control = self.page.locator("#history-select")
        control.focus()
        self.page.keyboard.press("ArrowDown")
        newest = max(HISTORY_FIXTURE)
        self.assert_historical(newest)
        self.assert_market(self.snapshot["default_market"], self.history[newest])

    def test_deep_link_to_day_and_market(self):
        day = min(HISTORY_FIXTURE)
        self.open_day(day, "&market=XAUTRY")
        self.assert_market("XAUTRY", self.history[day])
        self.page.locator('.market-options [data-market="EURTRY"]').click()
        self.assert_market("EURTRY", self.history[day])
        self.assert_historical(day)
        self.assertIn("market=EURTRY", self.page.url)

    def test_poll_never_replaces_a_past_close(self):
        live_requests = []
        self.page.on(
            "request",
            lambda request: "data/market.json" in request.url
            and live_requests.append(request.url),
        )
        self.page.clock.install()
        day = max(HISTORY_FIXTURE)
        self.open_day(day)
        self.page.clock.run_for("03:00")
        self.assert_historical(day)
        self.assert_market(self.snapshot["default_market"], self.history[day])
        self.assertEqual(live_requests, [], "the poll fetched live data")

    def test_back_to_live_and_refresh_leave_a_past_close(self):
        day = min(HISTORY_FIXTURE)
        for control in ("#live-button", "#refresh-button"):
            with self.subTest(control=control):
                self.open_day(day, "&market=EURTRY")
                self.page.locator(control).click()
                self.assert_live()
                self.assert_market("EURTRY")
                expect(self.page.locator("#history-select")).to_have_value("")
        self.page.locator("#history-select").select_option("")
        self.assert_live()

    def test_unknown_date_falls_back_to_live(self):
        for value, notice in (
            ("2020-01-01", "No archived close for 2020-01-01"),
            ("not-a-date", "not in the archive"),
        ):
            with self.subTest(date=value):
                self.open_dashboard(f"?date={value}&market=EURTRY")
                self.assert_live()
                self.assert_market("EURTRY")
                expect(self.page.locator("#history-notice")).to_contain_text(notice)

    def test_no_history_hides_control(self):
        self.open_dashboard(base_url=self.no_history_url)
        expect(self.page.locator("#history-control")).to_be_hidden()
        self.assert_live()
        self.assert_market(self.snapshot["default_market"])

    def test_no_history_ignores_date_link(self):
        self.open_dashboard(f"?date={max(HISTORY_FIXTURE)}", base_url=self.no_history_url)
        self.assert_live()
        expect(self.page.locator("#history-control")).to_be_hidden()
        expect(self.page.locator("#history-notice")).to_be_visible()

    def serve_snapshot(self, pattern, snapshot):
        self.page.route(
            pattern,
            lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(snapshot)
            ),
        )

    def expect_load_errors(self, count):
        """Take the expected console errors for rejected snapshots off the list."""
        rejected = [error for error in self.errors if "snapshot" in error]
        self.assertEqual(len(rejected), count, self.errors)
        self.errors = [error for error in self.errors if error not in rejected]

    def test_rejects_unknown_schema_version(self):
        unknown = copy.deepcopy(self.snapshot)
        unknown["schema_version"] = 5
        self.serve_snapshot(re.compile(r"/data/market\.json"), unknown)
        self.page.goto(self.base_url)
        state = self.page.locator("#data-state")
        expect(state).to_have_class(re.compile(r"\bis-error\b"))
        expect(state).to_contain_text("Data unavailable")
        expect(self.page.locator("#contract-rows")).to_contain_text("could not be loaded")
        self.expect_load_errors(1)

    def test_rejects_archived_day_with_bad_schema(self):
        day = max(HISTORY_FIXTURE)
        unknown = copy.deepcopy(self.history[day])
        unknown["schema_version"] = 3
        missing = copy.deepcopy(self.history[day])
        del missing["markets"]["EURTRY"]["contracts"][0]["days_to_maturity"]
        # The newest matching route wins, so each subtest serves its own file.
        for name, snapshot in (("schema_version", unknown), ("days", missing)):
            with self.subTest(broken=name):
                self.serve_snapshot(re.compile(rf"/data/history/{day}\.json"), snapshot)
                self.open_dashboard(f"?date={day}")
                self.assert_live()
                self.assert_market(self.snapshot["default_market"])
                expect(self.page.locator("#history-notice")).to_contain_text(
                    "could not be loaded"
                )
        self.expect_load_errors(2)

    def test_snapshot_markup_renders_as_text(self):
        # Bypasses the updater's schema check, which rejects such a snapshot.
        markup = '<img src=x onerror="window.__xss=1">'
        hostile = copy.deepcopy(self.snapshot)
        usd = hostile["markets"]["USDTRY"]
        usd["pair"] += markup
        usd["spot_unit"] += markup
        front = usd["contracts"][0]
        front["label"] += markup
        front["code"] += markup
        # The comparison day lists no USD/TRY, so the legend names the pair.
        day = "2026-09-30"
        past = copy.deepcopy(self.history[day])
        del past["markets"]["USDTRY"]
        past["market_order"].remove("USDTRY")
        self.serve_snapshot(re.compile(r"/data/market\.json"), hostile)
        self.serve_snapshot(re.compile(rf"/data/history/{day}\.json"), past)
        self.open_dashboard("?market=USDTRY")

        page = self.page
        for rows in ("#contract-rows", "#carry-rows"):
            with self.subTest(rows=rows):
                first = page.locator(f"{rows} > tr").first
                expect(first.locator("strong")).to_have_text(front["label"])
                expect(first.locator("small")).to_contain_text(front["code"])

        tooltips = (("#curve-chart", "#chart-tooltip", "chartPoints"),
                    ("#yield-chart", "#yield-tooltip", "yieldChartPoints"))
        for canvas, tooltip, kind in tooltips:
            with self.subTest(tooltip=tooltip):
                self.hover(canvas, self.chart_points(kind)[0])
                expect(page.locator(f"{tooltip} strong")).to_have_text(front["label"])
        expect(page.locator("#chart-tooltip")).to_contain_text(usd["spot_unit"])

        self.compare_select().select_option(day)
        expect(page.locator("#curve-legend")).to_contain_text(f"no {usd['pair']} data")
        expect(page.locator("#yield-legend")).to_contain_text(f"no {usd['pair']} data")

        containers = ("#contract-rows", "#carry-rows", "#chart-tooltip",
                      "#yield-tooltip", "#curve-legend", "#yield-legend")
        for container in containers:
            with self.subTest(container=container):
                expect(page.locator(f"{container} img")).to_have_count(0)
        self.assertIsNone(page.evaluate("() => window.__xss"))

    # Comparison overlay

    def compare_select(self):
        return self.page.locator("#compare-select")

    def choose_compare(self, day):
        self.compare_select().select_option(day)
        self.assert_comparing(day)

    def assert_comparing(self, day, current=LIVE_LABEL):
        expect(self.compare_select()).to_have_value(day)
        for legend in ("#curve-legend", "#yield-legend"):
            expect(self.page.locator(legend)).to_be_visible()
            expect(self.page.locator(legend)).to_contain_text(f"· {current}")
            expect(self.page.locator(legend)).to_contain_text(f"Close · {HISTORY_LABELS[day]}")
        for canvas in ("#curve-chart", "#yield-chart"):
            expect(self.page.locator(canvas)).to_have_attribute(
                "aria-label", re.compile(re.escape(f"close of {HISTORY_LABELS[day]}")),
            )
        self.assertIn(f"compare={day}", self.page.url)

    def assert_not_comparing(self):
        expect(self.compare_select()).to_have_value("")
        expect(self.page.locator("#curve-legend")).to_be_hidden()
        expect(self.page.locator("#yield-legend")).to_be_hidden()
        self.assertNotIn("compare=", self.page.url)

    def chart_points(self, kind="chartPoints"):
        """The plotted slots app.js keeps for its tooltips, one per maturity month."""
        return self.page.evaluate(
            f"""() => state.{kind}.map((point) => ({{
                x: point.x,
                y: point.y,
                label: point.label,
                current: point.current?.value ?? null,
                compare: point.compare?.value ?? null,
            }}))"""
        )

    def hover(self, canvas, point):
        self.page.locator(canvas).scroll_into_view_if_needed()
        box = self.page.locator(canvas).bounding_box()
        self.page.mouse.move(box["x"] + point["x"], box["y"] + point["y"])

    def hover_label(self, canvas, label, kind="chartPoints"):
        points = [point for point in self.chart_points(kind) if point["label"] == label]
        self.assertEqual(len(points), 1, f"no single {label} point")
        self.hover(canvas, points[0])

    def test_compare_control_offers_quick_picks_and_archived_days(self):
        self.open_dashboard()
        control = self.page.get_by_role("combobox", name="Compare with", exact=True)
        expect(control).to_be_visible()
        expect(control).to_have_value("")
        expect(control.locator("option")).to_have_text(
            [
                "None",
                "1 week ago · 30 Sept 2026",
                "1 month ago · 07 Sept 2026",
                "3 months ago · 07 Jul 2026",
            ]
            + [f"Close · {HISTORY_LABELS[day]}" for day in sorted(HISTORY_FIXTURE, reverse=True)]
        )
        self.assert_not_comparing()

    def test_quick_picks_need_a_close_near_the_target(self):
        # From 07 Sep only 07 Jul is earlier: two months back, too far from
        # 1 week, 1 month or 3 months ago to be offered as a quick pick.
        self.open_day("2026-09-07")
        expect(self.compare_select().locator("option")).to_have_text(
            ["None", "Close · 07 Jul 2026"]
        )

    def test_comparison_overlays_both_curves_for_every_market(self):
        day = "2026-09-30"
        self.open_dashboard()
        self.choose_compare(day)
        for key in self.snapshot["market_order"]:
            with self.subTest(market=key):
                self.page.locator(f'.market-options [data-market="{key}"]').click()
                self.assert_market(key)
                self.assert_comparing(day)
                self.assertIn(f"market={key}", self.page.url)
                live = self.snapshot["markets"][key]["contracts"]
                past = self.history[day]["markets"][key]["contracts"]
                for kind in ("chartPoints", "yieldChartPoints"):
                    points = self.chart_points(kind)
                    self.assertEqual(len(points), len(live))
                    self.assertEqual(
                        [point["label"] for point in points],
                        [contract["label"] for contract in live],
                    )
                    self.assertTrue(all(point["compare"] is not None for point in points))
                prices = self.chart_points()
                for point, contract in zip(prices, past):
                    self.assertAlmostEqual(point["compare"], contract["last"])

    def test_tooltips_show_hand_computed_deltas(self):
        day = "2026-09-30"
        self.open_dashboard("?market=USDTRY")
        self.choose_compare(day)

        # Oct 2026 closed at 50.083 live and at 50.083 × 0.97 = 48.58051 on
        # 30 Sep, so it rose by 1.50249.
        self.hover_label("#curve-chart", "Oct 2026")
        tooltip = self.page.locator("#chart-tooltip")
        expect(tooltip).to_be_visible()
        expect(tooltip).to_contain_text(f"{LIVE_LABEL}: 50.0830")
        expect(tooltip).to_contain_text("30 Sept 2026: 48.5805")
        expect(tooltip).to_contain_text("Δ +1.5025 TRY per USD")

        # Prices and spot were scaled alike, so only the extra 7 days to
        # maturity change the yield: (50.083 / 49.19486)^(1/30) on 30 Sep
        # against the live daily factor over 23 days.
        live_factor = (50.083 / 49.19486) ** (1 / 23)
        past_factor = (50.083 / 49.19486) ** (1 / 30)
        for period, days, digits in (("daily", 1, 4), ("annualized", 365, 2)):
            with self.subTest(period=period):
                self.page.locator(f'[data-yield-period="{period}"]').click()
                self.hover_label("#yield-chart", "Oct 2026", "yieldChartPoints")
                delta = ((live_factor ** days) - (past_factor ** days)) * 100
                tooltip = self.page.locator("#yield-tooltip")
                expect(tooltip).to_be_visible()
                expect(tooltip).to_contain_text(f"Δ {delta:+.{digits}f} pp")
                self.assertIn(f"compare={day}", self.page.url)

    def test_contracts_on_one_day_only_keep_the_axis_aligned(self):
        self.open_dashboard("?market=USDTRY")
        self.choose_compare(ROLLED_DAY)
        live = self.snapshot["markets"]["USDTRY"]["contracts"]
        for kind, canvas in (("chartPoints", "#curve-chart"), ("yieldChartPoints", "#yield-chart")):
            with self.subTest(chart=kind):
                points = self.chart_points(kind)
                self.assertEqual(
                    [point["label"] for point in points],
                    ["Sep 2026"] + [contract["label"] for contract in live],
                )
                xs = [point["x"] for point in points]
                self.assertEqual(xs, sorted(set(xs)), "x positions are not increasing")
                gaps = {round(b - a, 6) for a, b in zip(xs, xs[1:])}
                self.assertEqual(len(gaps), 1, "maturity slots are unevenly spaced")
                # Sep 2026 expired before the live day; Jan 2028 was not yet listed.
                self.assertIsNone(points[0]["current"])
                self.assertIsNotNone(points[0]["compare"])
                self.assertIsNotNone(points[-1]["current"])
                self.assertIsNone(points[-1]["compare"])
                self.assertTrue(all(p["current"] is not None and p["compare"] is not None
                                    for p in points[1:-1]))

                tooltip = self.page.locator(canvas.replace("curve-chart", "chart-tooltip")
                                            .replace("yield-chart", "yield-tooltip"))
                self.hover(canvas, points[0])
                expect(tooltip).to_contain_text("Sep 2026")
                expect(tooltip).to_contain_text(f"{LIVE_LABEL}: not listed")
                expect(tooltip).to_contain_text("Δ —")
                self.hover(canvas, points[-1])
                expect(tooltip).to_contain_text("Jan 2028")
                expect(tooltip).to_contain_text("07 Jul 2026: not listed")

    def test_compare_deep_link_survives_market_and_period_switches(self):
        day = "2026-09-07"
        self.open_dashboard(f"?compare={day}&market=EURTRY")
        self.assert_market("EURTRY")
        self.assert_comparing(day)
        self.page.locator('.market-options [data-market="XAUTRY"]').click()
        self.assert_market("XAUTRY")
        self.assert_comparing(day)
        for period in ("annualized", "monthly"):
            self.page.locator(f'[data-yield-period="{period}"]').click()
            expect(self.page.locator("#yield-period-label")).to_contain_text(
                period.capitalize()
            )
            self.assert_comparing(day)
            self.assertIn("market=XAUTRY", self.page.url)

    def test_compare_a_past_day_with_an_earlier_one(self):
        view, day = "2026-10-05", "2026-09-30"
        self.open_day(view, f"&compare={day}&market=XAUTRY")
        self.assert_market("XAUTRY", self.history[view])
        self.assert_comparing(day, current=HISTORY_LABELS[view])
        options = self.compare_select().locator("option")
        expect(options.filter(has_text="06 Oct 2026")).to_have_count(0)
        expect(options.filter(has_text="05 Oct 2026")).to_have_count(0)
        self.assertIn(f"date={view}", self.page.url)

    def test_comparison_that_is_not_earlier_is_dropped(self):
        self.open_day("2026-09-30", "&compare=2026-10-05")
        self.assert_not_comparing()
        expect(self.page.locator("#history-notice")).to_contain_text("not earlier than")

        self.open_dashboard("?compare=2026-09-30")
        self.assert_comparing("2026-09-30")
        self.page.locator("#history-select").select_option(ROLLED_DAY)
        self.assert_historical(ROLLED_DAY)
        self.assert_not_comparing()
        # Nothing archived is earlier than the oldest close, so there is nothing to offer.
        expect(self.page.locator("#compare-control")).to_be_hidden()

    def test_unknown_compare_date_is_ignored(self):
        self.open_dashboard("?compare=2020-01-01&market=EURTRY")
        self.assert_market("EURTRY")
        self.assert_not_comparing()
        expect(self.page.locator("#history-notice")).to_contain_text(
            "No archived close for 2020-01-01 to compare with"
        )

    def test_choosing_none_removes_the_overlay(self):
        self.open_dashboard()
        self.choose_compare("2026-10-06")
        self.compare_select().select_option("")
        self.assert_not_comparing()
        self.assertTrue(all(point["compare"] is None for point in self.chart_points()))

    def test_compare_control_is_keyboard_operable(self):
        self.open_dashboard()
        self.compare_select().focus()
        self.page.keyboard.press("ArrowDown")
        self.assert_comparing("2026-09-30")

    def test_no_history_hides_compare_control(self):
        self.open_dashboard("?compare=2026-10-06", base_url=self.no_history_url)
        expect(self.page.locator("#compare-control")).to_be_hidden()
        expect(self.page.locator("#curve-legend")).to_be_hidden()
        self.assertNotIn("compare=", self.page.url)
        self.assert_market(self.snapshot["default_market"])

    # Days left on a later Istanbul day than the live snapshot's

    def reopen_at(self, now, timezone_id=None, **options):
        """Replace the page with one whose clock reads now."""
        self.context.close()
        self.open_context(timezone_id, now, **options)

    def column(self, rows, label):
        return [
            text.strip()
            for text in self.page.locator(f'{rows} td[data-label="{label}"]').all_inner_texts()
        ]

    def number(self, text):
        return float(text.replace(",", "").replace("%", ""))

    def expected_days(self, market, today):
        """Each contract's calendar days left from today, as the updater counts them."""
        return [
            (date.fromisoformat(contract["maturity_date"]) - today).days
            for contract in market["contracts"]
        ]

    def assert_note(self, adjusted_to):
        note = self.page.locator("#days-note")
        if adjusted_to:
            expect(note).to_be_visible()
            expect(note).to_have_text(
                f"Days left and yields adjusted to {adjusted_to}; "
                f"prices are from the {LIVE_LABEL} snapshot."
            )
        else:
            expect(note).to_be_hidden()

    def assert_days(self, key, days):
        """The table, tooltips and curve horizon show these days left for market key."""
        market = self.snapshot["markets"][key]
        self.assertEqual(self.column("#contract-rows", "Days left"), [str(d) for d in days])
        if isinstance(market.get("contract_size"), (int, float)):
            self.assertEqual(self.column("#carry-rows", "Days left"), [str(d) for d in days])
        priced = [d for d, c in zip(days, market["contracts"]) if c.get("last") is not None]
        expect(self.page.locator("#curve-horizon")).to_have_text(f"{priced[-1]} days")
        self.hover_label("#curve-chart", market["contracts"][-1]["label"])
        expect(self.page.locator("#chart-tooltip")).to_contain_text(f"{days[-1]} days left")
        self.hover_label("#yield-chart", market["contracts"][-1]["label"], "yieldChartPoints")
        expect(self.page.locator("#yield-tooltip")).to_contain_text(f"{days[-1]} days left")

    def test_fixture_day_keeps_stored_values(self):
        self.open_dashboard("?market=USDTRY")
        self.assert_note(None)
        market = self.snapshot["markets"]["USDTRY"]
        self.assert_days("USDTRY", [c["days_to_maturity"] for c in market["contracts"]])
        for label, field, _, digits in YIELD_COLUMNS:
            with self.subTest(column=label):
                for text, contract in zip(self.column("#contract-rows", label), market["contracts"]):
                    self.assertAlmostEqual(self.number(text), contract[field], delta=10 ** -digits)

    def test_next_istanbul_day_recomputes_days_and_yields(self):
        self.reopen_at(NEXT_DAY_TIME)
        today = date(2026, 10, 8)
        for key in self.snapshot["market_order"]:
            with self.subTest(market=key):
                market = self.snapshot["markets"][key]
                self.open_dashboard(f"?market={key}")
                self.assert_note("08 Oct 2026")
                self.assert_market(key)
                days = self.expected_days(market, today)
                self.assertEqual(days, [c["days_to_maturity"] - 1 for c in market["contracts"]])
                self.assert_days(key, days)

                spot = market["spot"]["last"]
                factors = [
                    (c["last"] / spot) ** (1 / d) if c.get("last") else None
                    for c, d in zip(market["contracts"], days)
                ]
                for label, _, period, digits in YIELD_COLUMNS:
                    cells = self.column("#contract-rows", label)
                    for text, factor in zip(cells, factors):
                        self.assertAlmostEqual(
                            self.number(text), (factor ** period - 1) * 100, delta=10 ** -digits,
                        )
                # The yield curve plots the same recomputed daily yields.
                plotted = [p["current"] for p in self.chart_points("yieldChartPoints")]
                for value, factor in zip(plotted, [f for f in factors if f]):
                    self.assertAlmostEqual(value, (factor - 1) * 100, places=9)

                if isinstance(market.get("contract_size"), (int, float)):
                    trend = market["spot_trend"]["daily_factor"]
                    expected = [spot * trend ** d for d in days]
                    spots = self.column("#carry-rows", "Exp. spot at maturity")
                    profits = self.column("#carry-rows", "Exp. profit · TRY")
                    for contract, value, spot_text, profit_text in zip(
                        market["contracts"], expected, spots, profits,
                    ):
                        self.assertAlmostEqual(self.number(spot_text), value, delta=1e-4)
                        if contract.get("bid") is None:
                            self.assertEqual(profit_text, "—")
                        else:
                            self.assertAlmostEqual(
                                self.number(profit_text),
                                (contract["bid"] - value) * market["contract_size"],
                                delta=1,
                            )
                        # A day closer to maturity, the projection grows less.
                        self.assertLess(value, contract["expected_spot_at_maturity"])

    def test_contract_maturing_today_shows_zero_days_and_no_yield(self):
        self.reopen_at(datetime.fromisoformat("2026-10-30T12:00:00+03:00"))
        self.open_dashboard("?market=USDTRY")
        self.assert_note("30 Oct 2026")
        first = self.page.locator("#contract-rows > tr").first
        expect(first.locator("strong")).to_have_text("Oct 2026")
        self.assertEqual(self.column("#contract-rows", "Days left")[0], "0")
        for label, *_ in YIELD_COLUMNS:
            self.assertEqual(self.column("#contract-rows", label)[0], "—")
        labels = [p["label"] for p in self.chart_points("yieldChartPoints")]
        self.assertNotIn("Oct 2026", labels)

    def test_matured_contracts_are_dropped(self):
        self.reopen_at(datetime.fromisoformat("2026-10-31T12:00:00+03:00"))
        for key in self.snapshot["market_order"]:
            with self.subTest(market=key):
                self.open_dashboard(f"?market={key}")
                self.assert_note("31 Oct 2026")
                live = [c["label"] for c in self.snapshot["markets"][key]["contracts"]]
                self.assertEqual(live[0], "Oct 2026")
                rows = self.page.locator("#contract-rows strong")
                expect(rows).to_have_text(live[1:])
                for kind in ("chartPoints", "yieldChartPoints"):
                    self.assertEqual([p["label"] for p in self.chart_points(kind)], live[1:])
                expect(self.page.locator(f'[data-market-count="{key}"]')).to_have_text(
                    f"{len(live) - 1} maturities"
                )

    def test_market_left_without_prices_clears_its_charts(self):
        # Only Oct 2026 has a gold price, and it matures before the clock's day.
        snapshot = copy.deepcopy(self.snapshot)
        for contract in snapshot["markets"]["XAUTRY"]["contracts"][1:]:
            contract["last"] = None
        # A pixel ratio below 1, as at reduced zoom, must still clear the whole canvas.
        self.reopen_at(
            datetime.fromisoformat("2026-10-31T12:00:00+03:00"), device_scale_factor=0.5,
        )
        self.serve_snapshot(re.compile(r"/data/market\.json"), snapshot)
        self.open_dashboard("?market=USDTRY")
        self.page.locator('.market-options [data-market="XAUTRY"]').click()
        expect(self.page.locator("#contract-rows > tr")).to_have_count(2)
        self.assertEqual(self.chart_points(), [])
        self.assertEqual(self.chart_points("yieldChartPoints"), [])
        expect(self.page.locator("#curve-summary")).to_have_text(
            "2 listed maturities. None has a price to plot."
        )
        expect(self.page.locator("#yield-summary")).to_have_text(
            "No maturity has a yield to plot."
        )
        expect(self.page.locator("#yield-front-value")).to_have_text("—")
        for canvas in ("#curve-chart", "#yield-chart"):
            expect(self.page.locator(canvas)).to_have_attribute(
                "aria-label", re.compile("XAU/TRY.*no "),
            )
            blank = self.page.evaluate(
                f"""() => {{
                    const canvas = document.querySelector("{canvas}");
                    const pixels = canvas.getContext("2d")
                        .getImageData(0, 0, canvas.width, canvas.height).data;
                    return pixels.every((value) => value === 0);
                }}"""
            )
            self.assertTrue(blank, f"{canvas} still shows the previous market")

    def test_day_boundary_uses_istanbul_time(self):
        market = self.snapshot["markets"]["USDTRY"]
        stored = [c["days_to_maturity"] for c in market["contracts"]]
        # 20:30 UTC is 23:30 in Istanbul but already 8 Oct in Kiritimati;
        # 21:30 UTC is 00:30 on 8 Oct in Istanbul but still 7 Oct in Los Angeles.
        for now, zone, note, days in (
            ("2026-10-07T20:30:00+00:00", "Pacific/Kiritimati", None, stored),
            ("2026-10-07T21:30:00+00:00", "America/Los_Angeles", "08 Oct 2026",
             [d - 1 for d in stored]),
        ):
            with self.subTest(now=now, zone=zone):
                self.reopen_at(datetime.fromisoformat(now), zone)
                self.open_dashboard("?market=USDTRY")
                self.assert_note(note)
                self.assertEqual(self.column("#contract-rows", "Days left"), [str(d) for d in days])

    def test_archived_and_compared_closes_keep_stored_values(self):
        self.reopen_at(NEXT_DAY_TIME)
        day = "2026-10-06"
        archived = self.history[day]["markets"]["USDTRY"]
        self.open_day(day, "&market=USDTRY")
        self.assert_note(None)
        self.assertEqual(
            self.column("#contract-rows", "Days left"),
            [str(c["days_to_maturity"]) for c in archived["contracts"]],
        )
        for text, contract in zip(
            self.column("#contract-rows", "Daily yield · 1D"), archived["contracts"],
        ):
            self.assertAlmostEqual(self.number(text), contract["daily_yield_percent"], delta=1e-4)

        # Live is adjusted to 8 Oct, but the comparison keeps the 30 Sep values.
        compared = "2026-09-30"
        self.open_dashboard(f"?compare={compared}&market=USDTRY")
        self.assert_comparing(compared)
        self.assert_note("08 Oct 2026")
        past = {
            c["label"]: c["daily_yield_percent"]
            for c in self.history[compared]["markets"]["USDTRY"]["contracts"]
        }
        live = self.snapshot["markets"]["USDTRY"]
        for point in self.chart_points("yieldChartPoints"):
            self.assertAlmostEqual(point["compare"], past[point["label"]], places=9)
        front = live["contracts"][0]
        self.assertAlmostEqual(
            self.chart_points("yieldChartPoints")[0]["current"],
            ((front["last"] / live["spot"]["last"]) ** (1 / 22) - 1) * 100,
            places=9,
        )

    # Accessibility: axe-core audit of each rendered state

    def assert_accessible(self, state):
        """Fail with every serious or critical WCAG 2.1 A/AA violation on the page."""
        options = {
            "runOnly": {"type": "tag", "values": list(AXE_TAGS)},
            "rules": {rule: {"enabled": False} for rule in AXE_DISABLED_RULES},
            "resultTypes": ["violations"],
        }
        results = Axe().run(self.page, options=options).response
        failures = [
            f"{violation['id']} ({violation['impact']}) at "
            f"{' '.join(map(str, node['target']))}: {violation['help']} "
            f"<{violation['helpUrl']}>\n    "
            + "; ".join(check["message"] for key in ("any", "all", "none")
                        for check in node[key])
            for violation in results["violations"]
            if violation["impact"] in AXE_FAILING_IMPACTS
            for node in violation["nodes"]
        ]
        if failures:
            self.fail(f"{state}: {len(failures)} accessibility violation(s)\n"
                      + "\n".join(failures))

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_of_each_live_market(self):
        for key in self.snapshot["market_order"]:
            with self.subTest(market=key):
                self.open_dashboard(f"?market={key}")
                self.assert_market(key)
                self.assert_accessible(f"live {key}")

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_of_a_past_close(self):
        day = max(HISTORY_FIXTURE)
        self.open_day(day)
        self.assert_market(self.snapshot["default_market"], self.history[day])
        self.assert_accessible(f"past close {day}")

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_of_a_comparison_with_its_tooltip(self):
        compared = "2026-09-30"
        self.open_dashboard(f"?compare={compared}")
        self.assert_comparing(compared)
        self.hover("#curve-chart", self.chart_points()[0])
        expect(self.page.locator("#chart-tooltip")).to_be_visible()
        self.assert_accessible(f"comparison with {compared}")

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_of_each_yield_period(self):
        self.open_dashboard()
        for period in ("monthly", "annualized", "daily"):
            with self.subTest(period=period):
                self.page.locator(f'[data-yield-period="{period}"]').click()
                expect(
                    self.page.locator('[data-yield-period][aria-pressed="true"]')
                ).to_have_attribute("data-yield-period", period)
                self.assert_accessible(f"{period} yields")

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_on_a_narrow_mobile_viewport(self):
        self.reopen_at(FIXTURE_DAY_TIME, viewport=MOBILE_VIEWPORT)
        self.open_dashboard("?market=USDTRY")
        self.assert_market("USDTRY")
        self.assert_accessible("USDTRY at 390x844")

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_of_the_load_error(self):
        unknown = copy.deepcopy(self.snapshot)
        unknown["schema_version"] = 5
        self.serve_snapshot(re.compile(r"/data/market\.json"), unknown)
        self.page.goto(self.base_url)
        expect(self.page.locator("#data-state")).to_have_class(re.compile(r"\bis-error\b"))
        expect(self.page.locator("#contract-rows")).to_contain_text("could not be loaded")
        self.expect_load_errors(1)
        self.assert_accessible("load error")


if __name__ == "__main__":
    unittest.main()
