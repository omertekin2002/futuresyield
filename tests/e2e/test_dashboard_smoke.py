"""Headless-browser smoke test of the dashboard against a fixed snapshot.

Run with: python -m unittest tests/e2e/test_dashboard_smoke.py -v

Needs the packages in requirements-dev.txt and a Playwright Chromium
(`python -m playwright install chromium`). This directory has no
__init__.py, so `python -m unittest discover -s tests` never collects it.
"""

import copy
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

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "_site"
FIXTURE = ROOT / "tests" / "fixtures" / "market.json"
FORMATTED_NUMBER = re.compile(r"^\d{1,3}(,\d{3})*\.\d+$")

# Test fixture history: archived days derived from FIXTURE with every price
# scaled, so each day renders values that differ from the live snapshot.
HISTORY_FIXTURE = {"2026-10-05": 0.98, "2026-10-06": 0.99}
HISTORY_LABELS = {"2026-10-05": "05 Oct 2026", "2026-10-06": "06 Oct 2026"}
SPOT_PRICES = ("last", "open", "high", "low", "change")
CONTRACT_PRICES = ("last", "bid", "ask", "change", "expected_spot_at_maturity")


def derive_day(snapshot, day, factor):
    """Return a test-only copy of snapshot, dated day, with prices scaled."""
    derived = copy.deepcopy(snapshot)
    derived["test_fixture"] = (
        f"Test fixture: {FIXTURE.name} with every price scaled by {factor}. "
        "Not market data."
    )
    derived["generated_at"] = f"{day}T23:30:00+03:00"
    derived["market_date"] = day
    for market in derived["markets"].values():
        for key in SPOT_PRICES:
            if isinstance(market["spot"].get(key), (int, float)):
                market["spot"][key] *= factor
        for contract in market["contracts"]:
            for key in CONTRACT_PRICES:
                if isinstance(contract.get(key), (int, float)):
                    contract[key] *= factor
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
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.errors = []
        self.external_requests = []
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


if __name__ == "__main__":
    unittest.main()
