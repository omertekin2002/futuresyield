"""Headless-browser smoke test of the dashboard against a fixed snapshot.

Run with: python -m unittest tests/e2e/test_dashboard_smoke.py -v

Needs the packages in requirements-dev.txt and a Playwright Chromium
(`python -m playwright install chromium`). This directory has no
__init__.py, so `python -m unittest discover -s tests` never collects it.
"""

import copy
import csv
from datetime import date, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import parse_qsl, urlsplit

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
YIELD_PERIOD_LABELS = {
    "daily": "Daily · 1D",
    "monthly": "Monthly · 30D",
    "annualized": "Annualized · 365D",
}
# On this day each market still lists a Sep 2026 contract and not yet its
# last live maturity, so a comparison has contracts missing on either side.
ROLLED_DAY = "2026-07-07"
# The inspector's History table follows this contract. It is not listed yet
# on ROLLED_DAY, and on NO_PRICE_DAY it is listed without a last price.
HISTORY_SYMBOL = "USDTRYF2028"
NO_PRICE_DAY = "2026-09-07"
SPOT_PRICES = ("last", "open", "high", "low", "change")
CONTRACT_PRICES = ("last", "bid", "ask", "change", "expected_spot_at_maturity")

# The accessibility audit: axe-core's WCAG 2.1 A and AA rules, failing on any
# serious or critical violation.
AXE_TAGS = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa")
AXE_FAILING_IMPACTS = ("serious", "critical")
# Rules the audit turns off, each mapped to a one-line reason. None so far.
AXE_DISABLED_RULES = {}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
# Each chart's canvas, tooltip, keyboard marker, live region and the state
# list app.js keeps its points in.
KEYBOARD_CHARTS = (
    ("#curve-chart", "#chart-tooltip", "#curve-marker", "#curve-announcer", "chartPoints"),
    ("#yield-chart", "#yield-tooltip", "#yield-marker", "#yield-announcer", "yieldChartPoints"),
)
# The fixture's last trades that aren't on its newest quote day, 7 Oct, as the
# contract table shows them; each is stale. Every other contract shows its
# time alone, or "—" without one or with the old stand-in, the snapshot's own
# generated_at. In Tokyo, 6 Oct 18:07 and 22:58 are already 7 Oct, so a
# browser-zone day would wrongly clear those markers there.
STALE_TRADES = {
    "USDTRYN2027": "06 Oct 18:07",
    "USDTRYF2028": "05 Oct 17:54",
    "EURTRYZ2027": "06 Oct 22:58",
}
STALE_NOTE = ", last traded before the market’s latest quote day"
# Each chart's canvas, its stale-point key, its empty-chart message and the
# state list app.js keeps its points in.
STALE_CHARTS = (
    ("#curve-chart", "#curve-key", "#curve-empty", "chartPoints"),
    ("#yield-chart", "#yield-key", "#yield-empty", "yieldChartPoints"),
)
# The board CSV's columns: always, with a carry table (USD/TRY), and with a
# loaded comparison. "missing" always comes last.
CSV_COLUMNS = [
    "market", "view", "market_date", "snapshot_generated_at", "days_rolled_forward_from",
    "spot", "spot_unit", "spot_time", "spot_source",
    "maturity_month", "label", "symbol", "code", "maturity_date", "days_left",
    "last", "change_percent", "bid", "ask", "volume", "last_trade_at", "stale",
    "premium_percent", "yield_1d_percent", "yield_30d_percent", "yield_365d_percent",
]
CSV_CARRY_COLUMNS = ["expected_spot_at_maturity", "expected_short_profit_try"]
CSV_COMPARE_COLUMNS = [
    "compare_market_date", "compare_last", "compare_bid", "compare_ask",
    "compare_last_trade_at", "compare_stale", "compare_spot", "compare_premium_percent",
    "compare_days_left", "compare_yield_1d_percent", "compare_yield_30d_percent",
    "compare_yield_365d_percent", "delta_price", "delta_spot", "delta_premium_pp",
    "delta_days_left", "delta_yield_1d_pp", "delta_yield_30d_pp", "delta_yield_365d_pp",
]
# Each yield column of the CSV, the snapshot field it holds, its period and
# the yield period button that shows it in the chart.
CSV_YIELDS = (
    ("1d", "daily_yield_percent", 1, "daily"),
    ("30d", "monthly_yield_percent", 30, "monthly"),
    ("365d", "annualized_yield_percent", 365, "annualized"),
)
# Columns that are never a missing value: context, and the reasons themselves.
CSV_CONTEXT_COLUMNS = ("days_rolled_forward_from", "missing")
# Printing. The content width of an A4 portrait page with the stylesheet's
# 15 mm margins (210 mm - 30 mm, at 96 px per inch), and of US Letter's.
A4_CONTENT_WIDTH = 680
LETTER_CONTENT_WIDTH = 703
# The print header's rows that are always shown, in order.
PRINT_ROWS = ("Market", "View", "Yield period", "Page", "Printed")
# Interactive-only controls that print drops.
PRINT_HIDDEN = (
    "#refresh-button", "#history-control", "#compare-control", "#live-button",
    "#selection-clear", "#download-csv", "#print-page", "#chart-tooltip",
    "#yield-tooltip", "#curve-marker", "#yield-marker", ".period-toggle",
    ".select-button", '.market-options [aria-pressed="false"]',
)
# What a printout keeps: charts with their keys, tables, notes and disclaimer.
PRINT_VISIBLE = (
    "#print-header", "#curve-chart", "#yield-chart", "#curve-legend", "#yield-legend",
    "#curve-key", "#yield-key", "#carry", "#contracts", ".yield-footnote",
    "#carry-footnote", "#contracts .table-footnote", "footer .disclaimer",
    '.market-options [aria-pressed="true"]',
)
# A sensible ceiling for one market's printout: the header, sidebar and
# inspector, two charts, the carry table and the board.
MAX_PRINTED_PAGES = 8
NO_PRICE_MESSAGE = "No priced maturities to plot."
NO_YIELD_MESSAGE = (
    "No maturities with a yield to plot: every listed contract matures today or has no price."
)


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
            if contract.get("updated_at"):
                traded = datetime.fromisoformat(contract["updated_at"]) - timedelta(days=gap)
                contract["updated_at"] = traded.isoformat()
        if day == ROLLED_DAY:
            expired = copy.deepcopy(market["contracts"][0])
            expired["code"] = expired["code"].replace("1026", "0926")
            expired["label"] = "Sep 2026"
            expired["maturity_date"] = "2026-09-30"
            expired["days_to_maturity"] = (date(2026, 9, 30) - date.fromisoformat(day)).days
            expired["last"] *= 0.97
            market["contracts"] = [expired] + market["contracts"][:-1]
        for contract in market["contracts"]:
            if day == NO_PRICE_DAY and contract["symbol"] == HISTORY_SYMBOL:
                for key in ("last", "change", "change_percent", "premium_percent",
                            "annualized_premium_percent"):
                    contract[key] = None
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
        # A test that installs fake timers passes its time again: install()
        # without one starts from the real clock.
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

    def test_status_inside_the_refresh_window_keeps_the_90_minute_rule(self):
        # The fixture was generated on Wednesday 7 Oct at 14:06:12 Istanbul time.
        for now, state, text in (
            ("2026-10-07T14:30:00+03:00", "is-fresh", "Latest snapshot"),
            ("2026-10-07T15:36:12+03:00", "is-fresh", "Latest snapshot"),
            ("2026-10-07T15:40:00+03:00", "is-delayed", "Update delayed · 93m old"),
            ("2026-10-07T23:20:00+03:00", "is-delayed", "Update delayed · 553m old"),
        ):
            with self.subTest(now=now):
                self.open_live_at(now)
                self.assert_status(state, text)

    def test_status_after_hours_is_closed_once_the_last_refresh_landed(self):
        # The last scheduled refresh before each clock is 23:20 on the most
        # recent weekday, so a snapshot from 21:50 or later is on schedule.
        for now, generated_at in (
            ("2026-10-10T12:00:00+03:00", "2026-10-09T22:50:00+03:00"),  # Saturday
            ("2026-10-11T23:59:00+03:00", "2026-10-09T21:50:00+03:00"),  # Sunday
            ("2026-10-12T09:19:00+03:00", "2026-10-09T23:20:00+03:00"),  # Monday morning
            ("2026-10-08T02:00:00+03:00", "2026-10-07T23:05:00+03:00"),  # weeknight
            ("2026-10-07T23:59:00+03:00", "2026-10-07T22:20:00+03:00"),  # after 23:20
        ):
            with self.subTest(now=now):
                self.open_live_at(now, generated_at)
                label = datetime.fromisoformat(generated_at).strftime("%d %b, %H:%M")
                self.assert_status("is-closed", f"Market closed · snapshot {label} TRT")

    def test_status_after_hours_still_flags_a_missed_last_refresh(self):
        for now, generated_at, minutes in (
            ("2026-10-10T12:00:00+03:00", None, 4193),  # Saturday, Wednesday's snapshot
            ("2026-10-10T12:00:00+03:00", "2026-10-09T21:49:59+03:00", 850),
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
        self.page.clock.install(time=FIXTURE_DAY_TIME)
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
                expect(first.locator(".contract-name small")).to_contain_text(front["code"])
        page.locator("#contract-rows .select-button").first.click()
        expect(page.locator("#selection-label")).to_have_text(f"{front['label']} · {front['code']}")
        expect(page.locator("#selection-announcer")).to_have_text(
            f"Selected {front['label']}, {front['code']}."
        )

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

        for canvas, tooltip, _, announcer, _ in KEYBOARD_CHARTS:
            with self.subTest(keyboard=canvas):
                self.tab_to(canvas)
                expect(page.locator(f"{tooltip} strong")).to_have_text(front["label"])
                expect(page.locator(announcer)).to_contain_text(front["label"])

        containers = ("#contract-rows", "#carry-rows", "#chart-tooltip",
                      "#yield-tooltip", "#curve-legend", "#yield-legend",
                      "#curve-announcer", "#yield-announcer", "#selection-line",
                      "#selection-announcer")
        for container in containers:
            with self.subTest(container=container):
                expect(page.locator(f"{container} img")).to_have_count(0)
        self.assertIsNone(page.evaluate("() => window.__xss"))

    # Last trade per contract

    def expected_last_trades(self, snapshot, key, stale_trades=STALE_TRADES):
        """Each row's last-trade text, with its screen-reader words, and the stale labels."""
        texts, stale = [], []
        for contract in snapshot["markets"][key]["contracts"]:
            if contract["symbol"] in stale_trades:
                texts.append(f"Last trade {stale_trades[contract['symbol']]} stale{STALE_NOTE}")
                stale.append(contract["label"])
            elif contract["updated_at"] in (None, snapshot["generated_at"]):
                texts.append("Last trade —")
            else:
                # Every fixture time is stored in Istanbul time.
                texts.append(f"Last trade {contract['updated_at'][11:16]}")
        return texts, stale

    def assert_last_trades(self, texts, stale):
        rows = self.page.locator("#contract-rows > tr")
        expect(rows.locator(".last-trade")).to_have_count(len(texts))
        self.assertEqual(
            [" ".join(text.split()) for text in rows.locator(".last-trade").all_text_contents()],
            texts,
        )
        expect(rows.filter(has=self.page.locator(".stale-tag")).locator("strong")).to_have_text(stale)
        tags = rows.locator(".stale-tag")
        for index in range(tags.count()):
            expect(tags.nth(index)).to_be_visible()
            self.assertEqual(tags.nth(index).inner_text().split(",")[0].strip(), "STALE")

    def test_table_shows_each_last_trade_and_flags_stale_rows(self):
        usd = self.snapshot["markets"]["USDTRY"]["contracts"]
        for stand_in in (None, self.snapshot["generated_at"]):
            self.assertTrue(any(contract["updated_at"] == stand_in for contract in usd))
        for timezone_id in (None, "America/New_York", "Asia/Tokyo"):
            self.reopen_at(FIXTURE_DAY_TIME, timezone_id)
            for key in self.snapshot["market_order"]:
                with self.subTest(timezone=timezone_id, market=key):
                    self.open_dashboard(f"?market={key}")
                    self.assert_market(key)
                    self.assert_last_trades(*self.expected_last_trades(self.snapshot, key))

    def test_tooltips_and_live_region_give_the_last_trade(self):
        self.reopen_at(FIXTURE_DAY_TIME, "Asia/Tokyo")
        self.open_dashboard("?market=USDTRY")
        tooltip = self.page.locator("#chart-tooltip")
        self.hover_label("#curve-chart", "Jul 2027")
        expect(tooltip).to_contain_text("Last trade 06 Oct 18:07 · stale")
        self.hover_label("#curve-chart", "Oct 2026")
        expect(tooltip).to_contain_text("Last trade 13:49")
        expect(tooltip).not_to_contain_text("stale")
        self.hover_label("#yield-chart", "Aug 2027", "yieldChartPoints")
        expect(self.page.locator("#yield-tooltip")).to_contain_text("Last trade —")
        # Clear of the charts, so a scroll can't put the pointer back on one.
        self.page.mouse.move(0, 0)

        for chart in KEYBOARD_CHARTS:
            canvas, tooltip, _, announcer, _ = chart
            with self.subTest(chart=canvas):
                self.tab_to(canvas)
                # The hovers above left each chart's active point elsewhere.
                self.page.keyboard.press("Home")
                self.assert_keyboard_point(chart, "Oct 2026")
                expect(self.page.locator(announcer)).to_contain_text("23 days left. Last trade 13:49.")
                self.page.keyboard.press("End")
                self.assert_keyboard_point(chart, "Jan 2028")
                expect(self.page.locator(tooltip)).to_contain_text("Last trade 05 Oct 17:54 · stale")
                expect(self.page.locator(announcer)).to_contain_text("Last trade 05 Oct 17:54 · stale.")

    def test_past_close_and_comparison_give_the_day_on_screen(self):
        day = "2026-10-06"
        self.open_day(day, "&market=USDTRY")
        # The archived day's trades sit one day earlier, so the same rows are stale.
        texts, stale = self.expected_last_trades(
            self.history[day], "USDTRY",
            {"USDTRYN2027": "05 Oct 18:07", "USDTRYF2028": "04 Oct 17:54"},
        )
        self.assertEqual(stale, ["Jul 2027", "Jan 2028"])
        self.assert_last_trades(texts, stale)

        self.compare_select().select_option("2026-09-30")
        self.assert_comparing("2026-09-30", HISTORY_LABELS[day])
        tooltip = self.page.locator("#chart-tooltip")
        self.hover_label("#curve-chart", "Jul 2027")
        expect(tooltip).to_contain_text("Last trade 05 Oct 18:07 · stale")
        self.assertEqual(tooltip.inner_text().count("Last trade"), 1)

        # A maturity not listed on the day on screen has no last trade to give.
        self.compare_select().select_option(ROLLED_DAY)
        self.assert_comparing(ROLLED_DAY, HISTORY_LABELS[day])
        self.hover_label("#curve-chart", "Sep 2026")
        expect(tooltip).to_contain_text("Not listed on the viewed day")
        expect(tooltip).not_to_contain_text("Last trade")

    # Stale and empty charts

    def stale_points(self, kind):
        """The labels of the slots app.js flags stale, and of every compare-only slot."""
        return self.page.evaluate(
            f"""() => [
                state.{kind}.filter((point) => point.stale).map((point) => point.label),
                state.{kind}.filter((point) => !point.current).map((point) => point.stale),
            ]"""
        )

    def assert_stale_charts(self, market, stale_trades=STALE_TRADES):
        """Both charts flag exactly the stale contracts, with the key and labels to match."""
        stale = [c["label"] for c in market["contracts"] if c["symbol"] in stale_trades]
        plotted = len(market["contracts"])
        count = f"{len(stale)} of {plotted} maturities last traded before the latest quote day."
        for canvas, key, empty, kind in STALE_CHARTS:
            with self.subTest(chart=canvas):
                expect(self.page.locator(empty)).to_be_hidden()
                flagged, compare_only = self.stale_points(kind)
                self.assertEqual(flagged, stale)
                self.assertEqual(len(self.chart_points(kind)) - len(compare_only), plotted)
                self.assertFalse(any(compare_only), "a compare-only point is flagged stale")
                if stale:
                    expect(self.page.locator(key)).to_be_visible()
                    expect(self.page.locator(key)).to_have_text(f"Stale: {count}")
                    expect(self.page.locator(canvas)).to_have_attribute(
                        "aria-label", re.compile(re.escape(count)),
                    )
                else:
                    expect(self.page.locator(key)).to_be_hidden()
                    expect(self.page.locator(canvas)).not_to_have_attribute(
                        "aria-label", re.compile("last traded before"),
                    )

    def test_charts_flag_exactly_the_stale_points(self):
        # Tokyo puts 6 Oct 18:07 and 22:58 on 7 Oct; the flags must not move.
        for timezone_id in (None, "Asia/Tokyo"):
            self.reopen_at(FIXTURE_DAY_TIME, timezone_id)
            for key in self.snapshot["market_order"]:
                with self.subTest(timezone=timezone_id, market=key):
                    self.open_dashboard(f"?market={key}")
                    self.assert_market(key)
                    self.assert_stale_charts(self.snapshot["markets"][key])

    def test_stale_flags_follow_the_day_on_screen_never_the_comparison(self):
        day = "2026-10-06"
        self.open_day(day, "&market=USDTRY")
        self.assert_stale_charts(self.history[day]["markets"]["USDTRY"])
        # The comparison line keeps its squares: its slots are never flagged,
        # including the Sep 2026 contract listed only on the compared day.
        self.compare_select().select_option(ROLLED_DAY)
        self.assert_comparing(ROLLED_DAY, HISTORY_LABELS[day])
        self.assertIn("Sep 2026", [point["label"] for point in self.chart_points()])
        self.assert_stale_charts(self.history[day]["markets"]["USDTRY"])
        # The selection mark and the keyboard still reach a stale point.
        self.page.evaluate("() => setSelection('2027-07')")
        self.assertEqual(
            self.page.evaluate("() => state.chartPoints.find((point) => point.isSelected)?.label"),
            "Jul 2027",
        )
        self.tab_to("#curve-chart")
        self.page.keyboard.press("End")
        self.assert_keyboard_point(KEYBOARD_CHARTS[0], "Jan 2028")
        expect(self.page.locator("#chart-tooltip")).to_contain_text("· stale")

    def record_canvas_text(self):
        """Keep every string drawn on a canvas in window.__canvasText."""
        self.page.add_init_script(
            """(() => {
                window.__canvasText = [];
                const fillText = CanvasRenderingContext2D.prototype.fillText;
                CanvasRenderingContext2D.prototype.fillText = function (text, ...rest) {
                    window.__canvasText.push(String(text));
                    return fillText.call(this, text, ...rest);
                };
            })()"""
        )

    def peak_tag(self):
        """The yield chart's last drawn peak tag."""
        return self.page.evaluate(
            "() => window.__canvasText.filter((text) => text.startsWith('HIGHEST ')).at(-1)"
        )

    def test_stale_yield_peak_is_named_in_the_tag_summary_and_label(self):
        self.record_canvas_text()
        self.open_dashboard("?market=USDTRY")
        # The fixture's peak, Dec 2026, traded on the latest quote day.
        self.assertEqual(self.peak_tag(), "HIGHEST DAILY · +0.0792%")
        fresh = "Highest daily yield is +0.0792% at Dec 2026."
        expect(self.page.locator("#yield-summary")).to_contain_text(fresh)
        expect(self.page.locator("#yield-summary")).not_to_contain_text("stale")
        label = self.page.locator("#yield-chart").get_attribute("aria-label")
        self.assertIn(fresh, label)
        self.assertNotIn("which is stale", label)

        # Make the stale Jul 2027 contract the highest yield.
        snapshot = copy.deepcopy(self.snapshot)
        market = snapshot["markets"]["USDTRY"]
        july = next(c for c in market["contracts"] if c["symbol"] == "USDTRYN2027")
        july["last"] *= 1.15
        set_yields(july, market["spot"]["last"])
        self.serve_snapshot(re.compile(r"/data/market\.json"), snapshot)
        self.open_dashboard("?market=USDTRY")
        peak = self.page.evaluate(
            "() => state.yieldChartPoints.find((point) => point.isPeak)"
        )
        self.assertEqual(peak["label"], "Jul 2027")
        self.assertTrue(peak["stale"])
        daily = f"{july['daily_yield_percent']:+.4f}%"
        self.assertEqual(self.peak_tag(), f"HIGHEST DAILY · {daily} · STALE")
        note = (
            f"Highest daily yield is {daily} at Jul 2027, which is stale: "
            "it last traded before the latest quote day."
        )
        expect(self.page.locator("#yield-summary")).to_contain_text(note)
        expect(self.page.locator("#yield-chart")).to_have_attribute(
            "aria-label", re.compile(re.escape(note)),
        )

        self.page.locator('[data-yield-period="annualized"]').click()
        annualized = f"{july['annualized_yield_percent']:+.2f}%"
        self.assertEqual(self.peak_tag(), f"HIGHEST ANNUALIZED · {annualized} · STALE")
        expect(self.page.locator("#yield-summary")).to_contain_text(
            f"Highest annualized yield is {annualized} at Jul 2027, which is stale"
        )

    def unpriced_market(self, snapshot, key="XAUTRY"):
        """A copy of snapshot whose market key has no prices, and so no yields."""
        unpriced = copy.deepcopy(snapshot)
        market = unpriced["markets"][key]
        for contract in market["contracts"]:
            contract["last"] = None
            set_yields(contract, market["spot"]["last"])
        return unpriced

    def assert_empty_charts(self, empty=True):
        for canvas, key, message, _ in STALE_CHARTS:
            with self.subTest(chart=canvas, empty=empty):
                if empty:
                    expect(self.page.locator(message)).to_be_visible()
                    expect(self.page.locator(key)).to_be_hidden()
                    expect(self.page.locator(canvas)).to_have_attribute(
                        "aria-label", re.compile(r"XAU/TRY.*: no "),
                    )
                    expect(self.page.locator(canvas)).to_have_attribute(
                        "aria-label", re.compile(re.escape(self.page.locator(message).inner_text())),
                    )
                else:
                    expect(self.page.locator(message)).to_be_hidden()
        if empty:
            expect(self.page.locator("#curve-empty")).to_have_text(NO_PRICE_MESSAGE)
            expect(self.page.locator("#yield-empty")).to_have_text(NO_YIELD_MESSAGE)

    def test_empty_charts_say_why_and_clear_when_data_returns(self):
        self.serve_snapshot(re.compile(r"/data/market\.json"), self.unpriced_market(self.snapshot))
        self.open_dashboard("?market=XAUTRY")
        self.assert_empty_charts()
        # Each empty chart is still one Tab stop that Tab leaves.
        for canvas, *_ in STALE_CHARTS:
            self.tab_to(canvas)
            self.page.keyboard.press("Tab")
            self.assertFalse(self.page.evaluate(
                f"() => document.activeElement === document.querySelector('{canvas}')"
            ))
        self.page.locator('.market-options [data-market="USDTRY"]').click()
        self.assert_market("USDTRY")
        self.assert_empty_charts(empty=False)
        expect(self.page.locator("#curve-key")).to_be_visible()
        self.page.locator('.market-options [data-market="XAUTRY"]').click()
        self.assert_empty_charts()

        # With a comparison on, the message stays until the day on screen has data.
        self.page.evaluate("() => setCompare('2026-09-30')")
        expect(self.page.locator("#curve-legend")).to_be_visible()
        self.assert_empty_charts()

    def test_empty_chart_message_on_an_archived_close(self):
        day = "2026-10-06"
        self.serve_snapshot(
            re.compile(rf"/data/history/{day}\.json"), self.unpriced_market(self.history[day]),
        )
        self.open_day(day, "&market=XAUTRY")
        self.assert_empty_charts()
        self.page.locator('.market-options [data-market="EURTRY"]').click()
        self.assert_market("EURTRY", self.history[day])
        self.assert_empty_charts(empty=False)

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
        expect(self.page.locator("#curve-empty")).to_have_text(NO_PRICE_MESSAGE)
        expect(self.page.locator("#yield-empty")).to_have_text(NO_YIELD_MESSAGE)
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

    # Keyboard access to the chart points

    def tab_to(self, selector, limit=60):
        """Press Tab until selector has focus; return the ids focused on the way."""
        visited = []
        for _ in range(limit):
            self.page.keyboard.press("Tab")
            visited.append(self.page.evaluate("() => document.activeElement.id"))
            if self.page.evaluate(
                f"() => document.activeElement === document.querySelector('{selector}')"
            ):
                return visited
        self.fail(f"Tab never reached {selector}: {visited}")

    def watch_keys(self):
        """Record each keydown that reaches the window and whether it was handled.

        Calling it again clears the record; the listener is added only once, so
        each key is still recorded once.
        """
        self.page.evaluate(
            """() => {
                if (!window.__keys) {
                    window.addEventListener("keydown", (event) => {
                        window.__keys.push([event.key, event.defaultPrevented]);
                    });
                }
                window.__keys = [];
            }"""
        )

    def handled_keys(self):
        """Each key pressed since watch_keys, mapped to whether the page handled it."""
        return [tuple(key) for key in self.page.evaluate("() => window.__keys.splice(0)")]

    def assert_keyboard_point(self, chart, label):
        """The chart's tooltip and marker show the maturity label; return the point."""
        canvas, tooltip, marker, announcer, kind = chart
        points = [point for point in self.chart_points(kind) if point["label"] == label]
        self.assertEqual(len(points), 1, f"no single {label} point")
        expect(self.page.locator(tooltip)).to_be_visible()
        expect(self.page.locator(f"{tooltip} strong")).to_have_text(label)
        expect(self.page.locator(marker)).to_be_visible()
        expect(self.page.locator(announcer)).to_have_text(re.compile(rf"^{re.escape(label)}\. "))
        # The marker is centred on the point it names. Both boxes are read at
        # once, as the page may still be smooth-scrolling to the focused chart.
        offset = self.page.evaluate(
            f"""() => {{
                const box = document.querySelector("{canvas}").getBoundingClientRect();
                const ring = document.querySelector("{marker}").getBoundingClientRect();
                return [ring.x + ring.width / 2 - box.x, ring.y + ring.height / 2 - box.y];
            }}"""
        )
        self.assertAlmostEqual(offset[0], points[0]["x"], delta=1)
        self.assertAlmostEqual(offset[1], points[0]["y"], delta=1)
        return points[0]

    def assert_no_keyboard_point(self, chart):
        _, tooltip, marker, announcer, _ = chart
        expect(self.page.locator(tooltip)).to_be_hidden()
        expect(self.page.locator(marker)).to_be_hidden()
        expect(self.page.locator(announcer)).to_have_text("")

    def test_tab_reaches_both_charts_in_order_with_a_focus_indicator(self):
        self.open_dashboard("?market=USDTRY")
        before = self.tab_to("#curve-chart")
        self.assertNotIn("yield-chart", before)
        for chart in KEYBOARD_CHARTS:
            with self.subTest(chart=chart[0]):
                if chart is not KEYBOARD_CHARTS[0]:
                    self.tab_to(chart[0])
                outline = self.page.evaluate(
                    f"""() => {{
                        const style = getComputedStyle(document.querySelector("{chart[0]}"));
                        return [style.outlineStyle, style.outlineWidth, style.outlineColor];
                    }}"""
                )
                self.assertEqual(outline, ["solid", "3px", "rgb(0, 0, 238)"])
                self.assert_keyboard_point(chart, self.chart_points(chart[4])[0]["label"])

    def test_arrow_keys_step_through_each_chart_like_the_pointer(self):
        self.open_dashboard("?market=USDTRY")
        for chart in KEYBOARD_CHARTS:
            canvas, tooltip, marker, _, kind = chart
            with self.subTest(chart=canvas):
                points = self.chart_points(kind)
                self.tab_to(canvas)
                keyboard = []
                for index, point in enumerate(points):
                    if index:
                        self.page.keyboard.press("ArrowRight")
                    self.assert_keyboard_point(chart, point["label"])
                    keyboard.append(self.page.locator(tooltip).inner_text())
                pointer = []
                for point in points:
                    self.hover(canvas, point)
                    expect(self.page.locator(f"{tooltip} strong")).to_have_text(point["label"])
                    pointer.append(self.page.locator(tooltip).inner_text())
                self.assertEqual(keyboard, pointer)
                # The pointer takes over from the keyboard marker.
                expect(self.page.locator(marker)).to_be_hidden()

    def test_home_end_escape_and_blur(self):
        self.open_dashboard("?market=USDTRY")
        chart = KEYBOARD_CHARTS[0]
        labels = [point["label"] for point in self.chart_points()]
        self.tab_to(chart[0])
        self.watch_keys()
        scroll = self.page.evaluate("() => window.scrollY")
        for key, label in (("End", labels[-1]), ("ArrowRight", labels[-1]),
                           ("Home", labels[0]), ("ArrowLeft", labels[0])):
            with self.subTest(key=key):
                self.page.keyboard.press(key)
                self.assert_keyboard_point(chart, label)
        self.assertEqual(self.page.evaluate("() => window.scrollY"), scroll)
        self.assertEqual(
            self.handled_keys(),
            [("End", True), ("ArrowRight", True), ("Home", True), ("ArrowLeft", True)],
        )

        self.page.keyboard.press("Escape")
        self.assert_no_keyboard_point(chart)
        self.page.keyboard.press("ArrowRight")
        self.assert_keyboard_point(chart, labels[1])
        self.assertEqual(self.handled_keys(), [("Escape", True), ("ArrowRight", True)])

        # Other keys pass through, and Tab still leaves the chart.
        self.page.keyboard.press("ArrowDown")
        self.page.keyboard.press("Tab")
        self.assertEqual(self.handled_keys(), [("ArrowDown", False), ("Tab", False)])
        self.assertNotEqual(self.page.evaluate("() => document.activeElement.id"), "curve-chart")
        self.assert_no_keyboard_point(chart)

    def test_announces_the_point_and_comparison_as_text(self):
        self.open_dashboard("?market=USDTRY")
        self.choose_compare("2026-09-30")
        self.tab_to("#curve-chart")
        announcer = self.page.locator("#curve-announcer")
        expect(announcer).to_have_attribute("aria-live", "polite")
        # The same hand-computed values as the pointer tooltip test.
        expect(announcer).to_have_text(
            f"Oct 2026. {LIVE_LABEL}: 50.0830. 30 Sept 2026: 48.5805. "
            "Δ +1.5025 TRY per USD. 23 days left. Last trade 13:49."
        )
        self.assertEqual(self.page.evaluate("() => document.querySelector('#curve-announcer').children.length"), 0)
        expect(self.page.locator("#chart-tooltip")).to_contain_text("Δ +1.5025 TRY per USD")

    def test_yield_peak_note_is_shown_and_announced(self):
        self.open_dashboard("?market=USDTRY")
        chart = KEYBOARD_CHARTS[1]
        peak = self.page.evaluate("() => state.yieldChartPoints.findIndex((point) => point.isPeak)")
        points = self.chart_points("yieldChartPoints")
        self.tab_to("#yield-chart")
        for _ in range(peak):
            self.page.keyboard.press("ArrowRight")
        self.assert_keyboard_point(chart, points[peak]["label"])
        expect(self.page.locator("#yield-tooltip")).to_contain_text("Highest daily yield")
        expect(self.page.locator("#yield-announcer")).to_have_text(
            re.compile(r"\. Highest daily yield\.$")
        )
        self.page.keyboard.press("ArrowLeft" if peak else "ArrowRight")
        expect(self.page.locator("#yield-tooltip")).not_to_contain_text("Highest")
        expect(self.page.locator("#yield-announcer")).not_to_contain_text("Highest")

    def test_redraws_keep_the_keyboard_point_in_range(self):
        self.open_dashboard("?market=USDTRY")
        chart = KEYBOARD_CHARTS[1]
        self.tab_to("#yield-chart")
        self.page.keyboard.press("End")
        self.assert_keyboard_point(chart, "Jan 2028")

        # Leave for the period buttons just before the chart, and come back.
        self.page.keyboard.press("Shift+Tab")
        self.assert_no_keyboard_point(chart)
        expect(self.page.locator('[data-yield-period="annualized"]')).to_be_focused()
        self.page.keyboard.press("Enter")
        expect(self.page.locator("#yield-period-label")).to_have_text("Annualized · 365D")
        self.page.keyboard.press("Tab")
        point = self.assert_keyboard_point(chart, "Jan 2028")
        keyboard = self.page.locator("#yield-tooltip").inner_text()
        self.assertIn("365D", keyboard)

        # Redraws while the chart keeps focus: the same maturity stays active
        # with fresh values, or the nearest one that's still plotted.
        self.page.evaluate(f"() => setCompare('{ROLLED_DAY}')")
        self.assert_comparing(ROLLED_DAY)
        self.assert_keyboard_point(chart, "Jan 2028")
        expect(self.page.locator("#yield-tooltip")).to_contain_text("07 Jul 2026: not listed")
        self.page.evaluate("() => selectMarket('XAUTRY', true)")
        self.assert_market("XAUTRY")
        points = self.chart_points("yieldChartPoints")
        self.assert_keyboard_point(chart, points[-1]["label"])
        self.assertEqual(
            self.page.evaluate("() => charts.yield.activeIndex"), len(points) - 1,
        )
        expect(self.page.locator("#yield-chart")).to_be_focused()
        self.assertNotEqual(point, points[-1])

    def test_market_without_prices_ignores_chart_keys(self):
        snapshot = copy.deepcopy(self.snapshot)
        for contract in snapshot["markets"]["XAUTRY"]["contracts"][1:]:
            contract["last"] = None
        self.reopen_at(datetime.fromisoformat("2026-10-31T12:00:00+03:00"))
        self.serve_snapshot(re.compile(r"/data/market\.json"), snapshot)
        self.open_dashboard("?market=USDTRY")
        # A keyboard tooltip open when the market changes goes away with its points.
        self.tab_to("#curve-chart")
        self.page.keyboard.press("End")
        self.assert_keyboard_point(KEYBOARD_CHARTS[0], "Jan 2028")
        self.page.evaluate("() => selectMarket('XAUTRY', true)")
        self.assertEqual(self.chart_points(), [])
        self.assert_no_keyboard_point(KEYBOARD_CHARTS[0])

        self.page.locator('.market-options [data-market="XAUTRY"]').focus()
        self.watch_keys()
        for chart in KEYBOARD_CHARTS:
            with self.subTest(chart=chart[0]):
                self.tab_to(chart[0])
                for key in ("ArrowRight", "End", "Home", "ArrowLeft", "Escape"):
                    self.page.keyboard.press(key)
                    self.assert_no_keyboard_point(chart)
        self.assertTrue(
            all(not handled for key, handled in self.handled_keys() if key != "Tab"),
            "a chart without points handled a key",
        )

    # One selected maturity across the charts, the board and the carry table

    def contract(self, label, key="USDTRY"):
        return next(
            c for c in self.snapshot["markets"][key]["contracts"] if c["label"] == label
        )

    def point_position(self, label, kind="chartPoints"):
        points = [point for point in self.chart_points(kind) if point["label"] == label]
        self.assertEqual(len(points), 1, f"no single {label} point")
        return {"x": points[0]["x"], "y": points[0]["y"]}

    def click_point(self, canvas, label, kind="chartPoints"):
        self.page.locator(canvas).click(position=self.point_position(label, kind))

    def select_button(self, label, key="USDTRY"):
        code = self.contract(label, key)["code"]
        return self.page.get_by_role("button", name=f"Select {label} · {code}", exact=True)

    def selected_points(self, kind):
        return self.page.evaluate(
            f"() => state.{kind}.filter((point) => point.isSelected).map((point) => point.label)"
        )

    def assert_selected(self, label, key="USDTRY", price=True, yields=True):
        """label is selected everywhere; price and yields say which charts plot it."""
        page = self.page
        code = self.contract(label, key)["code"]
        rows = page.locator("#contract-rows > tr.is-selected")
        expect(rows).to_have_count(1)
        expect(rows.locator("strong")).to_have_text(label)
        pressed = page.locator('#contract-rows .select-button[aria-pressed="true"]')
        expect(pressed).to_have_count(1)
        expect(pressed).to_have_accessible_name(f"Select {label} · {code}")
        expect(page.locator('#contract-rows .select-button[aria-pressed="false"]')).to_have_count(
            len(self.snapshot["markets"][key]["contracts"]) - 1
        )
        expect(page.locator("#selection-line")).to_be_visible()
        expect(page.locator("#selection-line")).to_contain_text(f"Selected: {label} · {code}")
        expect(page.locator("#selection-announcer")).to_have_text(f"Selected {label}, {code}.")
        expect(page.locator("#inspector")).to_be_visible()
        expect(page.locator("#inspector-name")).to_have_text(label)
        expect(page.locator('#inspector [data-item="code"] .inspector-value')).to_have_text(code)
        if isinstance(self.snapshot["markets"][key].get("contract_size"), (int, float)):
            carry = page.locator("#carry-rows > tr.is-selected")
            expect(carry).to_have_count(1)
            expect(carry.locator("strong")).to_have_text(label)
        self.assertEqual(self.selected_points("chartPoints"), [label] if price else [])
        self.assertEqual(self.selected_points("yieldChartPoints"), [label] if yields else [])

    def assert_no_selection(self):
        page = self.page
        expect(page.locator("tr.is-selected")).to_have_count(0)
        expect(page.locator('.select-button[aria-pressed="true"]')).to_have_count(0)
        expect(page.locator("#selection-line")).to_be_hidden()
        expect(page.locator("#inspector")).to_be_hidden()
        self.assertEqual(self.selected_points("chartPoints"), [])
        self.assertEqual(self.selected_points("yieldChartPoints"), [])

    def test_clicking_a_price_point_selects_it_everywhere(self):
        self.open_dashboard("?market=USDTRY")
        self.assert_no_selection()
        self.click_point("#curve-chart", "Dec 2026")
        self.assert_selected("Dec 2026")
        # A click away from every point leaves the selection alone.
        self.page.locator("#curve-chart").click(position={"x": 5, "y": 5})
        self.assert_selected("Dec 2026")
        self.click_point("#curve-chart", "Dec 2026")
        self.assert_no_selection()
        expect(self.page.locator("#selection-announcer")).to_have_text("Selection cleared.")

    def test_clicking_a_yield_point_selects_it_and_moves_the_selection(self):
        self.open_dashboard("?market=USDTRY")
        self.click_point("#yield-chart", "Mar 2027", "yieldChartPoints")
        self.assert_selected("Mar 2027")
        self.click_point("#yield-chart", "Jan 2028", "yieldChartPoints")
        self.assert_selected("Jan 2028")

    def test_board_button_selects_and_clear_button_clears(self):
        self.open_dashboard("?market=USDTRY")
        button = self.select_button("Dec 2026")
        expect(button).to_have_attribute("aria-pressed", "false")
        button.click()
        self.assert_selected("Dec 2026")
        expect(button).to_be_focused()
        button.click()
        self.assert_no_selection()
        self.select_button("Jul 2027").click()
        self.assert_selected("Jul 2027")
        clear = self.page.get_by_role("button", name="Clear selection", exact=True)
        clear.click()
        self.assert_no_selection()
        # The line hides with its button, so focus moves to the charts' heading.
        expect(self.page.locator("#curve-title")).to_be_focused()

    def test_enter_and_space_on_a_focused_chart_select_the_active_point(self):
        self.open_dashboard("?market=USDTRY")
        price, yields = KEYBOARD_CHARTS
        self.tab_to("#curve-chart")
        self.watch_keys()
        self.page.keyboard.press("ArrowRight")
        self.page.keyboard.press("ArrowRight")
        self.assert_keyboard_point(price, "Dec 2026")
        self.page.keyboard.press("Enter")
        self.assert_selected("Dec 2026")
        # Escape still only hides the keyboard tooltip.
        self.page.keyboard.press("Escape")
        self.assert_no_keyboard_point(price)
        self.assert_selected("Dec 2026")
        self.page.keyboard.press(" ")
        self.assert_keyboard_point(price, "Dec 2026")
        self.assert_no_selection()
        self.assertEqual(
            self.handled_keys(),
            [("ArrowRight", True), ("ArrowRight", True), ("Enter", True),
             ("Escape", True), (" ", True)],
        )

        self.tab_to("#yield-chart")
        self.page.keyboard.press("End")
        self.page.keyboard.press(" ")
        self.assert_keyboard_point(yields, "Jan 2028")
        self.assert_selected("Jan 2028")

        # A held key toggles once; its repeats are still handled but ignored.
        # The selection is checked after every keydown, as three toggles would
        # end where one does.
        self.watch_keys()
        for key, check in (("Enter", self.assert_no_selection),
                           (" ", lambda: self.assert_selected("Jan 2028"))):
            for _ in range(3):
                self.page.keyboard.down(key)
                check()
            self.page.keyboard.up(key)
        self.assertEqual(self.handled_keys(), [("Enter", True)] * 3 + [(" ", True)] * 3)

    def test_tap_selects_a_point_on_a_phone(self):
        self.reopen_at(
            FIXTURE_DAY_TIME, viewport=MOBILE_VIEWPORT, has_touch=True, is_mobile=True,
        )
        self.open_dashboard("?market=USDTRY")
        for canvas, kind, label in (("#curve-chart", "chartPoints", "Nov 2026"),
                                    ("#yield-chart", "yieldChartPoints", "Apr 2027")):
            with self.subTest(chart=canvas):
                self.page.locator(canvas).scroll_into_view_if_needed()
                self.page.locator(canvas).tap(position=self.point_position(label, kind))
                self.assert_selected(label)
        self.page.locator("#yield-chart").tap(
            position=self.point_position("Apr 2027", "yieldChartPoints")
        )
        self.assert_no_selection()

    def test_selection_survives_refreshes_and_toggles_until_the_market_changes(self):
        live_requests = []
        self.page.on(
            "request",
            lambda request: "data/market.json" in request.url
            and live_requests.append(request.url),
        )
        self.page.clock.install(time=FIXTURE_DAY_TIME)
        self.open_dashboard("?market=USDTRY")
        self.select_button("Dec 2026").click()
        self.assert_selected("Dec 2026")

        # The poll redraws the board with fresh rows and keeps the selection.
        self.page.evaluate(
            "() => { document.querySelector('#contract-rows tr').__old = true;"
            " document.querySelector('#inspector').__old = true;"
            " document.querySelector('#inspector dl > div').__old = true; }"
        )
        self.page.clock.run_for("01:01")
        self.page.wait_for_function("() => !document.querySelector('#contract-rows tr').__old")
        self.assertEqual(len(live_requests), 2)
        self.assert_selected("Dec 2026")
        # The inspector stays in place and its rows are refilled.
        self.assertTrue(self.page.evaluate(
            "() => document.querySelector('#inspector').__old === true"
            " && !document.querySelector('#inspector dl > div').__old"
        ))
        # The new row's button takes over the focus of the one it replaced.
        expect(self.select_button("Dec 2026")).to_be_focused()

        self.page.locator('[data-yield-period="monthly"]').click()
        expect(self.page.locator("#yield-period-label")).to_have_text("Monthly · 30D")
        self.assert_selected("Dec 2026")
        self.choose_compare(ROLLED_DAY)
        self.assert_selected("Dec 2026")
        # A maturity listed only on the comparison day can't be selected.
        self.click_point("#curve-chart", "Sep 2026")
        self.assert_selected("Dec 2026")
        self.compare_select().select_option("")
        self.assert_not_comparing()
        self.assert_selected("Dec 2026")

        self.page.locator('.market-options [data-market="EURTRY"]').click()
        self.assert_market("EURTRY")
        self.assert_no_selection()
        expect(self.page.locator("#selection-announcer")).to_have_text("Selection cleared.")
        self.page.locator('.market-options [data-market="USDTRY"]').click()
        self.assert_market("USDTRY")
        self.assert_no_selection()

    def test_selection_without_a_yield_and_after_it_matures(self):
        # Oct 2026 matures today: it has a price but no yield to plot.
        maturity_day = datetime.fromisoformat("2026-10-30T12:00:00+03:00")
        self.reopen_at(maturity_day)
        self.page.clock.install(time=maturity_day)
        self.open_dashboard("?market=USDTRY")
        self.select_button("Oct 2026").click()
        self.assert_selected("Oct 2026", yields=False)
        self.page.locator('[data-yield-period="annualized"]').click()
        self.assert_selected("Oct 2026", yields=False)

        # The next day's poll rolls the snapshot forward and drops it.
        self.page.clock.set_fixed_time(datetime.fromisoformat("2026-10-31T12:00:00+03:00"))
        self.page.clock.run_for("01:01")
        self.assert_note("31 Oct 2026")
        expect(self.page.locator("#contract-rows strong").first).to_have_text("Nov 2026")
        self.assert_no_selection()
        expect(self.page.locator("#selection-announcer")).to_have_text("Selection cleared.")

    # The inspector for the selected maturity

    def inspector(self):
        """Each inspector row by data-item: its term, value text, stale flag and detail."""
        return self.page.evaluate(
            """() => Object.fromEntries(
                [...document.querySelectorAll('#inspector [data-item]')].map((row) => {
                    const value = row.querySelector('.inspector-value');
                    return [row.dataset.item, {
                        term: row.querySelector('dt').firstChild.textContent,
                        tag: row.querySelector('.inspector-tag')?.textContent ?? '',
                        value: value.firstChild?.textContent ?? '',
                        stale: Boolean(value.querySelector('.stale-tag')),
                        detail: row.querySelector('.inspector-detail')?.textContent ?? '',
                    }];
                }))"""
        )

    def inspector_values(self):
        return {key: row["value"] for key, row in self.inspector().items()}

    def assert_no_sideways_scroll(self):
        self.assertLessEqual(
            self.page.evaluate("() => document.documentElement.scrollWidth"),
            self.page.evaluate("() => window.innerWidth"),
        )

    def expected_inspector(self, contract, spot, days, trend=None, size=None, digits=4):
        """The inspector values and formulas, hand-computed from snapshot inputs."""
        price, spot_text = f"{contract['last']:,.{digits}f}", f"{spot:,.{digits}f}"
        factor = (contract["last"] / spot) ** (1 / days)
        values = {
            "days": str(days),
            "last": price,
            "spot": spot_text,
            "premium": f"{(contract['last'] / spot - 1) * 100:+.2f}%",
            "factor": f"{factor:.8f}",
            "yield-daily": f"{(factor - 1) * 100:+.4f}%",
            "yield-monthly": f"{(factor ** 30 - 1) * 100:+.2f}%",
            "yield-annualized": f"{(factor ** 365 - 1) * 100:+.2f}%",
        }
        details = {
            "premium": f"({price} / {spot_text} − 1) × 100",
            "factor": f"({price} / {spot_text})^(1 / {days})",
            "yield-daily": f"({factor:.8f}^1 − 1) × 100",
            "yield-monthly": f"({factor:.8f}^30 − 1) × 100",
            "yield-annualized": f"({factor:.8f}^365 − 1) × 100",
        }
        if trend:
            expected_spot = spot * trend["daily_factor"] ** days
            profit = (contract["bid"] - expected_spot) * size
            values["trend-factor"] = f"{trend['daily_factor']:.8f}"
            values["expected-spot"] = f"{expected_spot:,.4f}"
            values["profit"] = f"{profit:+,.0f} TRY"
            details["expected-spot"] = f"{spot_text} × {trend['daily_factor']:.8f}^{days}"
            details["profit"] = f"({contract['bid']:,.4f} − {expected_spot:,.4f}) × {size:,}"
        return values, details

    def assert_inspector(self, values, details):
        rows = self.inspector()
        for key, value in values.items():
            with self.subTest(item=key):
                self.assertEqual(rows[key]["value"], value)
        for key, detail in details.items():
            with self.subTest(detail=key):
                self.assertEqual(rows[key]["detail"], detail)

    def test_inspector_fills_in_the_usdtry_arithmetic(self):
        self.open_dashboard("?market=USDTRY")
        expect(self.page.locator("#inspector")).to_be_hidden()
        self.select_button("Oct 2026").click()
        self.assert_selected("Oct 2026")
        market = self.snapshot["markets"]["USDTRY"]
        contract = self.contract("Oct 2026")
        values, details = self.expected_inspector(
            contract, market["spot"]["last"], 23, market["spot_trend"], market["contract_size"],
        )
        # Hand-computed from tests/fixtures/market.json at the displayed precision.
        self.assertEqual(values, {
            "days": "23", "last": "50.0830", "spot": "49.1949", "premium": "+1.81%",
            "factor": "1.00077824", "yield-daily": "+0.0778%", "yield-monthly": "+2.36%",
            "yield-annualized": "+32.84%", "trend-factor": "1.00054111",
            "expected-spot": "49.8108", "profit": "+271 TRY",
        })
        self.assert_inspector({
            **values,
            "label": "Oct 2026",
            "symbol": "USDTRYV2026",
            "code": "F_USDTRY1026",
            "maturity": "30 Oct 2026",
            "bid-ask": "50.0820 / 50.0830",
            "last-trade": "13:49",
            "spot-time": "07 Oct, 14:00 TRT",
            "spot-source": "TradingView intraday via borsapy FX",
            "trend-window": "10 Apr 2026 → 07 Oct 2026",
        }, {
            **details,
            "days": "Calendar days from 07 Oct 2026",
            "spot": "TRY per USD",
            "trend-window": "180 days",
            "trend-factor": "(49.1949 / 44.6304)^(1 / 180)",
        })
        expect(self.page.locator("#inspector-day")).to_have_text(f"Live · {LIVE_LABEL}")
        expect(self.page.locator("#inspector-note")).to_be_hidden()
        self.assertFalse(self.inspector()["last-trade"]["stale"])

        # The yield period shown in the chart is marked, and follows the toggle.
        def tagged():
            return [key for key, row in self.inspector().items() if row["tag"]]
        self.assertEqual(tagged(), ["yield-daily"])
        self.page.locator('[data-yield-period="annualized"]').click()
        self.assertEqual(tagged(), ["yield-annualized"])
        self.assertEqual(self.inspector()["yield-annualized"]["tag"], "In chart")

        # The region is labelled by its heading, with one list per group.
        region = self.page.get_by_role("region", name="Inspector · Oct 2026")
        expect(region).to_be_visible()
        expect(region.locator("dl")).to_have_count(5)

    def test_inspector_agrees_with_the_board_and_tooltips(self):
        self.open_dashboard("?market=USDTRY")
        market = self.snapshot["markets"]["USDTRY"]
        labels = [c["label"] for c in market["contracts"]]
        for label in ("Dec 2026", "Jul 2027", "Aug 2027", "Jan 2028"):
            with self.subTest(contract=label):
                self.select_button(label).click()
                self.assert_selected(label)
                rows = self.inspector()
                index = labels.index(label)
                row = self.page.locator("#contract-rows > tr").nth(index)
                cell = lambda name: row.locator(f'td[data-label="{name}"]').inner_text().strip()
                self.assertEqual(rows["last"]["value"], row.locator(".last-price").inner_text())
                self.assertEqual(rows["bid-ask"]["value"], cell("Bid / Ask"))
                self.assertEqual(rows["days"]["value"], cell("Days left"))
                self.assertEqual(rows["premium"]["value"], cell("vs. spot"))
                self.assertEqual(rows["yield-daily"]["value"], cell("Daily yield · 1D"))
                self.assertEqual(rows["yield-monthly"]["value"], cell("Monthly yield · 30D"))
                self.assertEqual(rows["yield-annualized"]["value"], cell("Annualized · 365D"))
                code = market["contracts"][index]["symbol"]
                self.assertEqual(rows["last-trade"]["stale"], code in STALE_TRADES)
                if code in STALE_TRADES:
                    self.assertEqual(rows["last-trade"]["value"], STALE_TRADES[code])

                carry = self.page.locator("#carry-rows > tr").nth(index)
                carry_cell = lambda name: carry.locator(f'td[data-label="{name}"]').inner_text().strip()
                self.assertEqual(rows["expected-spot"]["value"], carry_cell("Exp. spot at maturity"))
                profit = carry_cell("Exp. profit · TRY")
                self.assertEqual(rows["profit"]["value"], "—" if profit == "—" else f"{profit} TRY")

                # Each chart's tooltip shows the same price and yield.
                self.hover_label("#curve-chart", label)
                expect(self.page.locator("#chart-tooltip")).to_contain_text(
                    f"{rows['last']['value']} · TRY per USD"
                )
                expect(self.page.locator("#chart-tooltip")).to_contain_text(
                    f"{rows['days']['value']} days left"
                )
                self.hover_label("#yield-chart", label, "yieldChartPoints")
                expect(self.page.locator("#yield-tooltip")).to_contain_text(
                    f"{rows['yield-daily']['value']} 1D"
                )

        self.page.locator('[data-yield-period="monthly"]').click()
        self.hover_label("#yield-chart", "Jan 2028", "yieldChartPoints")
        expect(self.page.locator("#yield-tooltip")).to_contain_text(
            f"{self.inspector()['yield-monthly']['value']} 30D"
        )

    def test_inspector_states_why_values_are_missing(self):
        self.open_dashboard("?market=USDTRY")
        self.select_button("Aug 2027").click()
        rows = self.inspector()
        self.assertEqual(rows["bid-ask"]["value"], "—")
        self.assertEqual(rows["last-trade"]["value"], "—")
        self.assertFalse(rows["last-trade"]["stale"])
        self.assertEqual(rows["profit"]["value"], "—")
        self.assertEqual(
            rows["profit"]["detail"], "No profit: the contract has no bid in this snapshot."
        )
        # The expected spot doesn't need a bid, so it is still shown.
        contract = self.contract("Aug 2027")
        self.assertEqual(rows["expected-spot"]["value"], f"{contract['expected_spot_at_maturity']:,.4f}")

        # On its maturity day a contract has 0 days left and no yield.
        self.reopen_at(datetime.fromisoformat("2026-10-30T12:00:00+03:00"))
        self.open_dashboard("?market=USDTRY")
        self.select_button("Oct 2026").click()
        self.assert_selected("Oct 2026", yields=False)
        rows = self.inspector()
        self.assertEqual(rows["days"]["value"], "0")
        self.assertEqual(rows["factor"]["value"], "—")
        self.assertEqual(
            rows["factor"]["detail"], "No yield: the contract matures today, with 0 days left."
        )
        for period in ("daily", "monthly", "annualized"):
            self.assertEqual(rows[f"yield-{period}"]["value"], "—")
            self.assertEqual(rows[f"yield-{period}"]["detail"], "No yield without a daily factor.")
        # The premium needs no days left.
        self.assertEqual(rows["premium"]["value"], "+1.81%")

    def test_inspector_on_a_later_istanbul_day_uses_the_adjusted_days(self):
        self.reopen_at(NEXT_DAY_TIME)
        self.open_dashboard("?market=USDTRY")
        self.assert_note("08 Oct 2026")
        self.select_button("Oct 2026").click()
        self.assert_selected("Oct 2026")
        market = self.snapshot["markets"]["USDTRY"]
        values, details = self.expected_inspector(
            self.contract("Oct 2026"), market["spot"]["last"], 22,
            market["spot_trend"], market["contract_size"],
        )
        self.assert_inspector(values, {
            **details, "days": "Calendar days from 08 Oct 2026, adjusted",
        })
        note = self.page.locator("#inspector-note")
        expect(note).to_be_visible()
        expect(note).to_have_text(
            f"Days left and yields adjusted to 08 Oct 2026; prices are from the {LIVE_LABEL} snapshot."
        )

    def test_inspector_of_an_archived_close_shows_its_stored_values(self):
        day = "2026-10-06"
        self.open_day(day, "&market=USDTRY")
        self.select_button("Dec 2026").click()
        self.assert_selected("Dec 2026")
        market = self.history[day]["markets"]["USDTRY"]
        contract = next(c for c in market["contracts"] if c["label"] == "Dec 2026")
        expect(self.page.locator("#inspector-day")).to_have_text(f"Close · {HISTORY_LABELS[day]}")
        expect(self.page.locator("#inspector-note")).to_be_hidden()
        self.assert_inspector({
            "days": str(contract["days_to_maturity"]),
            "last": f"{contract['last']:,.4f}",
            "spot": f"{market['spot']['last']:,.4f}",
            "premium": f"{contract['premium_percent']:+.2f}%",
            "factor": f"{contract['daily_yield_factor']:.8f}",
            "yield-daily": f"{contract['daily_yield_percent']:+.4f}%",
            "yield-monthly": f"{contract['monthly_yield_percent']:+.2f}%",
            "yield-annualized": f"{contract['annualized_yield_percent']:+.2f}%",
            "expected-spot": f"{contract['expected_spot_at_maturity']:,.4f}",
            "profit": f"{contract['expected_short_profit_try']:+,.0f} TRY",
        }, {"days": f"Calendar days from {HISTORY_LABELS[day]}"})

    def test_inspector_labels_each_market_spot(self):
        cases = (
            ("EURTRY", "Dec 2026", "TradingView intraday via borsapy FX", "TRY per EUR"),
            ("XAUTRY", "Dec 2026", "Synthetic XAU/USD × USD/TRY gram reference", "TRY per gram"),
        )
        for key, label, source, unit in cases:
            with self.subTest(market=key):
                self.open_dashboard(f"?market={key}")
                self.select_button(label, key).click()
                self.assert_selected(label, key)
                rows = self.inspector()
                self.assertEqual(rows["spot-source"]["value"], source)
                self.assertEqual(rows["spot"]["detail"], unit)
                expect(self.page.locator("#inspector-carry-group")).to_be_hidden()
                self.assertNotIn("profit", rows)
                market = self.snapshot["markets"][key]
                contract = self.contract(label, key)
                self.assert_inspector(*self.expected_inspector(
                    contract, market["spot"]["last"], contract["days_to_maturity"],
                    digits=market["price_digits"],
                ))

    # The inspector's comparison with an archived close

    def comparison(self):
        """Each comparison row by data-compare-item: header, tag and each column's text."""
        return self.page.evaluate(
            """() => Object.fromEntries(
                [...document.querySelectorAll('#inspector-compare-rows tr[data-compare-item]')]
                .map((row) => {
                    const cell = (index) => {
                        const td = row.querySelectorAll('td')[index];
                        const value = td.querySelector('.inspector-value');
                        return {
                            value: value.firstChild?.textContent ?? '',
                            stale: Boolean(value.querySelector('.stale-tag')),
                            detail: td.querySelector('.inspector-detail')?.textContent ?? '',
                        };
                    };
                    return [row.dataset.compareItem, {
                        term: row.querySelector('th').firstChild.textContent,
                        tag: row.querySelector('th .inspector-tag')?.textContent ?? '',
                        current: cell(0),
                        compared: cell(1),
                        change: cell(2).value,
                    }];
                }))"""
        )

    def comparison_texts(self, part):
        """One column of the comparison, by row: 'current', 'compared' or 'change'."""
        return {
            key: row[part] if part == "change" else row[part]["value"]
            for key, row in self.comparison().items()
        }

    def expected_comparison(self, current, past, period="daily", unit="TRY per USD", digits=4,
                            current_days=None, current_yield=None):
        """Both sides' values and the changes, hand-computed from the two snapshots.

        current and past are (market, contract) pairs; current_days and
        current_yield override the day on screen's stored values when it has
        been rolled forward.
        """
        (now_market, now), (then_market, then) = current, past
        _, field, _, yield_digits = next(c for c in YIELD_COLUMNS if c[1].startswith(period))
        now_spot, then_spot = now_market["spot"]["last"], then_market["spot"]["last"]
        now_days = now["days_to_maturity"] if current_days is None else current_days
        now_yield = now[field] if current_yield is None else current_yield
        price = lambda value: f"{value:,.{digits}f}"
        values = {
            "current": {
                "last": price(now["last"]),
                "spot": price(now_spot),
                "premium": f"{now['premium_percent']:+.2f}%",
                "days": str(now_days),
                "yield": f"{now_yield:+.{yield_digits}f}%",
            },
            "compared": {
                "last": price(then["last"]),
                "spot": price(then_spot),
                "premium": f"{then['premium_percent']:+.2f}%",
                "days": str(then["days_to_maturity"]),
                "yield": f"{then[field]:+.{yield_digits}f}%",
            },
            "change": {
                "last": f"{now['last'] - then['last']:+,.{digits}f} {unit}",
                "spot": f"{now_spot - then_spot:+,.{digits}f} {unit}",
                "premium": f"{now['premium_percent'] - then['premium_percent']:+.2f} pp",
                "days": f"{now_days - then['days_to_maturity']:+d} days",
                "yield": f"{now_yield - then[field]:+.{yield_digits}f} pp",
                "bid-ask": "",
                "last-trade": "",
                "spot-time": "",
            },
        }
        return values

    def assert_comparison(self, expected):
        for part, values in expected.items():
            texts = self.comparison_texts(part)
            for key, value in values.items():
                with self.subTest(part=part, item=key):
                    self.assertEqual(texts[key], value)

    def history_contract(self, day, label, key="USDTRY"):
        market = self.history[day]["markets"][key]
        return market, next(c for c in market["contracts"] if c["label"] == label)

    def assert_comparison_hidden(self):
        expect(self.page.locator("#inspector-compare-group")).to_be_hidden()
        self.assertEqual(self.comparison(), {})

    def assert_tooltip_delta(self, label, change):
        """The yield tooltip for label shows the same change as the inspector."""
        self.hover_label("#yield-chart", label, "yieldChartPoints")
        tooltip = self.page.locator("#yield-tooltip")
        expect(tooltip).to_be_visible()
        expect(tooltip).to_contain_text(f"Δ {change}")

    def test_inspector_compares_both_days_inputs_and_the_change(self):
        compared = "2026-09-30"
        self.open_dashboard("?market=USDTRY")
        self.select_button("Oct 2026").click()
        self.assert_selected("Oct 2026")
        # Without a comparison there is no Comparison group.
        self.assert_comparison_hidden()

        self.choose_compare(compared)
        group = self.page.locator("#inspector-compare-group")
        expect(group).to_be_visible()
        live = self.snapshot["markets"]["USDTRY"]
        expected = self.expected_comparison(
            (live, self.contract("Oct 2026")), self.history_contract(compared, "Oct 2026"),
        )
        # Hand-computed: 50.083 live against 50.083 × 0.97 on 30 Sep, and the
        # spot scaled alike, so the premium is unchanged and only the extra
        # 7 days to maturity move the yield.
        self.assertEqual(expected["change"]["last"], "+1.5025 TRY per USD")
        self.assertEqual(expected["change"]["spot"], "+1.4758 TRY per USD")
        self.assertEqual(expected["change"]["premium"], "+0.00 pp")
        self.assertEqual(expected["change"]["days"], "-7 days")
        self.assert_comparison(expected)
        rows = self.comparison()
        self.assertEqual(rows["date"]["current"]["value"], LIVE_LABEL)
        self.assertEqual(rows["date"]["compared"]["value"], HISTORY_LABELS[compared])
        self.assertEqual(rows["date"]["change"], "7 calendar days apart")
        self.assertEqual(rows["bid-ask"]["current"]["value"], "50.0820 / 50.0830")
        past = self.history_contract(compared, "Oct 2026")[1]
        self.assertEqual(
            rows["bid-ask"]["compared"]["value"], f"{past['bid']:,.4f} / {past['ask']:,.4f}",
        )
        self.assertEqual(rows["last-trade"]["current"]["value"], "13:49")
        self.assertEqual(rows["last-trade"]["compared"]["value"], "13:49")
        self.assertFalse(rows["last-trade"]["compared"]["stale"])
        # derive_day() keeps each spot's time, so both read 07 Oct.
        self.assertEqual(rows["spot-time"]["current"]["value"], "07 Oct, 14:00 TRT")
        self.assertEqual(rows["spot-time"]["compared"]["value"], "07 Oct, 14:00 TRT")
        self.assertEqual(rows["yield"]["term"], "Daily yield · 1D")
        self.assertEqual(rows["yield"]["tag"], "In chart")
        expect(self.page.locator("#inspector-compare-note")).to_have_text(
            f"Change is the day on screen minus the close of {HISTORY_LABELS[compared]}, "
            "whose values are shown as that close stored them."
        )

        # A real table with column and row headers.
        table = self.page.get_by_role("table", name="Comparison")
        expect(table).to_be_visible()
        expect(table.get_by_role("columnheader")).to_have_text(
            ["Input", f"Live · {LIVE_LABEL}", f"Close · {HISTORY_LABELS[compared]}", "Change"]
        )
        expect(table.get_by_role("rowheader")).to_have_count(9)

        # Δ yield matches the tooltip, and follows the yield period.
        self.assert_tooltip_delta("Oct 2026", rows["yield"]["change"])
        for period in ("annualized", "monthly"):
            with self.subTest(period=period):
                self.page.locator(f'[data-yield-period="{period}"]').click()
                expected = self.expected_comparison(
                    (live, self.contract("Oct 2026")),
                    self.history_contract(compared, "Oct 2026"), period,
                )
                self.assert_comparison({part: {"yield": values["yield"]}
                                        for part, values in expected.items()})
                term = {"annualized": "Annualized yield · 365D", "monthly": "Monthly yield · 30D"}
                self.assertEqual(self.comparison()["yield"]["term"], term[period])
                self.assert_tooltip_delta("Oct 2026", self.comparison()["yield"]["change"])

        # A stale last trade on the compared close keeps the board's tag, and
        # a contract without a trade time shows "—" on both days, never stale.
        self.select_button("Jan 2028").click()
        rows = self.comparison()
        self.assertEqual(rows["last-trade"]["current"]["value"], "05 Oct 17:54")
        self.assertTrue(rows["last-trade"]["current"]["stale"])
        self.assertEqual(rows["last-trade"]["compared"]["value"], "28 Sept 17:54")
        self.assertTrue(rows["last-trade"]["compared"]["stale"])
        self.select_button("Aug 2027").click()
        rows = self.comparison()
        for side in ("current", "compared"):
            self.assertEqual(rows["last-trade"][side]["value"], "—")
            self.assertFalse(rows["last-trade"][side]["stale"])
            self.assertEqual(rows["bid-ask"][side]["value"], "—")

        # Clearing the comparison, then the selection, hides the group;
        # switching market clears the selection with it.
        self.compare_select().select_option("")
        self.assert_not_comparing()
        expect(self.page.locator("#inspector")).to_be_visible()
        self.assert_comparison_hidden()
        self.choose_compare(compared)
        expect(group).to_be_visible()
        self.page.locator("#selection-clear").click()
        self.assert_no_selection()
        self.assert_comparison_hidden()
        self.select_button("Oct 2026").click()
        expect(group).to_be_visible()
        self.page.locator('.market-options [data-market="EURTRY"]').click()
        self.assert_no_selection()
        self.assert_comparison_hidden()
        self.select_button("Dec 2026", "EURTRY").click()
        expect(group).to_be_visible()
        expected = self.expected_comparison(
            (self.snapshot["markets"]["EURTRY"], self.contract("Dec 2026", "EURTRY")),
            self.history_contract(compared, "Dec 2026", "EURTRY"), "monthly", unit="TRY per EUR",
        )
        self.assert_comparison(expected)

    def test_inspector_comparison_says_when_a_maturity_was_not_listed(self):
        self.open_dashboard(f"?compare={ROLLED_DAY}&market=USDTRY")
        self.assert_comparing(ROLLED_DAY)
        # Jan 2028 was not listed yet on 07 Jul.
        self.select_button("Jan 2028").click()
        group = self.page.locator("#inspector-compare-group")
        expect(group).to_be_visible()
        expect(self.page.locator("#inspector-compare-note")).to_have_text(
            f"Not listed on {HISTORY_LABELS[ROLLED_DAY]}: Jan 2028 was not listed yet on "
            "that close, so no changes are shown."
        )
        expect(self.page.locator("#inspector-compare-table")).to_be_hidden()
        self.assertEqual(self.comparison(), {})
        # A maturity listed on both days still compares.
        self.select_button("Dec 2027").click()
        expect(self.page.locator("#inspector-compare-table")).to_be_visible()
        self.assert_comparison(self.expected_comparison(
            (self.snapshot["markets"]["USDTRY"], self.contract("Dec 2027")),
            self.history_contract(ROLLED_DAY, "Dec 2027"),
        ))

    def test_inspector_comparison_in_a_past_close_view(self):
        day, compared = "2026-10-06", "2026-09-30"
        self.open_day(day, f"&compare={compared}&market=USDTRY")
        self.assert_comparing(compared, HISTORY_LABELS[day])
        self.select_button("Dec 2026").click()
        self.assert_selected("Dec 2026")
        expected = self.expected_comparison(
            self.history_contract(day, "Dec 2026"), self.history_contract(compared, "Dec 2026"),
        )
        self.assertEqual(expected["change"]["days"], "-6 days")
        self.assert_comparison(expected)
        rows = self.comparison()
        self.assertEqual(rows["date"]["current"]["value"], HISTORY_LABELS[day])
        self.assertEqual(rows["date"]["change"], "6 calendar days apart")
        expect(
            self.page.get_by_role("table", name="Comparison").get_by_role("columnheader")
        ).to_have_text(
            ["Input", f"Close · {HISTORY_LABELS[day]}", f"Close · {HISTORY_LABELS[compared]}",
             "Change"]
        )
        self.assert_tooltip_delta("Dec 2026", rows["yield"]["change"])

    def test_inspector_comparison_keeps_the_compared_close_stored_on_a_later_day(self):
        self.reopen_at(NEXT_DAY_TIME)
        compared = "2026-09-30"
        self.open_dashboard(f"?compare={compared}&market=USDTRY")
        self.assert_comparing(compared)
        self.assert_note("08 Oct 2026")
        self.select_button("Oct 2026").click()
        live = self.snapshot["markets"]["USDTRY"]
        front = self.contract("Oct 2026")
        # Live moves on to 22 days left; 30 Sep keeps its stored 30 days and yield.
        rolled_yield = ((front["last"] / live["spot"]["last"]) ** (1 / 22) - 1) * 100
        expected = self.expected_comparison(
            (live, front), self.history_contract(compared, "Oct 2026"),
            current_days=22, current_yield=rolled_yield,
        )
        self.assertEqual(expected["change"]["days"], "-8 days")
        self.assertEqual(expected["compared"]["days"], "30")
        self.assert_comparison(expected)
        rows = self.comparison()
        self.assertEqual(rows["date"]["change"], "7 calendar days apart")
        self.assertEqual(rows["date"]["current"]["detail"], "Days left from 08 Oct 2026, adjusted")
        self.assert_tooltip_delta("Oct 2026", rows["yield"]["change"])

    def test_inspector_comparison_states_why_values_are_missing(self):
        compared = "2026-09-30"
        unpriced = copy.deepcopy(self.snapshot)
        market = unpriced["markets"]["USDTRY"]
        contract = next(c for c in market["contracts"] if c["label"] == "Dec 2026")
        contract["last"] = None
        contract["premium_percent"] = None
        set_yields(contract, market["spot"]["last"])
        self.serve_snapshot(re.compile(r"/data/market\.json"), unpriced)
        self.open_dashboard(f"?compare={compared}&market=USDTRY")
        self.assert_comparing(compared)
        self.select_button("Dec 2026").click()
        rows = self.comparison()
        self.assertEqual(rows["last"]["current"]["value"], "—")
        self.assertEqual(rows["last"]["current"]["detail"], "No last price in this snapshot.")
        self.assertEqual(rows["premium"]["current"]["value"], "—")
        self.assertEqual(
            rows["premium"]["current"]["detail"], "No premium: the contract has no last price."
        )
        self.assertEqual(rows["yield"]["current"]["value"], "—")
        self.assertEqual(
            rows["yield"]["current"]["detail"], "No yield: the contract has no last price."
        )
        for key in ("last", "premium", "yield"):
            with self.subTest(item=key):
                self.assertNotEqual(rows[key]["compared"]["value"], "—")
                self.assertEqual(rows[key]["compared"]["detail"], "")
                self.assertEqual(rows[key]["change"], "—")
        # Spot and days left don't depend on the price.
        self.assertNotEqual(rows["spot"]["change"], "—")
        self.assertEqual(rows["days"]["change"], "-7 days")

        # On its maturity day a contract has no yield, and says why.
        self.reopen_at(datetime.fromisoformat("2026-10-30T12:00:00+03:00"))
        self.open_dashboard(f"?compare={compared}&market=USDTRY")
        self.assert_comparing(compared)
        self.select_button("Oct 2026").click()
        rows = self.comparison()
        self.assertEqual(rows["days"]["current"]["value"], "0")
        self.assertEqual(rows["days"]["compared"]["value"], "30")
        self.assertEqual(rows["days"]["change"], "-30 days")
        self.assertEqual(rows["yield"]["current"]["value"], "—")
        self.assertEqual(
            rows["yield"]["current"]["detail"],
            "No yield: the contract matures today, with 0 days left.",
        )
        self.assertEqual(rows["yield"]["change"], "—")
        self.assertEqual(rows["premium"]["change"], "+0.00 pp")

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_of_the_inspector_comparison(self):
        for viewport in (None, MOBILE_VIEWPORT):
            with self.subTest(viewport=viewport):
                if viewport:
                    self.reopen_at(FIXTURE_DAY_TIME, viewport=viewport)
                self.open_dashboard("?compare=2026-09-30&market=USDTRY")
                self.select_button("Jan 2028").click()
                expect(self.page.locator("#inspector-compare-table")).to_be_visible()
                self.assert_no_sideways_scroll()
                self.assert_accessible(f"inspector comparison at {viewport or 'desktop'}")
                # The not-listed message, too.
                self.compare_select().select_option(ROLLED_DAY)
                expect(self.page.locator("#inspector-compare-note")).to_contain_text("Not listed")
                self.assert_no_sideways_scroll()
                self.assert_accessible(f"not-listed comparison at {viewport or 'desktop'}")

    # The inspector's History table: the selected contract on every archived close

    def watch_history_fetches(self):
        """Record each archived close the page requests, by date, from now on."""
        fetched = []
        pattern = re.compile(r"/data/history/(\d{4}-\d{2}-\d{2})\.json")
        def record(request):
            match = pattern.search(request.url)
            if match:
                fetched.append(match.group(1))
        self.page.on("request", record)
        return fetched

    def history_rows(self):
        """Each History row: its state, date, link, tag and each cell's text."""
        return self.page.evaluate(
            """() => [...document.querySelectorAll('#inspector-history-rows tr')].map((row) => {
                const header = row.querySelector('th');
                const link = header?.querySelector('a');
                return {
                    state: row.dataset.state,
                    day: row.dataset.historyRow ?? null,
                    onScreen: row.classList.contains('is-on-screen'),
                    date: header ? (link ?? header).firstChild.textContent : null,
                    href: link?.getAttribute('href') ?? null,
                    tag: header?.querySelector('.inspector-tag')?.textContent ?? '',
                    cells: [...row.querySelectorAll('td')].map((td) => {
                        const value = td.querySelector('.inspector-value');
                        return value ? (value.firstChild?.textContent ?? '') : td.textContent;
                    }),
                    stale: Boolean(row.querySelector('.stale-tag')),
                };
            })"""
        )

    def history_header(self):
        return self.page.locator("#inspector-history-head th").all_text_contents()

    def load_history(self, closes):
        button = self.page.locator("#history-load")
        expect(button).to_be_visible()
        expect(button).to_have_text(
            f"Load history · {closes} archived close{'' if closes == 1 else 's'}"
        )
        button.click()
        expect(self.page.locator("#inspector-history-scroll")).to_be_visible()
        expect(button).to_be_hidden()

    def expected_history_row(self, market, contract, period="daily", digits=4):
        """A priced History row's cells, hand-computed from the snapshot's inputs."""
        spot, last, days = market["spot"]["last"], contract["last"], contract["days_to_maturity"]
        _, _, period_days, yield_digits = next(
            c for c in YIELD_COLUMNS if c[1].startswith(period)
        )
        factor = (last / spot) ** (1 / days)
        return [
            f"{last:,.{digits}f}",
            f"{(last / spot - 1) * 100:+.2f}%",
            f"{(factor ** period_days - 1) * 100:+.{yield_digits}f}%",
            str(days),
        ]

    def history_symbol_on(self, day, key="USDTRY"):
        market = self.history[day]["markets"][key]
        return market, next(c for c in market["contracts"] if c["symbol"] == HISTORY_SYMBOL)

    def test_history_loads_on_demand_and_lists_each_close(self):
        fetched = self.watch_history_fetches()
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.assert_selected("Jan 2028")
        group = self.page.locator("#inspector-history-group")
        expect(group).to_be_visible()
        expect(self.page.locator("#inspector-history-scroll")).to_be_hidden()
        self.assertEqual(fetched, [], "fetched archived closes before Load history")

        self.load_history(len(HISTORY_FIXTURE))
        self.assertEqual(sorted(fetched), sorted(HISTORY_FIXTURE))
        expect(self.page.locator("#inspector-history-status")).to_have_text(
            "History loaded: 5 archived closes."
        )
        expect(self.page.locator("#inspector-history-coverage")).to_have_text(
            "Observed with a price on 3 of 5 archived closes, 07 Jul 2026 – 06 Oct 2026."
        )
        self.assertEqual(
            self.history_header(),
            ["Close", "Last", "Premium", "Daily yield · 1D", "Days left", "Last trade"],
        )

        rows = self.history_rows()
        self.assertEqual(
            [(row["state"], row["day"]) for row in rows],
            [
                ("listed", "2026-10-07"),
                ("listed", "2026-10-06"),
                ("listed", "2026-10-05"),
                ("gap", None),
                ("listed", "2026-09-30"),
                ("gap", None),
                ("listed", NO_PRICE_DAY),
                ("gap", None),
                ("not-listed", ROLLED_DAY),
            ],
        )
        live, *archived = [row for row in rows if row["state"] != "gap"]
        # The live view is the day on screen: labelled, and not a link.
        self.assertEqual((live["date"], live["tag"], live["href"]), (LIVE_LABEL, "Live", None))
        self.assertTrue(live["onScreen"])
        live_market = self.snapshot["markets"]["USDTRY"]
        live_contract = self.contract("Jan 2028")
        self.assertEqual(
            live["cells"], self.expected_history_row(live_market, live_contract) + ["05 Oct 17:54"]
        )
        self.assertTrue(live["stale"])
        for row in archived:
            with self.subTest(day=row["day"]):
                self.assertFalse(row["onScreen"])
                self.assertEqual(row["date"], HISTORY_LABELS[row["day"]])
                self.assertEqual(dict(parse_qsl(row["href"].lstrip("?"))), {
                    "date": row["day"], "market": "USDTRY", "maturity": "2028-01",
                    "period": "daily",
                })
        # Each priced close's values come from its own snapshot, by symbol,
        # and its last trade is stale within that snapshot, as on the board.
        for row in archived[:3]:
            with self.subTest(priced=row["day"]):
                market, contract = self.history_symbol_on(row["day"])
                traded = datetime.fromisoformat(contract["updated_at"])
                self.assertEqual(
                    row["cells"],
                    self.expected_history_row(market, contract)
                    + [traded.strftime("%d %b %H:%M").replace("Sep ", "Sept ")],
                )
                self.assertTrue(row["stale"])
        # Listed without a price: no price, premium or yield, nothing filled in.
        _, unpriced = self.history_symbol_on(NO_PRICE_DAY)
        self.assertEqual(
            archived[3]["cells"][:4],
            ["Listed, no price", "", "", str(unpriced["days_to_maturity"])],
        )
        self.assertEqual(archived[4]["cells"], ["Not listed on this close."])
        gaps = [row["cells"] for row in rows if row["state"] == "gap"]
        self.assertEqual(gaps, [
            ["No archived close for 2 weekdays (a holiday or a missed capture)."],
            ["No archived close for 16 weekdays (a holiday or a missed capture)."],
            ["No archived close for 43 weekdays (a holiday or a missed capture)."],
        ])
        self.assertEqual(self.errors, [])

    def test_history_reuses_its_closes_across_periods_selections_and_markets(self):
        fetched = self.watch_history_fetches()
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        self.assertEqual(len(fetched), len(HISTORY_FIXTURE))
        fetched.clear()

        # The period switch changes only the yield column.
        self.page.locator('[data-yield-period="monthly"]').click()
        expect(self.page.locator("#inspector-history-head th").nth(3)).to_have_text(
            "Monthly yield · 30D"
        )
        row = next(r for r in self.history_rows() if r["day"] == "2026-10-06")
        market, contract = self.history_symbol_on("2026-10-06")
        self.assertEqual(row["cells"][:4], self.expected_history_row(market, contract, "monthly"))
        self.assertIn("period=monthly", row["href"])

        # Another maturity and another market load straight from the cache.
        self.select_button("Dec 2026").click()
        self.assert_selected("Dec 2026")
        expect(self.page.locator("#inspector-history-scroll")).to_be_visible()
        expect(self.page.locator("#history-load")).to_be_hidden()
        self.assertEqual(
            [r["state"] for r in self.history_rows() if r["state"] != "gap"], ["listed"] * 6
        )
        self.page.locator('.market-options [data-market="EURTRY"]').click()
        self.assert_market("EURTRY")
        self.select_button("Dec 2026", "EURTRY").click()
        expect(self.page.locator("#inspector-history-scroll")).to_be_visible()
        row = next(r for r in self.history_rows() if r["day"] == "2026-09-30")
        market = self.history["2026-09-30"]["markets"]["EURTRY"]
        contract = next(c for c in market["contracts"] if c["symbol"] == "EURTRYZ2026")
        self.assertEqual(row["cells"][:4], self.expected_history_row(market, contract, "monthly"))
        self.assertIn("market=EURTRY", row["href"])
        self.assertEqual(fetched, [], "refetched cached closes")

    def test_history_date_opens_that_close_with_the_same_maturity(self):
        fetched = self.watch_history_fetches()
        self.open_dashboard("?market=USDTRY&maturity=2028-01&period=annualized")
        self.load_history(len(HISTORY_FIXTURE))
        fetched.clear()
        day = "2026-09-30"
        link = self.page.locator(f'#inspector-history-rows a[data-history-day="{day}"]')
        expect(link).to_have_text(HISTORY_LABELS[day])
        link.click()
        self.assert_historical(day)
        self.assert_selected("Jan 2028")
        self.assertEqual(
            {key: self.url_params()[key] for key in ("date", "market", "maturity", "period")},
            {"date": day, "market": "USDTRY", "maturity": "2028-01", "period": "annualized"},
        )
        self.assertEqual(self.page.evaluate("() => state.yieldPeriod"), "annualized")
        # Viewing a past close: later closes are left out, and the day on
        # screen is its own row, not a link.
        expect(self.page.locator("#inspector-history-coverage")).to_have_text(
            "Observed with a price on 1 of 3 archived closes, 07 Jul 2026 – 30 Sept 2026."
        )
        rows = [row for row in self.history_rows() if row["state"] != "gap"]
        self.assertEqual([row["day"] for row in rows], [day, NO_PRICE_DAY, ROLLED_DAY])
        self.assertEqual((rows[0]["tag"], rows[0]["href"]), ("On screen", None))
        self.assertTrue(rows[0]["onScreen"])
        self.assertEqual(fetched, [], "refetched cached closes")
        # The board agrees with the History row on screen.
        market, contract = self.history_symbol_on(day)
        self.assertEqual(
            rows[0]["cells"][:4], self.expected_history_row(market, contract, "annualized")
        )

        # A new tab or window keeps the browser's own behaviour.
        self.assertEqual(
            self.page.evaluate(
                """() => {
                  const link = document.querySelector('#inspector-history-rows a');
                  const event = new MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true });
                  link.addEventListener('click', (e) => e.preventDefault(), { once: true });
                  link.dispatchEvent(event);
                  return state.viewDate;
                }"""
            ),
            day,
        )

    def test_history_links_follow_the_comparison(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        def compare_params():
            return {
                row["day"]: dict(parse_qsl(row["href"].lstrip("?"))).get("compare")
                for row in self.history_rows() if row["href"]
            }
        self.assertEqual(set(compare_params().values()), {None})
        # Only closes later than the comparison carry it.
        self.choose_compare("2026-09-30")
        self.assert_comparing("2026-09-30")
        self.assertEqual(compare_params(), {
            "2026-10-06": "2026-09-30", "2026-10-05": "2026-09-30", "2026-09-30": None,
            NO_PRICE_DAY: None, ROLLED_DAY: None,
        })
        self.compare_select().select_option("")
        self.assert_not_comparing()
        self.assertEqual(set(compare_params().values()), {None})

    def test_history_link_works_as_a_shared_url(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        href = self.page.locator(
            '#inspector-history-rows a[data-history-day="2026-10-05"]'
        ).get_attribute("href")
        self.page.goto(self.base_url + href)
        self.assert_historical("2026-10-05")
        self.assert_selected("Jan 2028")
        # A fresh page hasn't loaded the History table.
        expect(self.page.locator("#history-load")).to_have_text(
            "Load history · 4 archived closes"
        )

    def test_history_says_which_close_could_not_be_loaded(self):
        failed = "2026-10-05"
        self.page.route(f"**/data/history/{failed}.json", lambda route: route.fulfill(status=404))
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        rows = {row["day"]: row for row in self.history_rows() if row["day"]}
        self.assertEqual(rows[failed]["state"], "error")
        self.assertEqual(
            rows[failed]["cells"], ["Could not be loaded: Market snapshot returned 404."]
        )
        # It is still a link, and every other close still renders.
        self.assertIsNotNone(rows[failed]["href"])
        self.assertEqual(
            [rows[day]["state"] for day in ("2026-10-06", "2026-09-30", NO_PRICE_DAY, ROLLED_DAY)],
            ["listed", "listed", "listed", "not-listed"],
        )
        expect(self.page.locator("#inspector-history-coverage")).to_have_text(
            "Observed with a price on 2 of 5 archived closes, 07 Jul 2026 – 06 Oct 2026. "
            "1 close could not be loaded."
        )
        # The browser's own report of the 404 is the only error.
        self.assertEqual(len(self.errors), 1, self.errors)
        self.assertIn("404", self.errors[0])
        self.errors = []

    def test_no_history_hides_the_history_group(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01", base_url=self.no_history_url)
        self.assert_selected("Jan 2028")
        expect(self.page.locator("#inspector-history-group")).to_be_hidden()
        expect(self.page.locator("#history-load")).to_be_hidden()

    def test_history_load_is_keyboard_operable(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.tab_to("#history-load")
        self.page.keyboard.press("Enter")
        expect(self.page.locator("#inspector-history-scroll")).to_be_visible()
        # Focus moves on to the table, which scrolls by keyboard.
        expect(self.page.locator("#inspector-history-scroll")).to_be_focused()
        self.page.keyboard.press("Tab")
        expect(self.page.locator("#inspector-history-rows a").first).to_be_focused()

    def test_history_prints_only_once_loaded(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.print_media(A4_CONTENT_WIDTH)
        expect(self.page.locator("#inspector")).to_be_visible()
        expect(self.page.locator("#inspector-history-group")).to_be_hidden()
        expect(self.page.locator("#history-chart")).to_be_hidden()
        self.page.emulate_media(media="screen")
        self.page.set_viewport_size({"width": 1280, "height": 900})
        self.load_history(len(HISTORY_FIXTURE))
        self.print_media(A4_CONTENT_WIDTH)
        expect(self.page.locator("#inspector-history-scroll")).to_be_visible()
        expect(self.page.locator("#history-load")).to_be_hidden()
        expect(self.page.locator("#inspector-history-status")).to_be_hidden()
        table = self.page.locator(".inspector-history-table").bounding_box()
        self.assertLessEqual(table["width"], A4_CONTENT_WIDTH)
        # The chart prints with the table, redrawn at the page's width.
        expect(self.page.locator("#history-chart")).to_be_visible()
        for selector in ("#history-chart", "#history-chart-svg"):
            box = self.page.locator(selector).bounding_box()
            self.assertLessEqual(box["x"] + box["width"], A4_CONTENT_WIDTH, selector)
        self.assertLessEqual(
            float(self.page.locator("#history-chart-svg").get_attribute("viewBox").split()[2]),
            A4_CONTENT_WIDTH,
        )

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_of_the_history_table(self):
        for viewport in (None, MOBILE_VIEWPORT):
            with self.subTest(viewport=viewport):
                if viewport:
                    self.reopen_at(FIXTURE_DAY_TIME, viewport=viewport)
                self.open_dashboard("?market=USDTRY&maturity=2028-01")
                self.load_history(len(HISTORY_FIXTURE))
                expect(self.page.locator("#history-chart")).to_be_visible()
                self.assert_no_sideways_scroll()
                self.assert_accessible(f"history table at {viewport or 'desktop'}")
                # With a point focused, too.
                self.page.locator("#history-chart-svg a").first.focus()
                self.assert_accessible(f"history chart point at {viewport or 'desktop'}")

    # The History chart: the same closes as the table, on a calendar-time axis

    def history_chart(self):
        """The chart's points, line runs, missing marks and gap bands."""
        return self.page.evaluate(
            """() => {
                const svg = document.querySelector('#history-chart-svg');
                const centre = (node) => {
                    const box = node.getBBox();
                    return box.x + box.width / 2;
                };
                return {
                    points: [...svg.querySelectorAll('.history-point')].map((node) => {
                        const marker = node.querySelector('.history-marker');
                        return {
                            day: node.dataset.historyDay,
                            link: node.tagName === 'a',
                            href: node.getAttribute('href'),
                            name: node.getAttribute('aria-label'),
                            onScreen: node.classList.contains('is-on-screen'),
                            shape: marker.tagName,
                            stale: marker.classList.contains('is-stale'),
                            halo: Boolean(node.querySelector('.history-halo')),
                            label: node.querySelector('.history-on-screen-label')?.textContent ?? null,
                            x: centre(marker),
                        };
                    }),
                    runs: [...svg.querySelectorAll('.history-lines polyline')].map(
                        (line) => line.dataset.days.split(' ')
                    ),
                    marks: Object.fromEntries([...svg.querySelectorAll('.history-missing-mark')].map(
                        (mark) => [mark.dataset.historyDay, mark.dataset.state]
                    )),
                    gaps: [...svg.querySelectorAll('rect[data-gap]')].map((band) => Number(band.dataset.gap)),
                };
            }"""
        )

    def history_yield_cells(self):
        """Each History row's yield cell, by date, for priced rows."""
        return {
            row["day"]: row["cells"][2]
            for row in self.history_rows()
            if row["state"] == "listed" and row["cells"][0] != "Listed, no price"
        }

    def test_history_chart_appears_only_once_history_is_loaded(self):
        fetched = self.watch_history_fetches()
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        expect(self.page.locator("#history-load")).to_be_visible()
        expect(self.page.locator("#history-chart")).to_be_hidden()
        self.assertEqual(self.page.locator("#history-chart-svg > *").count(), 0)
        self.assertEqual(fetched, [], "fetched archived closes before Load history")

        self.load_history(len(HISTORY_FIXTURE))
        expect(self.page.locator("#history-chart")).to_be_visible()
        expect(self.page.locator("#history-chart-caption")).to_have_text(
            "USDTRYF2028 · Daily yield · 1D on each close, oldest to newest. "
            "Each point opens its close; the table below lists every value."
        )
        self.assertEqual(sorted(fetched), sorted(HISTORY_FIXTURE))
        fetched.clear()

        # Period, selection and market switches redraw from the cache.
        self.page.locator('[data-yield-period="monthly"]').click()
        expect(self.page.locator("#history-chart-caption")).to_contain_text("Monthly yield · 30D")
        self.select_button("Dec 2026").click()
        self.assert_selected("Dec 2026")
        expect(self.page.locator("#history-chart-caption")).to_contain_text("USDTRYZ2026")
        self.assertEqual(len(self.history_chart()["points"]), 6)
        self.page.locator('.market-options [data-market="EURTRY"]').click()
        self.assert_market("EURTRY")
        expect(self.page.locator("#history-chart")).to_be_hidden()
        self.select_button("Dec 2026", "EURTRY").click()
        expect(self.page.locator("#history-chart-caption")).to_contain_text("EURTRYZ2026")
        self.assertEqual(len(self.history_chart()["points"]), 6)
        self.assertEqual(fetched, [], "refetched cached closes")

    def test_history_chart_points_equal_the_table(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        for period in ("daily", "annualized"):
            with self.subTest(period=period):
                self.page.locator(f'[data-yield-period="{period}"]').click()
                label, short = YIELD_PERIOD_LABELS[period].split(" · ")
                expect(self.page.locator("#inspector-history-head th").nth(3)).to_have_text(
                    f"{label} yield · {short}"
                )
                points = self.history_chart()["points"]
                table = self.history_yield_cells()
                # One point per priced close, oldest first, and nothing else.
                self.assertEqual(
                    [point["day"] for point in points],
                    ["2026-09-30", "2026-10-05", "2026-10-06", "2026-10-07"],
                )
                for point in points:
                    value = re.search(r": ([+-][\d.]+%) ", point["name"]).group(1)
                    self.assertEqual(value, table[point["day"]], point["day"])
                # Hand-computed from two closes' own spot, price and days left.
                for day in ("2026-09-30", "2026-10-06"):
                    market, contract = self.history_symbol_on(day)
                    expected = self.expected_history_row(market, contract, period)[2]
                    point = next(p for p in points if p["day"] == day)
                    self.assertIn(f": {expected} {period} yield", point["name"])
                self.assertEqual(
                    next(p for p in points if p["day"] == "2026-10-05")["name"],
                    f"05 Oct 2026: {table['2026-10-05']} {period} yield "
                    f"({short}), last trade 03 Oct 17:54, stale",
                )

    def test_history_chart_breaks_at_every_missing_close_and_gap(self):
        failed = "2026-10-06"
        self.page.route(f"**/data/history/{failed}.json", lambda route: route.fulfill(status=404))
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        chart = self.history_chart()
        # Not listed, listed without a price and not loaded: a mark, no point.
        self.assertEqual(chart["marks"], {
            ROLLED_DAY: "not-listed", NO_PRICE_DAY: "listed", failed: "error",
        })
        self.assertEqual(
            [point["day"] for point in chart["points"]], ["2026-09-30", "2026-10-05", "2026-10-07"]
        )
        # Every point is cut off from its neighbours: by weekdays without a
        # close before 05 Oct, and by the close that failed after it.
        self.assertEqual(chart["runs"], [])
        self.assertEqual(chart["gaps"], [43, 16, 2])
        self.assertEqual(len(self.errors), 1, self.errors)
        self.errors = []

        # Loaded, the closes 05, 06 and 07 Oct are the only joined run.
        self.reopen_at(FIXTURE_DAY_TIME)
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        chart = self.history_chart()
        self.assertEqual(chart["runs"], [["2026-10-05", "2026-10-06", "2026-10-07"]])
        self.assertEqual(chart["marks"], {ROLLED_DAY: "not-listed", NO_PRICE_DAY: "listed"})
        self.assertEqual(chart["gaps"], [43, 16, 2])
        # Calendar time: 30 Sep to 05 Oct is five days wide, 05 to 06 Oct one.
        x = {point["day"]: point["x"] for point in chart["points"]}
        step = x["2026-10-06"] - x["2026-10-05"]
        self.assertGreater(step, 0)
        self.assertAlmostEqual((x["2026-10-05"] - x["2026-09-30"]) / step, 5, places=3)
        self.assertAlmostEqual((x["2026-10-07"] - x["2026-10-06"]) / step, 1, places=3)
        key = self.page.locator("#history-chart-key")
        expect(key).to_contain_text("No yield on that close; the table says why")
        expect(key).to_contain_text("Weekdays with no archived close")
        expect(key).to_contain_text("The line breaks at each of these; nothing is interpolated.")

    def test_history_chart_marks_stale_points_and_the_day_on_screen(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        points = self.history_chart()["points"]
        # This contract last traded before each close's latest quote day.
        self.assertEqual([p["stale"] for p in points], [True] * 4)
        live = points[-1]
        self.assertEqual(
            (live["day"], live["onScreen"], live["link"], live["shape"], live["halo"], live["label"]),
            ("2026-10-07", True, False, "rect", True, "Live"),
        )
        self.assertTrue(live["name"].startswith("07 Oct 2026, Live: "))
        self.assertEqual({p["shape"] for p in points[:-1]}, {"circle"})
        self.assertFalse(any(p["onScreen"] or p["halo"] for p in points[:-1]))
        key = self.page.locator("#history-chart-key")
        expect(key).to_contain_text("Live: the snapshot on screen")
        expect(key).to_contain_text("Hollow: last traded before that close’s latest quote day")

        # On a past close, its own point is highlighted; Dec 2026 isn't stale.
        self.open_day("2026-10-05", "&market=USDTRY&maturity=2026-12")
        self.load_history(4)
        points = self.history_chart()["points"]
        self.assertEqual(
            [p["day"] for p in points], ["2026-07-07", "2026-09-07", "2026-09-30", "2026-10-05"]
        )
        self.assertEqual([p["stale"] for p in points], [False] * 4)
        self.assertEqual(
            [(p["onScreen"], p["label"]) for p in points],
            [(False, None)] * 3 + [(True, "On screen")],
        )
        self.assertEqual(points[-1]["shape"], "circle")
        expect(key).to_contain_text("On screen: the close you are viewing")
        expect(key).not_to_contain_text("Hollow")

    def test_history_chart_point_opens_its_close_like_the_table(self):
        fetched = self.watch_history_fetches()
        self.open_dashboard("?market=USDTRY&maturity=2028-01&period=annualized")
        self.load_history(len(HISTORY_FIXTURE))
        fetched.clear()
        day = "2026-09-30"
        point = self.page.locator(f'#history-chart-svg a[data-history-day="{day}"]')
        table_href = self.page.locator(
            f'#inspector-history-rows a[data-history-day="{day}"]'
        ).get_attribute("href")
        self.assertEqual(point.get_attribute("href"), table_href)
        self.assertEqual(
            point.get_attribute("aria-label"),
            f"30 Sept 2026: {self.history_yield_cells()[day]} annualized yield (365D), "
            "last trade 28 Sept 17:54, stale",
        )
        # A modified click keeps the browser's own behaviour.
        self.assertIsNone(
            self.page.evaluate(
                """() => {
                  const link = document.querySelector('#history-chart-svg a');
                  const event = new MouseEvent('click', { bubbles: true, cancelable: true, shiftKey: true });
                  link.addEventListener('click', (e) => e.preventDefault(), { once: true });
                  link.dispatchEvent(event);
                  return state.viewDate;
                }"""
            )
        )
        point.click()
        self.assert_historical(day)
        self.assert_selected("Jan 2028")
        self.assertEqual(
            {key: self.url_params()[key] for key in ("date", "market", "maturity", "period")},
            {"date": day, "market": "USDTRY", "maturity": "2028-01", "period": "annualized"},
        )
        # The view matches opening the same close from the table.
        expect(self.page.locator("#history-chart")).to_be_visible()
        points = self.history_chart()["points"]
        self.assertEqual(points[-1]["day"], day)
        self.assertTrue(points[-1]["onScreen"])
        self.assertEqual(fetched, [], "refetched cached closes")

    def test_history_chart_point_opens_its_close_from_the_keyboard_and_a_tap(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        # Points come straight after the loading status in Tab order.
        self.page.locator("#inspector-history-status").focus()
        self.tab_to('#history-chart-svg a[data-history-day="2026-09-30"]', limit=3)
        expect(self.page.locator("#history-chart-svg .history-point:focus .history-ring")).to_be_visible()
        self.page.keyboard.press("Enter")
        self.assert_historical("2026-09-30")
        self.assertEqual(self.url_params()["maturity"], "2028-01")

        self.reopen_at(FIXTURE_DAY_TIME, viewport=MOBILE_VIEWPORT, has_touch=True, is_mobile=True)
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        self.page.locator('#history-chart-svg a[data-history-day="2026-10-05"]').tap()
        self.assert_historical("2026-10-05")
        self.assert_no_sideways_scroll()

    def test_history_chart_point_centres_hit_their_own_point(self):
        # Dec 2026 is priced on every close, so on a phone 05, 06 and 07 Oct
        # sit a few pixels apart and the live point's halo covers the others.
        for viewport in (None, MOBILE_VIEWPORT):
            with self.subTest(viewport=viewport):
                if viewport:
                    self.reopen_at(
                        FIXTURE_DAY_TIME, viewport=viewport, has_touch=True, is_mobile=True,
                    )
                self.open_dashboard("?market=USDTRY&maturity=2026-12")
                self.load_history(len(HISTORY_FIXTURE))
                hits = self.page.evaluate(
                    """() => [...document.querySelectorAll('#history-chart-svg .history-point')]
                        .map((node) => {
                            const box = node.querySelector('.history-marker').getBoundingClientRect();
                            const hit = document.elementFromPoint(
                                box.x + box.width / 2, box.y + box.height / 2,
                            );
                            return [node.dataset.historyDay,
                                    hit?.closest('.history-point')?.dataset.historyDay ?? null];
                        })"""
                )
                self.assertEqual(len(hits), 6)
                for day, hit in hits:
                    self.assertEqual(hit, day)
                if viewport:
                    box = self.page.locator(
                        '#history-chart-svg [data-history-day="2026-10-06"] .history-marker'
                    ).bounding_box()
                    self.page.touchscreen.tap(
                        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    )
                    self.assert_historical("2026-10-06")

    def test_history_chart_follows_the_yield_period(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        before = self.history_chart()["points"]
        self.page.locator('[data-yield-period="monthly"]').click()
        expect(self.page.locator("#history-chart-caption")).to_contain_text("Monthly yield · 30D")
        after = self.history_chart()["points"]
        self.assertEqual([p["day"] for p in after], [p["day"] for p in before])
        table = self.history_yield_cells()
        for point in after:
            self.assertIn(f": {table[point['day']]} monthly yield (30D)", point["name"])
            if point["link"]:
                self.assertEqual(dict(parse_qsl(point["href"].lstrip("?")))["period"], "monthly")
        self.assertNotEqual([p["name"] for p in after], [p["name"] for p in before])

    # Shareable links: the selected maturity and the yield period

    def url_params(self):
        return dict(parse_qsl(urlsplit(self.page.url).query))

    def history_length(self):
        return self.page.evaluate("() => history.length")

    def assert_period(self, period):
        pressed = self.page.locator('[data-yield-period][aria-pressed="true"]')
        expect(pressed).to_have_count(1)
        expect(pressed).to_have_attribute("data-yield-period", period)
        expect(self.page.locator("#yield-period-label")).to_have_text(YIELD_PERIOD_LABELS[period])
        expect(self.page.locator("#yield-chart")).to_have_attribute(
            "aria-label", re.compile(f"^{YIELD_PERIOD_LABELS[period].split(' ')[0]} compounded"),
        )

    def test_link_restores_the_maturity_and_yield_period(self):
        self.open_dashboard("?market=USDTRY&maturity=2026-12&period=annualized")
        self.assert_market("USDTRY")
        self.assert_period("annualized")
        self.assert_selected("Dec 2026")
        self.assertEqual(
            [key for key, row in self.inspector().items() if row["tag"]], ["yield-annualized"],
        )
        self.assertEqual(self.inspector()["yield-annualized"]["tag"], "In chart")
        self.assert_comparison_hidden()
        expect(self.page.locator("#history-notice")).to_be_hidden()
        self.assertEqual(
            self.url_params(), {"market": "USDTRY", "maturity": "2026-12", "period": "annualized"},
        )

        # The default period is left out of the URL, even when a link names it.
        self.open_dashboard("?market=EURTRY&period=daily&maturity=2027-12")
        self.assert_period("daily")
        self.assert_selected("Dec 2027", "EURTRY")
        expect(self.page.locator("#history-notice")).to_be_hidden()
        self.assertEqual(self.url_params(), {"market": "EURTRY", "maturity": "2027-12"})

    def test_link_with_a_comparison_restores_the_inspector_comparison(self):
        compared = "2026-09-30"
        for day in (None, "2026-10-06"):
            with self.subTest(day=day):
                query = f"&compare={compared}&market=USDTRY&maturity=2026-12&period=annualized"
                if day:
                    self.open_day(day, query)
                    current = self.history_contract(day, "Dec 2026")
                else:
                    self.open_dashboard("?" + query[1:])
                    current = (self.snapshot["markets"]["USDTRY"], self.contract("Dec 2026"))
                self.assert_comparing(compared, HISTORY_LABELS[day] if day else LIVE_LABEL)
                self.assert_period("annualized")
                self.assert_selected("Dec 2026")
                expect(self.page.locator("#inspector-compare-table")).to_be_visible()
                self.assert_comparison(self.expected_comparison(
                    current, self.history_contract(compared, "Dec 2026"), period="annualized",
                ))
                self.assertEqual(self.comparison()["yield"]["tag"], "In chart")
                expect(self.page.locator("#history-notice")).to_be_hidden()
                self.assertEqual(self.url_params().get("maturity"), "2026-12")
                self.assertEqual(self.url_params().get("period"), "annualized")

    def test_selection_and_period_keep_the_url_in_sync_without_history_entries(self):
        self.open_dashboard("?market=USDTRY")
        length = self.history_length()
        self.assertEqual(self.url_params(), {"market": "USDTRY"})

        self.select_button("Dec 2026").click()
        self.assert_selected("Dec 2026")
        self.assertEqual(self.url_params().get("maturity"), "2026-12")
        self.click_point("#curve-chart", "Mar 2027")
        self.assert_selected("Mar 2027")
        self.assertEqual(self.url_params().get("maturity"), "2027-03")
        # Selecting it again clears it, and the param with it.
        self.click_point("#curve-chart", "Mar 2027")
        self.assert_no_selection()
        self.assertNotIn("maturity", self.url_params())
        self.select_button("Jul 2027").click()
        self.assertEqual(self.url_params().get("maturity"), "2027-07")
        self.page.get_by_role("button", name="Clear selection", exact=True).click()
        self.assert_no_selection()
        self.assertNotIn("maturity", self.url_params())

        for period in ("monthly", "annualized", "daily"):
            with self.subTest(period=period):
                self.page.locator(f'[data-yield-period="{period}"]').click()
                self.assert_period(period)
                self.assertEqual(
                    self.url_params().get("period"), None if period == "daily" else period,
                )

        self.page.locator('[data-yield-period="annualized"]').click()
        self.select_button("Nov 2026").click()
        self.assertEqual(
            self.url_params(), {"market": "USDTRY", "maturity": "2026-11", "period": "annualized"},
        )
        self.assertEqual(self.history_length(), length)

        # A reload reopens the same view.
        self.page.reload()
        expect(self.page.locator("#data-state")).to_have_class(re.compile(r"\bis-delayed\b"))
        self.assert_period("annualized")
        self.assert_selected("Nov 2026")
        self.assertEqual(self.history_length(), length)

    def test_switching_market_drops_the_maturity_and_keeps_the_period(self):
        self.open_dashboard("?market=USDTRY&maturity=2028-01&period=monthly")
        self.assert_selected("Jan 2028")
        length = self.history_length()

        # A past close, a comparison and Live keep both while the maturity is listed.
        day = "2026-10-06"
        self.page.locator("#history-select").select_option(day)
        self.assert_historical(day)
        self.assert_selected("Jan 2028")
        self.compare_select().select_option("2026-09-30")
        self.assert_comparing("2026-09-30", HISTORY_LABELS[day])
        self.assert_selected("Jan 2028")
        self.page.locator("#live-button").click()
        self.assert_live()
        self.assert_selected("Jan 2028")
        self.assertEqual(self.url_params(), {
            "market": "USDTRY", "maturity": "2028-01", "period": "monthly",
            "compare": "2026-09-30",
        })

        # A close where it isn't listed yet clears it, and the param.
        self.page.locator("#history-select").select_option(ROLLED_DAY)
        self.assert_historical(ROLLED_DAY)
        self.assert_no_selection()
        self.assertNotIn("maturity", self.url_params())
        self.assertEqual(self.url_params().get("period"), "monthly")

        self.page.locator("#live-button").click()
        self.assert_live()
        self.select_button("Dec 2026").click()
        self.assertEqual(self.url_params().get("maturity"), "2026-12")
        self.page.locator('.market-options [data-market="EURTRY"]').click()
        self.assert_market("EURTRY")
        self.assert_no_selection()
        expect(self.page.locator("#selection-announcer")).to_have_text("Selection cleared.")
        self.assert_period("monthly")
        self.assertEqual(self.url_params(), {"market": "EURTRY", "period": "monthly"})
        # Only the moves to another view add entries: two closes, Live twice
        # and the market.
        self.assertEqual(self.history_length(), length + 5)

    def test_unlisted_or_malformed_params_fall_back_with_a_notice(self):
        not_a_month = "That maturity link is not a month, so nothing is selected."
        bad_period = "That yield period is not 1D, 30D or 365D. Showing daily yields."
        cases = (
            ("?market=USDTRY&maturity=2026-09", None, "USDTRY",
             ["No Sep 2026 maturity listed for USD/TRY on 07 Oct 2026."]),
            ("?market=EURTRY&maturity=2027-03", None, "EURTRY",
             ["No Mar 2027 maturity listed for EUR/TRY on 07 Oct 2026."]),
            ("?market=USDTRY&maturity=Dec-2026", None, "USDTRY", [not_a_month]),
            ("?market=USDTRY&maturity=2026-13", None, "USDTRY", [not_a_month]),
            ("?market=USDTRY&maturity=", None, "USDTRY", [not_a_month]),
            ("&market=USDTRY&maturity=2028-01", ROLLED_DAY, "USDTRY",
             ["No Jan 2028 maturity listed for USD/TRY on 07 Jul 2026."]),
            ("?market=USDTRY&period=weekly", None, "USDTRY", [bad_period]),
            ("?market=USDTRY&period=constructor", None, "USDTRY", [bad_period]),
            # Each fallback from one link is explained.
            ("?date=2020-01-01&market=USDTRY&maturity=2026-09&period=weekly", None, "USDTRY",
             [bad_period, "No archived close for 2020-01-01. Showing live data.",
              "No Sep 2026 maturity listed for USD/TRY on 07 Oct 2026."]),
        )
        for query, day, market, notices in cases:
            with self.subTest(query=query):
                if day:
                    self.open_day(day, query)
                    self.assert_market(market, self.history[day])
                else:
                    self.open_dashboard(query)
                    self.assert_market(market)
                self.assert_no_selection()
                self.assert_period("daily")
                notice = self.page.locator("#history-notice")
                expect(notice).to_be_visible()
                expect(notice).to_have_text(" ".join(notices))
                params = self.url_params()
                self.assertNotIn("maturity", params)
                self.assertNotIn("period", params)
                self.assertEqual(params["market"], market)

    def test_poll_does_not_restore_a_cleared_link_selection(self):
        live_requests = []
        self.page.on(
            "request",
            lambda request: "data/market.json" in request.url
            and live_requests.append(request.url),
        )
        self.page.clock.install(time=FIXTURE_DAY_TIME)
        self.open_dashboard("?market=USDTRY&maturity=2026-12&period=annualized")
        self.assert_selected("Dec 2026")
        length = self.history_length()
        self.page.get_by_role("button", name="Clear selection", exact=True).click()
        self.assert_no_selection()

        for polls in (2, 3):
            self.page.evaluate("() => { document.querySelector('#contract-rows tr').__old = true; }")
            self.page.clock.run_for("01:01")
            self.page.wait_for_function(
                "() => !document.querySelector('#contract-rows tr').__old"
            )
            self.assertEqual(len(live_requests), polls)
            self.assert_no_selection()
            self.assert_period("annualized")
            self.assertEqual(self.url_params(), {"market": "USDTRY", "period": "annualized"})

        # A maturity chosen afterwards stays through the poll, in the URL too.
        self.select_button("Mar 2027").click()
        self.page.evaluate("() => { document.querySelector('#contract-rows tr').__old = true; }")
        self.page.clock.run_for("01:01")
        self.page.wait_for_function("() => !document.querySelector('#contract-rows tr').__old")
        self.assert_selected("Mar 2027")
        self.assertEqual(self.url_params().get("maturity"), "2027-03")
        self.assertEqual(self.history_length(), length)

    # Back and Forward: moves to another view are history entries

    def go_back(self):
        self.page.go_back(wait_until="commit")

    def go_forward(self):
        self.page.go_forward(wait_until="commit")

    def assert_focus_not_lost(self):
        self.assertFalse(
            self.page.evaluate(
                "() => !document.activeElement || document.activeElement === document.body"
            ),
            "focus fell back to the page",
        )

    def test_back_and_forward_step_between_live_and_a_close_from_the_view_menu(self):
        fetched = self.watch_history_fetches()
        self.open_dashboard("?market=USDTRY")
        length = self.history_length()
        day = "2026-10-05"
        self.page.locator("#history-select").select_option(day)
        self.assert_historical(day)
        self.assertEqual(self.history_length(), length + 1)

        self.go_back()
        self.assert_live()
        expect(self.page.locator("#history-select")).to_have_value("")
        self.assert_market("USDTRY")
        self.assertEqual(self.url_params(), {"market": "USDTRY"})
        self.go_forward()
        self.assert_historical(day)
        self.assert_market("USDTRY", self.history[day])
        self.assertEqual(self.url_params(), {"market": "USDTRY", "date": day})
        self.assertEqual(fetched, [day], "refetched a close already loaded")

        # Back to live, Refresh and Live in the menu leave a close as moves too,
        # and stay put when already live.
        for control in ("#live-button", "#refresh-button", "menu"):
            with self.subTest(control=control):
                self.page.locator("#history-select").select_option(day)
                self.assert_historical(day)
                before = self.history_length()
                if control == "menu":
                    self.page.locator("#history-select").select_option("")
                else:
                    self.page.locator(control).click()
                self.assert_live()
                self.assertEqual(self.history_length(), before + 1)
                self.go_back()
                self.assert_historical(day)
                self.go_forward()
                self.assert_live()
        before = self.history_length()
        self.page.locator("#refresh-button").click()
        self.assert_live()
        expect(self.page.locator("#refresh-button")).to_be_enabled()
        self.assertEqual(self.history_length(), before)
        self.assertEqual(self.errors, [])

    def test_back_walks_through_closes_opened_from_history_links(self):
        fetched = self.watch_history_fetches()
        self.open_dashboard("?market=USDTRY&maturity=2028-01&period=annualized")
        self.load_history(len(HISTORY_FIXTURE))
        fetched.clear()
        length = self.history_length()
        first, second = "2026-10-06", "2026-09-30"
        self.page.locator(f'#inspector-history-rows a[data-history-day="{first}"]').click()
        self.assert_historical(first)
        self.page.locator(f'#history-chart-svg a[data-history-day="{second}"]').click()
        self.assert_historical(second)
        self.assertEqual(self.history_length(), length + 2)

        params = {"market": "USDTRY", "maturity": "2028-01", "period": "annualized"}
        for go, day in (
            (self.go_back, first), (self.go_back, None),
            (self.go_forward, first), (self.go_forward, second),
        ):
            with self.subTest(go=go.__name__, day=day):
                go()
                if day:
                    self.assert_historical(day)
                else:
                    self.assert_live()
                self.assert_selected("Jan 2028")
                self.assert_period("annualized")
                expect(self.page.locator("#inspector-name")).to_have_text("Jan 2028")
                self.assertEqual(self.url_params(), {**params, **({"date": day} if day else {})})
                # The History table follows the day on screen.
                on_screen = [row for row in self.history_rows() if row["onScreen"]]
                self.assertEqual([row["day"] for row in on_screen], [day or self.snapshot["market_date"]])
                self.assert_focus_not_lost()
        self.assertEqual(fetched, [], "refetched cached closes")
        self.assertEqual(self.history_length(), length + 2)

    def test_back_restores_the_previous_market_and_its_maturity(self):
        self.open_dashboard("?market=USDTRY&maturity=2026-12")
        self.assert_selected("Dec 2026")
        length = self.history_length()
        # The market on screen is not a move.
        self.page.locator('.market-options [data-market="USDTRY"]').click()
        self.assertEqual(self.history_length(), length)

        self.page.locator('.market-options [data-market="EURTRY"]').click()
        self.assert_market("EURTRY")
        self.assert_no_selection()
        self.assertEqual(self.history_length(), length + 1)
        self.go_back()
        self.assert_market("USDTRY")
        self.assert_selected("Dec 2026")
        self.assertEqual(self.url_params(), {"market": "USDTRY", "maturity": "2026-12"})
        self.go_forward()
        self.assert_market("EURTRY")
        self.assert_no_selection()
        self.assertEqual(self.url_params(), {"market": "EURTRY"})

        # A close keeps its market across Back too.
        day = "2026-10-06"
        self.page.locator("#history-select").select_option(day)
        self.assert_historical(day)
        self.page.locator('.market-options [data-market="XAUTRY"]').click()
        self.assert_market("XAUTRY", self.history[day])
        self.go_back()
        self.assert_historical(day)
        self.assert_market("EURTRY", self.history[day])
        self.go_back()
        self.assert_live()
        self.assert_market("EURTRY")

    def test_adjustments_poll_and_fallbacks_add_no_history_entries(self):
        live_requests = []
        self.page.on(
            "request",
            lambda request: "data/market.json" in request.url
            and live_requests.append(request.url),
        )
        self.page.clock.install(time=FIXTURE_DAY_TIME)
        self.open_dashboard("?market=USDTRY")
        length = self.history_length()
        # A link whose every value falls back: one entry for the page itself.
        self.open_dashboard(
            "?date=2020-01-01&compare=not-a-date&market=USDTRY&maturity=2026-13&period=weekly"
        )
        expect(self.page.locator("#history-notice")).to_contain_text("not in the archive")
        self.assertEqual(self.url_params(), {"market": "USDTRY"})
        length += 1
        self.assertEqual(self.history_length(), length)

        self.choose_compare("2026-09-30")
        self.select_button("Dec 2026").click()
        self.assert_selected("Dec 2026")
        self.page.locator('[data-yield-period="monthly"]').click()
        self.assert_period("monthly")
        self.compare_select().select_option("")
        self.assert_not_comparing()
        self.assertEqual(
            self.url_params(), {"market": "USDTRY", "maturity": "2026-12", "period": "monthly"}
        )
        self.assertEqual(self.history_length(), length)

        polls = len(live_requests)
        self.page.evaluate("() => { document.querySelector('#contract-rows tr').__old = true; }")
        self.page.clock.run_for("01:01")
        self.page.wait_for_function("() => !document.querySelector('#contract-rows tr').__old")
        self.assertEqual(len(live_requests), polls + 1)
        self.assertEqual(self.history_length(), length)

        # A close that fails to load leaves the view and the entry as they were.
        failed = "2026-10-05"
        broken = copy.deepcopy(self.history[failed])
        broken["schema_version"] = 3
        self.serve_snapshot(re.compile(rf"/data/history/{failed}\.json"), broken)
        self.page.locator("#history-select").select_option(failed)
        expect(self.page.locator("#history-notice")).to_contain_text(
            f"The close of {HISTORY_LABELS[failed]} could not be loaded."
        )
        self.assert_live()
        expect(self.page.locator("#history-select")).to_have_value("")
        self.assertEqual(
            self.url_params(), {"market": "USDTRY", "maturity": "2026-12", "period": "monthly"}
        )
        self.assertEqual(self.history_length(), length)
        self.expect_load_errors(1)

        # Back goes to the previous view, not the previous tweak.
        self.go_back()
        self.assert_live()
        self.assertEqual(self.url_params(), {"market": "USDTRY"})
        self.assert_no_selection()
        self.assert_period("daily")

    def test_back_onto_a_close_that_can_no_longer_be_loaded_falls_back_to_live(self):
        self.open_dashboard("?market=EURTRY&compare=2026-09-30")
        day = "2026-10-05"
        self.page.locator("#history-select").select_option(day)
        self.assert_historical(day)
        self.go_back()
        self.assert_live()
        self.assert_comparing("2026-09-30")
        length = self.history_length()

        broken = copy.deepcopy(self.history[day])
        broken["schema_version"] = 3
        self.serve_snapshot(re.compile(rf"/data/history/{day}\.json"), broken)
        self.page.evaluate(f"() => state.historyCache.delete('{day}')")
        self.go_forward()
        notice = self.page.locator("#history-notice")
        expect(notice).to_have_text(f"The close of {HISTORY_LABELS[day]} could not be loaded.")
        self.assert_live()
        self.assert_market("EURTRY")
        self.assert_comparing("2026-09-30")
        expect(self.page.locator("#history-select")).to_have_value("")
        self.assertEqual(self.url_params(), {"market": "EURTRY", "compare": "2026-09-30"})
        self.assertEqual(self.history_length(), length)
        self.expect_load_errors(1)

    def test_back_and_forward_during_a_slow_load_land_on_the_last_entry(self):
        self.open_dashboard("?market=USDTRY")
        for day in ("2026-10-06", "2026-10-05", "2026-09-30"):
            self.page.locator("#history-select").select_option(day)
            self.assert_historical(day)
        length = self.history_length()
        # Several steps at once.
        self.page.evaluate("() => history.go(-3)")
        self.assert_live()
        self.assertEqual(self.url_params(), {"market": "USDTRY"})
        self.page.evaluate("() => history.go(2)")
        self.assert_historical("2026-10-05")
        self.assert_market("USDTRY", self.history["2026-10-05"])

        # Live data that arrives after Forward never replaces the close it reached.
        self.page.evaluate(
            """() => {
              const fetchNow = window.fetch;
              window.fetch = (url, options) => String(url).includes('market.json')
                ? new Promise((resolve) => setTimeout(resolve, 600)).then(() => fetchNow(url, options))
                : fetchNow(url, options);
            }"""
        )
        self.page.evaluate("() => history.go(-2)")
        self.page.wait_for_function("() => location.search === '?market=USDTRY'")
        expect(self.page.locator("#refresh-button")).to_be_disabled()
        self.go_forward()
        self.assert_historical("2026-10-06")
        self.page.wait_for_timeout(1000)
        self.assert_historical("2026-10-06")
        self.assert_market("USDTRY", self.history["2026-10-06"])
        self.assertEqual(self.url_params(), {"market": "USDTRY", "date": "2026-10-06"})
        expect(self.page.locator("#refresh-button")).to_be_enabled()

        # A close chosen while Back's live load is under way is one new entry.
        self.page.evaluate("() => history.back()")
        self.page.wait_for_function("() => location.search === '?market=USDTRY'")
        self.page.locator("#history-select").select_option(ROLLED_DAY)
        self.assert_historical(ROLLED_DAY)
        self.page.wait_for_timeout(1000)
        self.assert_historical(ROLLED_DAY)
        self.assertEqual(self.history_length(), length - 2)
        self.go_back()
        self.assert_live()
        self.assertEqual(self.errors, [])

    def test_back_after_tapping_a_history_chart_point_on_a_phone(self):
        self.reopen_at(FIXTURE_DAY_TIME, viewport=MOBILE_VIEWPORT, has_touch=True, is_mobile=True)
        self.open_dashboard("?market=USDTRY&maturity=2028-01")
        self.load_history(len(HISTORY_FIXTURE))
        day = "2026-10-05"
        self.page.locator(f'#history-chart-svg a[data-history-day="{day}"]').tap()
        self.assert_historical(day)
        self.go_back()
        self.assert_live()
        self.assert_selected("Jan 2028")
        expect(self.page.locator("#history-chart")).to_be_visible()
        self.assert_focus_not_lost()
        self.assert_no_sideways_scroll()

    # Accessibility: axe-core audit of each rendered state

    # Downloading the contract board as CSV

    def download_button(self):
        return self.page.get_by_role("button", name=re.compile(r"^Download CSV"))

    def download_csv(self, press=None):
        """Download the board by click, or by pressing a key on the focused button.

        Returns the suggested file name, the raw bytes and the parsed rows.
        """
        button = self.download_button()
        expect(button).to_be_enabled()
        with self.page.expect_download() as info:
            if press:
                button.focus()
                self.page.keyboard.press(press)
            else:
                button.click()
        download = info.value
        raw = Path(download.path()).read_bytes()
        return download.suggested_filename, raw, list(
            csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        )

    def csv_rows(self, table):
        header, *rows = table
        return [dict(zip(header, row)) for row in rows]

    def assert_csv_format(self, raw, table):
        """UTF-8 with a BOM, CRLF records, one header and every row as wide."""
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "no UTF-8 byte-order mark")
        self.assertEqual(raw.count(b"\r\n"), len(table))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertTrue(all(len(row) == len(table[0]) for row in table))

    def assert_missing_is_explained(self, rows):
        """A row names a reason exactly when one of its values is empty."""
        for row in rows:
            empty = [
                key for key, value in row.items()
                if key not in CSV_CONTEXT_COLUMNS and value == ""
            ]
            with self.subTest(maturity=row["maturity_month"], empty=empty):
                self.assertEqual(bool(row["missing"]), bool(empty))

    def assert_csv_contract(self, row, contract):
        """A row's stored contract values, in full precision."""
        self.assertEqual(row["maturity_month"], contract["maturity_date"][:7])
        for column, field in (
            ("label", "label"), ("symbol", "symbol"), ("code", "code"),
            ("maturity_date", "maturity_date"),
        ):
            self.assertEqual(row[column], contract[field])
        for column, field in (
            ("last", "last"), ("change_percent", "change_percent"), ("bid", "bid"),
            ("ask", "ask"), ("volume", "volume"), ("premium_percent", "premium_percent"),
        ):
            with self.subTest(column=column):
                value = contract.get(field)
                if isinstance(value, (int, float)):
                    self.assertEqual(float(row[column]), value)
                else:
                    self.assertEqual(row[column], "")

    def test_csv_of_the_live_usdtry_board(self):
        self.open_dashboard("?market=USDTRY")
        button = self.download_button()
        expect(button).to_have_accessible_name("Download CSV of the USD/TRY contract board")
        name, raw, table = self.download_csv()
        self.assertEqual(name, "vade-USDTRY-2026-10-07.csv")
        self.assert_csv_format(raw, table)
        self.assertEqual(table[0], CSV_COLUMNS + CSV_CARRY_COLUMNS + ["missing"])

        market = self.snapshot["markets"]["USDTRY"]
        rows = self.csv_rows(table)
        self.assertEqual(len(rows), len(market["contracts"]))
        # Board order.
        self.assertEqual(
            [row["label"] for row in rows],
            [text.strip() for text in self.page.locator("#contract-rows strong").all_inner_texts()],
        )
        for row, contract in zip(rows, market["contracts"]):
            stale = contract["symbol"] in STALE_TRADES
            timed = contract["updated_at"] not in (None, self.snapshot["generated_at"])
            with self.subTest(contract=contract["code"]):
                self.assertEqual(row["market"], "USDTRY")
                self.assertEqual(row["view"], "live")
                self.assertEqual(row["market_date"], "2026-10-07")
                self.assertEqual(row["snapshot_generated_at"], self.snapshot["generated_at"])
                self.assertEqual(row["days_rolled_forward_from"], "")
                self.assertEqual(float(row["spot"]), market["spot"]["last"])
                self.assertEqual(row["spot_unit"], "TRY per USD")
                self.assertEqual(row["spot_time"], market["spot"]["updated_at"])
                self.assertEqual(row["spot_source"], market["spot"]["source"])
                self.assertEqual(int(row["days_left"]), contract["days_to_maturity"])
                self.assert_csv_contract(row, contract)
                for period, field, _, _ in CSV_YIELDS:
                    self.assertEqual(float(row[f"yield_{period}_percent"]), contract[field])
                for field in CSV_CARRY_COLUMNS:
                    if isinstance(contract.get(field), (int, float)):
                        self.assertEqual(float(row[field]), contract[field])
                    else:
                        self.assertEqual(row[field], "")
                # The board's stale rule, and a time only where the board shows one.
                self.assertEqual(row["stale"], "true" if stale else "false")
                if not timed:
                    self.assertEqual(row["last_trade_at"], "")
                    self.assertIn("no last trade time", row["missing"].split("; "))
                else:
                    self.assertEqual(row["last_trade_at"], contract["updated_at"])

        by_code = {row["code"]: row for row in rows}
        # Hand-checked rows.
        front = by_code["F_USDTRY1026"]
        self.assertEqual(front["last"], "50.083")
        self.assertEqual(front["bid"], "50.082")
        self.assertEqual(front["yield_365d_percent"], "32.83610915665602")
        self.assertEqual(front["expected_short_profit_try"], "271.2229257931753")
        self.assertEqual(front["last_trade_at"], "2026-10-07T13:49:00+03:00")
        self.assertEqual(front["missing"], "")
        # No bid: no profit either, and nothing filled in.
        no_bid = by_code["F_USDTRY0827"]
        self.assertEqual((no_bid["last"], no_bid["bid"], no_bid["ask"]), ("63.121", "", ""))
        self.assertEqual((no_bid["volume"], no_bid["expected_short_profit_try"]), ("", ""))
        self.assertEqual(no_bid["expected_spot_at_maturity"], "58.7461747709814")
        self.assertEqual(
            no_bid["missing"].split("; "),
            ["no bid", "no ask", "no volume", "no last trade time", "no profit: no bid"],
        )
        stale = by_code["F_USDTRY0727"]
        self.assertEqual((stale["stale"], stale["last_trade_at"]), ("true", "2026-10-06T18:07:00+03:00"))
        # The board shows the same numbers, rounded.
        for text, row in zip(self.column("#contract-rows", "Daily yield · 1D"), rows):
            self.assertAlmostEqual(self.number(text), float(row["yield_1d_percent"]), delta=1e-4)
        self.assert_missing_is_explained(rows)

    def test_csv_of_markets_without_a_carry_table(self):
        for key, unit in (("EURTRY", "TRY per EUR"), ("XAUTRY", "TRY per gram")):
            with self.subTest(market=key):
                self.open_dashboard(f"?market={key}")
                market = self.snapshot["markets"][key]
                expect(self.download_button()).to_have_accessible_name(
                    f"Download CSV of the {market['pair']} contract board"
                )
                name, raw, table = self.download_csv()
                self.assertEqual(name, f"vade-{key}-2026-10-07.csv")
                self.assert_csv_format(raw, table)
                # The USD/TRY carry columns are left out.
                self.assertEqual(table[0], CSV_COLUMNS + ["missing"])
                rows = self.csv_rows(table)
                self.assertEqual(len(rows), len(market["contracts"]))
                for row, contract in zip(rows, market["contracts"]):
                    self.assertEqual((row["market"], row["spot_unit"]), (key, unit))
                    self.assert_csv_contract(row, contract)
                self.assert_missing_is_explained(rows)

    def test_csv_of_a_rolled_forward_live_snapshot(self):
        self.reopen_at(NEXT_DAY_TIME)
        self.open_dashboard("?market=USDTRY")
        self.assert_note("08 Oct 2026")
        market = self.snapshot["markets"]["USDTRY"]
        name, raw, table = self.download_csv()
        self.assertEqual(name, "vade-USDTRY-2026-10-08.csv")
        self.assert_csv_format(raw, table)
        rows = self.csv_rows(table)
        days = self.expected_days(market, date(2026, 10, 8))
        self.assertEqual([int(row["days_left"]) for row in rows], days)
        self.assertEqual(self.column("#contract-rows", "Days left"), [row["days_left"] for row in rows])
        spot, trend = market["spot"]["last"], market["spot_trend"]["daily_factor"]
        for row, contract, day in zip(rows, market["contracts"], days):
            with self.subTest(contract=contract["code"]):
                self.assertEqual(row["view"], "live")
                self.assertEqual(row["market_date"], "2026-10-08")
                self.assertEqual(row["days_rolled_forward_from"], "2026-10-07")
                # Prices stay as published; days, yields and the projection move on.
                self.assertEqual(float(row["last"]), contract["last"])
                factor = (contract["last"] / spot) ** (1 / day)
                for period, _, period_days, _ in CSV_YIELDS:
                    self.assertAlmostEqual(
                        float(row[f"yield_{period}_percent"]),
                        (factor ** period_days - 1) * 100,
                        places=9,
                    )
                self.assertAlmostEqual(
                    float(row["expected_spot_at_maturity"]), spot * trend ** day, places=9,
                )
        self.assert_missing_is_explained(rows)

    def test_csv_of_an_archived_close(self):
        day = "2026-10-06"
        self.open_day(day, "&market=USDTRY")
        name, raw, table = self.download_csv()
        self.assertEqual(name, f"vade-USDTRY-{day}.csv")
        self.assert_csv_format(raw, table)
        market = self.history[day]["markets"]["USDTRY"]
        rows = self.csv_rows(table)
        self.assertEqual(len(rows), len(market["contracts"]))
        for row, contract in zip(rows, market["contracts"]):
            with self.subTest(contract=contract["code"]):
                self.assertEqual((row["view"], row["market_date"]), ("close", day))
                self.assertEqual(row["days_rolled_forward_from"], "")
                self.assertEqual(int(row["days_left"]), contract["days_to_maturity"])
                self.assert_csv_contract(row, contract)
                for period, field, _, _ in CSV_YIELDS:
                    self.assertEqual(float(row[f"yield_{period}_percent"]), contract[field])
        self.assert_missing_is_explained(rows)

    def test_csv_with_a_comparison_matches_the_inspector(self):
        day = "2026-10-06"
        self.open_day(day, f"&compare={ROLLED_DAY}&market=USDTRY")
        self.assert_comparing(ROLLED_DAY, HISTORY_LABELS[day])
        name, raw, table = self.download_csv()
        self.assertEqual(name, f"vade-USDTRY-{day}-vs-{ROLLED_DAY}.csv")
        self.assert_csv_format(raw, table)
        self.assertEqual(
            table[0], CSV_COLUMNS + CSV_CARRY_COLUMNS + CSV_COMPARE_COLUMNS + ["missing"],
        )
        rows = {row["label"]: row for row in self.csv_rows(table)}
        # Only the board's maturities: Sep 2026, listed only on the compared
        # close, is not a row.
        self.assertEqual(
            list(rows), [c["label"] for c in self.history[day]["markets"]["USDTRY"]["contracts"]],
        )

        # Jan 2028 was not listed yet on the compared close.
        unlisted = rows["Jan 2028"]
        for column in CSV_COMPARE_COLUMNS:
            self.assertEqual(unlisted[column], "", column)
        self.assertEqual(unlisted["last"], str(self.history_contract(day, "Jan 2028")[1]["last"]))
        self.assertIn(f"not listed on {ROLLED_DAY}", unlisted["missing"].split("; "))

        (now_market, now), (then_market, then) = (
            self.history_contract(day, "Dec 2026"), self.history_contract(ROLLED_DAY, "Dec 2026"),
        )
        row = rows["Dec 2026"]
        self.assertEqual(row["compare_market_date"], ROLLED_DAY)
        self.assertEqual(float(row["compare_last"]), then["last"])
        self.assertEqual(float(row["compare_spot"]), then_market["spot"]["last"])
        self.assertEqual(int(row["compare_days_left"]), then["days_to_maturity"])
        self.assertEqual(
            int(row["delta_days_left"]), now["days_to_maturity"] - then["days_to_maturity"],
        )
        self.assertAlmostEqual(float(row["delta_price"]), now["last"] - then["last"], places=12)
        self.assertAlmostEqual(
            float(row["delta_spot"]), now_market["spot"]["last"] - then_market["spot"]["last"],
            places=12,
        )
        self.assertAlmostEqual(
            float(row["delta_premium_pp"]), now["premium_percent"] - then["premium_percent"],
            places=12,
        )
        # Each period's change is the inspector's, in full precision.
        self.select_button("Dec 2026").click()
        self.assert_selected("Dec 2026")
        for period, field, _, button in CSV_YIELDS:
            with self.subTest(period=period):
                self.assertEqual(float(row[f"compare_yield_{period}_percent"]), then[field])
                delta = float(row[f"delta_yield_{period}_pp"])
                self.assertAlmostEqual(delta, now[field] - then[field], places=12)
                self.page.locator(f'[data-yield-period="{button}"]').click()
                self.assert_period(button)
                digits = next(c[3] for c in YIELD_COLUMNS if c[1] == field)
                self.assertEqual(self.comparison_texts("change")["yield"], f"{delta:+.{digits}f} pp")
        self.assert_missing_is_explained(rows.values())

    def test_csv_without_the_compared_market_says_why(self):
        compared = "2026-09-30"
        thin = copy.deepcopy(self.history[compared])
        del thin["markets"]["EURTRY"]
        thin["market_order"].remove("EURTRY")
        self.serve_snapshot(re.compile(rf"/data/history/{compared}\.json"), thin)
        self.open_dashboard(f"?compare={compared}&market=EURTRY")
        expect(self.page.locator("#curve-legend")).to_contain_text("no EUR/TRY data")
        name, _, table = self.download_csv()
        self.assertEqual(name, f"vade-EURTRY-2026-10-07-vs-{compared}.csv")
        self.assertEqual(table[0], CSV_COLUMNS + CSV_COMPARE_COLUMNS + ["missing"])
        for row in self.csv_rows(table):
            self.assertEqual([row[column] for column in CSV_COMPARE_COLUMNS],
                             [""] * len(CSV_COMPARE_COLUMNS))
            self.assertIn(f"no EUR/TRY data on {compared}", row["missing"].split("; "))

    def test_csv_quotes_fields_and_guards_formulas(self):
        self.open_dashboard("?market=USDTRY")
        cases = [
            ["=1+2", "'=1+2"], ["+1", "'+1"], ["-1", "'-1"], ["@SUM(A1)", "'@SUM(A1)"],
            ["\tx", "'\tx"], ["\rx", "\"'\rx\""], ["\n=1+1", "\"'\n=1+1\""],
            ["a,b", '"a,b"'],
            ['say "hi"', '"say ""hi"""'], ["two\nlines", '"two\nlines"'],
            ["İstanbul", "İstanbul"], [-1.5, "-1.5"], [0.1 + 0.2, "0.30000000000000004"],
            [None, ""], [float("nan"), ""], [True, "true"], [False, "false"],
        ]
        encoded = self.page.evaluate(
            "(cases) => cases.map(([value]) => csvField(value === 'NaN' ? NaN : value))",
            [[("NaN" if isinstance(v, float) and v != v else v), e] for v, e in cases],
        )
        self.assertEqual(encoded, [expected for _, expected in cases])

        # A snapshot string that needs both, end to end.
        snapshot = copy.deepcopy(self.snapshot)
        label = '=HYPERLINK("http://example.invalid"), "x"'
        snapshot["markets"]["USDTRY"]["contracts"][0]["label"] = label
        snapshot["markets"]["USDTRY"]["contracts"][0]["change_percent"] = -0.01
        self.serve_snapshot(re.compile(r"/data/market\.json"), snapshot)
        self.open_dashboard("?market=USDTRY")
        _, raw, table = self.download_csv()
        self.assertIn(b'"\'=HYPERLINK(""http://example.invalid""), ""x"""', raw)
        front = self.csv_rows(table)[0]
        self.assertEqual(front["label"], "'" + label)
        # Negative numbers stay numeric.
        self.assertEqual(front["change_percent"], "-0.01")

    def test_csv_button_is_keyboard_operable_on_a_phone(self):
        self.reopen_at(FIXTURE_DAY_TIME, viewport=MOBILE_VIEWPORT)
        self.open_dashboard("?market=USDTRY")
        button = self.download_button()
        expect(button).to_be_visible()
        box = button.bounding_box()
        self.assertGreaterEqual(box["x"], 0)
        self.assertLessEqual(box["x"] + box["width"], MOBILE_VIEWPORT["width"])
        self.assert_no_sideways_scroll()
        for key in ("Enter", " "):
            with self.subTest(key=key):
                name, _, table = self.download_csv(press=key)
                self.assertEqual(name, "vade-USDTRY-2026-10-07.csv")
                self.assertEqual(len(table), len(self.snapshot["markets"]["USDTRY"]["contracts"]) + 1)

    def test_csv_button_waits_for_data(self):
        unknown = copy.deepcopy(self.snapshot)
        unknown["schema_version"] = 5
        self.serve_snapshot(re.compile(r"/data/market\.json"), unknown)
        self.page.goto(self.base_url)
        expect(self.page.locator("#data-state")).to_have_class(re.compile(r"\bis-error\b"))
        self.expect_load_errors(1)
        expect(self.download_button()).to_be_disabled()

    # Printing the dashboard

    def print_row(self, term):
        """The print header's value for term, or None when its row is hidden."""
        row = self.page.locator("#print-header dl > div").filter(
            has=self.page.locator("dt", has_text=re.compile(f"^{re.escape(term)}$"))
        )
        expect(row).to_have_count(1)
        return row.locator("dd").inner_text().strip() if row.is_visible() else None

    def print_media(self, width=None):
        """Lay the page out for print, optionally at a page's content width.

        Emulating the media applies the print styles at once, but sends no
        beforeprint: the page learns of it from its media query's change event,
        a frame later. Until then the header is visible and not yet filled, so
        return only once that event has been delivered.
        """
        if width:
            self.page.set_viewport_size({"width": width, "height": 1000})
        self.page.evaluate(
            """() => {
              const query = window.matchMedia('print');
              window.printMediaDelivered = query.matches;
              if (!query.matches) {
                query.addEventListener(
                  'change',
                  () => { window.printMediaDelivered = true; },
                  { once: true },
                );
              }
            }"""
        )
        self.page.emulate_media(media="print")
        # Change events reach every listener in one task, app.js's included.
        self.page.wait_for_function("() => window.printMediaDelivered === true")
        expect(self.page.locator("#print-header")).to_be_visible()

    def assert_print_header(self, view, compare=None, maturity=None, period="daily", note=None):
        self.print_media()
        expect(self.page.locator("#print-header dt")).to_have_count(8)
        for term in PRINT_ROWS:
            self.assertTrue(self.print_row(term), term)
        self.assertEqual(
            self.print_row("Market"),
            "USD/TRY · spot in TRY per USD · curve in TRY / USD",
        )
        self.assertEqual(self.print_row("View"), view)
        self.assertEqual(self.print_row("Compared with"), compare)
        self.assertEqual(self.print_row("Selected maturity"), maturity)
        self.assertEqual(self.print_row("Yield period"), YIELD_PERIOD_LABELS[period])
        self.assertEqual(self.print_row("Note"), note)
        self.assertEqual(self.print_row("Page"), self.page.url)
        self.assertEqual(self.print_row("Printed"), "07 Oct 2026, 23:00 TRT")
        expect(self.page.locator(".print-title")).to_have_text("VADE · VİOP futures, USD/TRY")

    def pdf_pages(self, **options):
        pdf = self.page.pdf(format="A4", **options)
        self.assertTrue(pdf.startswith(b"%PDF"))
        return len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", pdf))

    def test_print_button_opens_the_print_dialog(self):
        self.open_dashboard("?market=USDTRY")
        self.page.evaluate("() => { window.print = () => { window.printed = (window.printed ?? 0) + 1; }; }")
        button = self.page.get_by_role("button", name="Print this page", exact=True)
        expect(button).to_be_visible()
        expect(button).to_have_accessible_description(re.compile(r"print dialog"))
        # Beside Download CSV, in the board's heading.
        expect(self.page.locator(".board-actions #download-csv + .sr-only + #print-page")).to_have_count(1)
        button.click()
        self.assertEqual(self.page.evaluate("window.printed"), 1)
        for key in ("Enter", " "):
            button.focus()
            self.page.keyboard.press(key)
        self.assertEqual(self.page.evaluate("window.printed"), 3)

    def test_print_header_is_not_on_screen_or_in_the_accessibility_tree(self):
        self.open_dashboard("?market=USDTRY&maturity=2026-12&compare=2026-09-30")
        header = self.page.locator("#print-header")
        expect(header).to_be_hidden()
        self.assertEqual(header.evaluate("(node) => getComputedStyle(node).display"), "none")
        # Filled once printing starts, and still hidden on screen afterwards.
        self.page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
        expect(self.page.locator("#print-view")).to_contain_text("Live snapshot taken")
        expect(header).to_be_hidden()
        tree = self.page.locator("body").aria_snapshot()
        for text in ("Live snapshot taken", "Printed", "VADE · VİOP futures"):
            self.assertNotIn(text, tree)
        self.assert_no_sideways_scroll()

    def test_print_header_of_the_live_view(self):
        self.open_dashboard("?market=USDTRY")
        self.assert_print_header(
            "Live snapshot taken 07 Oct 2026, 14:06 TRT (Europe/Istanbul). "
            "Live values change on every refresh."
        )

    def test_print_header_of_an_archived_close(self):
        self.open_day("2026-10-06", "&market=USDTRY")
        self.assert_print_header("Archived close of 06 Oct 2026")
        # The past-close banner still prints, without its button.
        expect(self.page.locator("#history-banner")).to_be_visible()
        expect(self.page.locator("#live-button")).to_be_hidden()

    def test_print_header_of_a_comparison_with_a_maturity_and_period(self):
        query = "?market=USDTRY&compare=2026-09-30&maturity=2026-12&period=annualized"
        for day in (None, "2026-10-06"):
            with self.subTest(day=day):
                if day:
                    self.open_day(day, "&" + query[1:])
                else:
                    self.open_dashboard(query)
                self.assert_comparing("2026-09-30", current=HISTORY_LABELS[day] if day else LIVE_LABEL)
                self.assert_selected("Dec 2026")
                self.assert_print_header(
                    f"Archived close of {HISTORY_LABELS[day]}" if day else (
                        "Live snapshot taken 07 Oct 2026, 14:06 TRT (Europe/Istanbul). "
                        "Live values change on every refresh."
                    ),
                    compare="Close of 30 Sept 2026",
                    maturity=f"Dec 2026 · {self.contract('Dec 2026')['code']}",
                    period="annualized",
                )
                self.page.emulate_media(media="screen")

    def test_print_header_waits_for_the_compared_snapshot(self):
        day = "2026-09-30"
        view = (
            "Live snapshot taken 07 Oct 2026, 14:06 TRT (Europe/Istanbul). "
            "Live values change on every refresh."
        )
        for missing_market in (False, True):
            with self.subTest(missing_market=missing_market):
                self.page.emulate_media(media="screen")
                self.open_dashboard("?market=USDTRY")
                snapshot = copy.deepcopy(self.history[day])
                if missing_market:
                    del snapshot["markets"]["USDTRY"]
                    snapshot["market_order"].remove("USDTRY")
                pending = []
                pattern = re.compile(rf"/data/history/{day}\.json$")
                # Keep the archive request pending until after the first print:
                # its selected date is already in the URL, but no overlay exists.
                self.page.route(pattern, lambda route: pending.append(route))
                with self.page.expect_request(pattern):
                    self.compare_select().select_option(day)
                expect(self.compare_select()).to_have_value(day)
                expect(self.page.locator("#curve-legend")).to_be_hidden()
                self.assertIn(f"compare={day}", self.page.url)
                self.page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
                self.assert_print_header(view)

                self.assertEqual(len(pending), 1)
                pending.pop().fulfill(
                    status=200, content_type="application/json", body=json.dumps(snapshot)
                )
                if missing_market:
                    expect(self.page.locator("#curve-legend")).to_contain_text("no USD/TRY data")
                else:
                    self.assert_comparing(day)
                self.page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
                self.assert_print_header(
                    view,
                    compare="Close of 30 Sept 2026" + (" · no USD/TRY data" if missing_market else ""),
                )
                self.page.unroute(pattern)

    def test_print_header_keeps_the_rolled_forward_note_and_the_print_time(self):
        self.reopen_at(NEXT_DAY_TIME)
        self.open_dashboard("?market=USDTRY")
        note = self.page.locator("#days-note")
        expect(note).to_be_visible()
        self.print_media()
        self.assertEqual(self.print_row("Note"), note.inner_text().strip())
        self.assertEqual(self.print_row("Printed"), "08 Oct 2026, 10:00 TRT")
        # The print time is taken when printing starts, not when the page loaded.
        self.page.clock.set_fixed_time(NEXT_DAY_TIME + timedelta(minutes=95))
        self.page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
        self.assertEqual(self.print_row("Printed"), "08 Oct 2026, 11:35 TRT")

    def test_print_hides_controls_and_keeps_charts_tables_and_notes(self):
        self.open_dashboard("?market=USDTRY&compare=2026-09-30&maturity=2026-12")
        self.assert_comparing("2026-09-30")
        # A tooltip and a keyboard marker that were showing are dropped too.
        self.tab_to("#curve-chart")
        self.page.keyboard.press("ArrowRight")
        expect(self.page.locator("#curve-marker")).to_be_visible()
        self.print_media(A4_CONTENT_WIDTH)
        for selector in PRINT_HIDDEN:
            with self.subTest(hidden=selector):
                locator = self.page.locator(selector)
                for index in range(locator.count()):
                    expect(locator.nth(index)).to_be_hidden()
        for selector in PRINT_VISIBLE + ("#inspector", "#inspector-compare-table"):
            with self.subTest(visible=selector):
                expect(self.page.locator(selector)).to_be_visible()
        expect(self.page.locator("footer .disclaimer")).to_contain_text("not investment advice")
        # Charts redraw at the printed width, so they print undistorted.
        for canvas in ("#curve-chart", "#yield-chart"):
            with self.subTest(canvas=canvas):
                self.page.wait_for_function(
                    """(selector) => {
                      const canvas = document.querySelector(selector);
                      const box = canvas.getBoundingClientRect();
                      return canvas.width === Math.round(box.width) && canvas.height === Math.round(box.height);
                    }""",
                    arg=canvas,
                )
                self.assertLessEqual(
                    self.page.locator(canvas).bounding_box()["width"], A4_CONTENT_WIDTH
                )

    def test_print_fits_every_table_column_on_the_page(self):
        for width in (A4_CONTENT_WIDTH, LETTER_CONTENT_WIDTH):
            for query in ("?market=USDTRY&maturity=2026-12&compare=2026-09-30", "?market=EURTRY"):
                with self.subTest(width=width, query=query):
                    self.open_dashboard(query)
                    self.print_media(width)
                    shells = self.page.evaluate(
                        """() => [...document.querySelectorAll('.table-shell, .inspector-compare-table')]
                          .filter((node) => node.offsetParent !== null)
                          .map((node) => ({
                            scroll: node.scrollWidth,
                            client: node.clientWidth,
                            table: (node.querySelector('table') ?? node).getBoundingClientRect().right,
                            right: node.getBoundingClientRect().right,
                          }))"""
                    )
                    self.assertGreaterEqual(len(shells), 2 if "USDTRY" in query else 1)
                    for shell in shells:
                        self.assertLessEqual(shell["scroll"], shell["client"], shell)
                        self.assertLessEqual(shell["table"], shell["right"] + 0.5, shell)
                    self.assert_no_sideways_scroll()
                    self.page.emulate_media(media="screen")

    def test_print_keeps_table_structure_and_catalog_note_at_narrow_page_width(self):
        self.open_dashboard("?market=USDTRY")
        self.print_media(600)
        expect(self.page.locator(".catalog-note")).to_be_visible()
        table = self.page.locator("#contracts table")
        self.assertEqual(table.evaluate("node => getComputedStyle(node).display"), "table")
        self.assertEqual(table.locator("tbody").evaluate("node => getComputedStyle(node).display"), "table-row-group")

    def test_print_to_pdf_of_a_live_and_a_comparison_view(self):
        for query in ("?market=USDTRY", "?market=USDTRY&compare=2026-09-30&maturity=2026-12&period=annualized"):
            with self.subTest(query=query):
                self.open_dashboard(query)
                pages = self.pdf_pages()
                self.assertGreaterEqual(pages, 2)
                self.assertLessEqual(pages, MAX_PRINTED_PAGES)
                # Printing leaves the screen as it was.
                expect(self.page.locator("#print-header")).to_be_hidden()
                expect(self.page.locator("#refresh-button")).to_be_visible()

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
    def test_accessibility_of_a_keyboard_tooltip(self):
        compared = "2026-09-30"
        self.open_dashboard(f"?compare={compared}")
        self.assert_comparing(compared)
        for chart in KEYBOARD_CHARTS:
            with self.subTest(chart=chart[0]):
                self.tab_to(chart[0])
                self.page.keyboard.press("ArrowRight")
                self.assert_keyboard_point(chart, self.chart_points(chart[4])[1]["label"])
                self.assert_accessible(f"keyboard tooltip on {chart[0]}")

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
    def test_accessibility_with_a_selected_maturity(self):
        for viewport in (None, MOBILE_VIEWPORT):
            with self.subTest(viewport=viewport):
                if viewport:
                    self.reopen_at(FIXTURE_DAY_TIME, viewport=viewport)
                self.open_dashboard("?market=USDTRY")
                self.select_button("Dec 2026").click()
                self.assert_selected("Dec 2026")
                self.assert_no_sideways_scroll()
                self.assert_accessible(f"selection at {viewport or 'desktop'}")

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_of_the_stale_key_and_empty_charts(self):
        for viewport in (None, MOBILE_VIEWPORT):
            with self.subTest(viewport=viewport):
                if viewport:
                    self.reopen_at(FIXTURE_DAY_TIME, viewport=viewport)
                self.open_dashboard("?market=USDTRY")
                expect(self.page.locator("#curve-key")).to_be_visible()
                expect(self.page.locator("#yield-key")).to_be_visible()
                self.assert_accessible(f"stale key at {viewport or 'desktop'}")
        self.serve_snapshot(re.compile(r"/data/market\.json"), self.unpriced_market(self.snapshot))
        self.open_dashboard("?market=XAUTRY")
        self.assert_empty_charts()
        self.assert_accessible("empty charts")

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

    @unittest.skipIf(Axe is None, "axe-playwright-python is not installed")
    def test_accessibility_of_a_restored_link(self):
        for viewport in (None, MOBILE_VIEWPORT):
            with self.subTest(viewport=viewport):
                if viewport:
                    self.reopen_at(FIXTURE_DAY_TIME, viewport=viewport)
                self.open_day(
                    "2026-10-06",
                    "&compare=2026-09-30&market=USDTRY&maturity=2026-12&period=annualized",
                )
                self.assert_selected("Dec 2026")
                self.assert_period("annualized")
                expect(self.page.locator("#inspector-compare-table")).to_be_visible()
                self.assert_no_sideways_scroll()
                self.assert_accessible(f"restored link at {viewport or 'desktop'}")
        # The fallback notice, too.
        self.open_dashboard("?market=USDTRY&maturity=2026-09&period=weekly")
        expect(self.page.locator("#history-notice")).to_be_visible()
        self.assert_accessible("link fallback notice")


if __name__ == "__main__":
    unittest.main()
