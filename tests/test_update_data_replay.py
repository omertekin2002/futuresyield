"""Run the updater end to end against recorded borsapy responses.

Only ``scripts.update_data.bp`` is replaced (with ``BorsapyReplay``), plus
``time.sleep``, ``print`` and ``sys.argv`` around ``main``. Every ``fetch_*``
function, contract discovery and normalization run for real. ``main`` takes no
clock argument, so its tests bind ``now`` to the capture time on the real
``build_snapshot``; that keeps the recorded contracts from expiring as the
calendar moves on.

The happy path uses the recording unchanged. Every other case edits a copy of
it, and its docstring says what was derived from the recording.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import partial
import json
import math
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_data
from scripts.market_calendar import ISTANBUL
from scripts.snapshot_schema import validate_snapshot
from scripts.update_data import TROY_OUNCE_GRAMS, iso_timestamp

try:  # `python -m unittest discover -s tests` puts tests/ on sys.path.
    from borsapy_replay import BorsapyReplay
except ImportError:  # `python -m unittest tests.test_update_data_replay`
    from tests.borsapy_replay import BorsapyReplay


# The recording was captured at 22:28:40 Istanbul time on 20 Nov 2026.
NOW = datetime(2026, 11, 20, 22, 30, tzinfo=ISTANBUL)
# The same quotes captured after midnight, as the GitHub fallback cron does when
# the laptop trigger is offline: early on Saturday 21 Nov, before any session.
AFTER_MIDNIGHT = datetime(2026, 11, 21, 0, 30, tzinfo=ISTANBUL)
MARKET_KEYS = ["USDTRY", "EURTRY", "XAUTRY"]


class ReplayTestCase(unittest.TestCase):
    def setUp(self):
        self.replay = BorsapyReplay()
        self.printed: list[str] = []
        self.network_attempts: list[object] = []

        def refuse_network(_socket, address, *args, **kwargs):
            self.network_attempts.append(address)
            raise OSError("replay tests must not touch the network")

        # Belt and braces: bp is replaced wholesale, so nothing should connect.
        network = patch.object(socket.socket, "connect", refuse_network)
        network.start()
        self.addCleanup(network.stop)

    def tearDown(self):
        self.assertEqual(self.network_attempts, [], "a replay test reached the network")

    def assert_all_calls_recorded(self):
        self.assertEqual(self.replay.unrecorded, [])

    def build(self, now: datetime = NOW) -> dict:
        with (
            patch("scripts.update_data.bp", self.replay.module()),
            patch("builtins.print", side_effect=self.record_print),
        ):
            snapshot = update_data.build_snapshot(now=now)
        validate_snapshot(snapshot)
        json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        return snapshot

    def record_print(self, *args, **_kwargs):
        self.printed.append(" ".join(str(arg) for arg in args))

    def run_main(self, output: Path, attempts: int = 2, now: datetime = NOW) -> None:
        argv = ["update_data.py", "--output", str(output), "--attempts", str(attempts)]
        with (
            patch("sys.argv", argv),
            patch("scripts.update_data.bp", self.replay.module()),
            patch(
                "scripts.update_data.build_snapshot",
                partial(update_data.build_snapshot, now=now),
            ),
            patch("scripts.update_data.time.sleep"),
            patch("builtins.print", side_effect=self.record_print),
        ):
            update_data.main()

    def output_path(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name) / "data" / "market.json"

    def assert_nothing_published(self, output: Path, exc: SystemExit):
        self.assertIn("keeping last deployment", str(exc.code))
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".json.tmp").exists())

    @staticmethod
    def contract(snapshot: dict, market: str, symbol: str) -> dict:
        matches = [
            item
            for item in snapshot["markets"][market]["contracts"]
            if item["symbol"] == symbol
        ]
        if len(matches) != 1:
            raise AssertionError(f"{symbol} appears {len(matches)} times in {market}")
        return matches[0]

    def table_record(self, code: str) -> dict:
        return next(
            record
            for record in self.replay.viop_futures["records"]
            if record["code"] == code
        )

    def recorded_symbols(self, market: str) -> list[str]:
        return [
            item["symbol"]
            for item in self.replay.viop_contracts[market]
            if not item["is_continuous"]
        ]


class HappyPathTests(ReplayTestCase):
    def test_snapshot_from_the_recording(self):
        snapshot = self.build()
        self.assert_all_calls_recorded()

        self.assertEqual(snapshot["generated_at"], "2026-11-20T22:30:00+03:00")
        self.assertEqual(snapshot["market_date"], "2026-11-20")
        self.assertEqual(list(snapshot["markets"]), MARKET_KEYS)
        self.assertEqual(
            snapshot["sources"]["library"],
            "borsapy 0.10.2",
            "borsapy changed: re-record tests/fixtures/borsapy (see its README)",
        )
        self.assertEqual(self.replay.streams_opened, 1)

    def test_every_recorded_contract_is_priced_by_the_stream(self):
        snapshot = self.build()

        for market in MARKET_KEYS:
            contracts = snapshot["markets"][market]["contracts"]
            self.assertEqual(
                [item["symbol"] for item in contracts],
                self.recorded_symbols(market),
            )
            for item in contracts:
                quote = self.replay.quotes[item["symbol"]]
                with self.subTest(symbol=item["symbol"]):
                    self.assertEqual(item["last"], quote["last"])
                    self.assertEqual(item["source"], "TradingView via borsapy")
                    self.assertEqual(item["status"], "available")
                    self.assertEqual(item["bid"], quote["bid"])
                    self.assertEqual(item["ask"], quote["ask"])
                    self.assertEqual(item["change"], quote["change"])
                    self.assertEqual(item["change_percent"], quote["change_percent"])
                    self.assertEqual(item["volume"], quote["volume"])
                    self.assertEqual(item["updated_at"], iso_timestamp(quote["timestamp"]))

    def test_continuous_contracts_are_excluded(self):
        snapshot = self.build()

        for market in MARKET_KEYS:
            symbols = [item["symbol"] for item in snapshot["markets"][market]["contracts"]]
            self.assertNotIn(f"{market}1!", symbols)
            self.assertNotIn(f"{market}2!", symbols)

    def test_quote_fields_and_table_turnover(self):
        snapshot = self.build()

        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        self.assertEqual(dec["code"], "F_USDTRY1226")
        self.assertEqual(dec["label"], "Dec 2026")
        self.assertEqual(dec["maturity_date"], "2026-12-31")
        self.assertEqual(dec["days_to_maturity"], 41)
        self.assertEqual(dec["last"], 52.26)
        self.assertEqual(dec["bid"], 52.24)
        self.assertEqual(dec["change"], 0.008)
        # volume_qty is TRY turnover under borsapy 0.10.2's remap.
        self.assertEqual(dec["turnover_try"], 11922511259.0)
        self.assertEqual(dec["updated_at"], "2026-11-20T18:09:00+03:00")
        self.assertAlmostEqual(dec["premium_percent"], (52.26 / 50.53719 - 1) * 100)

        gold = self.contract(snapshot, "XAUTRY", "XAUTRYG2027")
        self.assertEqual(gold["code"], "F_XAUTRYM0227")
        self.assertEqual(gold["turnover_try"], 66377942.0)

    def test_recorded_quote_without_trade_time_has_null_updated_at(self):
        """USDTRYH2027's recorded quote really arrived with ``timestamp: None``."""
        self.assertIsNone(self.replay.quotes["USDTRYH2027"]["timestamp"])
        snapshot = self.build()

        march = self.contract(snapshot, "USDTRY", "USDTRYH2027")
        self.assertEqual(march["last"], 56.16)
        self.assertIsNone(march["updated_at"])

    def test_quote_without_change_falls_back_to_table_volume_tl(self):
        """Derived: ``change`` and ``change_percent`` removed from USDTRYZ2026's quote."""
        del self.replay.quotes["USDTRYZ2026"]["change"]
        del self.replay.quotes["USDTRYZ2026"]["change_percent"]
        snapshot = self.build()

        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        # Table: volume_tl is the absolute change, change is the percentage.
        self.assertEqual(dec["change"], 0.01)
        self.assertEqual(dec["change_percent"], 0.02)
        self.assertEqual(dec["last"], 52.26)

    def test_usdtry_spot_from_the_intraday_history(self):
        spot = self.build()["markets"]["USDTRY"]["spot"]

        self.assertEqual(spot["last"], 50.53719)
        self.assertEqual(spot["open"], 50.44719)  # first 15-minute bar of 20 Nov
        self.assertEqual(spot["high"], 50.57419)
        self.assertEqual(spot["low"], 50.43181)
        self.assertEqual(spot["updated_at"], "2026-11-20T22:15:00+03:00")
        self.assertEqual(spot["source"], "TradingView intraday via borsapy FX")

    def test_eurtry_spot_from_the_intraday_history(self):
        spot = self.build()["markets"]["EURTRY"]["spot"]

        self.assertEqual(spot["last"], 55.45519)
        self.assertEqual(spot["open"], 55.50119)
        self.assertEqual(spot["source"], "TradingView intraday via borsapy FX")

    def test_xautry_spot_is_synthetic_xauusd_times_usdtry(self):
        spot = self.build()["markets"]["XAUTRY"]["spot"]

        self.assertAlmostEqual(spot["last"], 3515.22 * 50.53719 / TROY_OUNCE_GRAMS)
        self.assertAlmostEqual(spot["open"], 3660.045 * 50.44719 / TROY_OUNCE_GRAMS)
        self.assertAlmostEqual(spot["high"], 3662.325 * 50.57419 / TROY_OUNCE_GRAMS)
        self.assertAlmostEqual(spot["low"], 3502.99 * 50.43181 / TROY_OUNCE_GRAMS)
        self.assertEqual(spot["updated_at"], "2026-11-20T22:15:00+03:00")
        self.assertEqual(spot["source"], "Synthetic XAUUSD × USDTRY via borsapy FX")

    def test_usdtry_trend_uses_the_close_on_or_before_the_target(self):
        snapshot = self.build()
        trend = snapshot["markets"]["USDTRY"]["spot_trend"]

        # 180 days before 20 Nov is Sun 24 May; Fri 22 May is the last close
        # on or before it, and the recorded Mon 25 May close is ignored.
        self.assertEqual(trend["start_date"], "2026-05-22")
        self.assertEqual(trend["start_spot"], 45.7392)
        self.assertEqual(trend["end_date"], "2026-11-20")
        self.assertEqual(trend["end_spot"], 50.53719)
        self.assertEqual(trend["days"], 182)
        factor = (50.53719 / 45.7392) ** (1 / 182)
        self.assertAlmostEqual(trend["daily_factor"], factor, places=12)

        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        expected_spot = 50.53719 * factor**41
        self.assertAlmostEqual(dec["expected_spot_at_maturity"], expected_spot)
        self.assertAlmostEqual(
            dec["expected_short_profit_try"], (52.24 - expected_spot) * 1_000
        )
        self.assertIsNone(snapshot["markets"]["EURTRY"]["spot_trend"])
        self.assertIsNone(snapshot["markets"]["XAUTRY"]["spot_trend"])

    def test_main_writes_the_replayed_snapshot(self):
        output = self.output_path()
        self.run_main(output)

        written = json.loads(output.read_text(encoding="utf-8"))
        validate_snapshot(written)
        expected = json.loads(json.dumps(self.build(), ensure_ascii=False))
        self.assertEqual(written, expected)
        self.assertFalse(output.with_suffix(".json.tmp").exists())
        self.assert_all_calls_recorded()


class AfterMidnightTests(ReplayTestCase):
    """The recording unchanged, run at AFTER_MIDNIGHT instead of NOW."""

    def test_snapshot_is_dated_to_the_session_its_quotes_close(self):
        output = self.output_path()
        self.run_main(output, now=AFTER_MIDNIGHT)
        self.assert_all_calls_recorded()

        written = json.loads(output.read_text(encoding="utf-8"))
        validate_snapshot(written)
        self.assertEqual(written["generated_at"], "2026-11-21T00:30:00+03:00")
        self.assertEqual(written["market_date"], "2026-11-20")

        # Counted from Friday's session, not from Saturday as the calendar day
        # would give: one day more than the old rule, the same as at NOW.
        evening = self.build()
        saturday = date(2026, 11, 21)
        for key in MARKET_KEYS:
            contracts = written["markets"][key]["contracts"]
            self.assertEqual(
                [c["symbol"] for c in contracts],
                [c["symbol"] for c in evening["markets"][key]["contracts"]],
            )
            for contract, same in zip(contracts, evening["markets"][key]["contracts"]):
                with self.subTest(contract=contract["symbol"]):
                    old_rule = (date.fromisoformat(contract["maturity_date"]) - saturday).days
                    self.assertEqual(contract["days_to_maturity"], old_rule + 1)
                    self.assertEqual(contract["days_to_maturity"], same["days_to_maturity"])
                    for field in ("daily_yield_factor", "annualized_yield_percent",
                                  "expected_spot_at_maturity", "expected_short_profit_try"):
                        self.assertEqual(contract[field], same[field], field)


class QuirkTests(ReplayTestCase):
    """Known upstream quirks, each derived from a copy of the recording."""

    def test_quote_without_bid(self):
        """Derived: ``bid`` removed from USDTRYZ2026's quote."""
        del self.replay.quotes["USDTRYZ2026"]["bid"]
        snapshot = self.build()

        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        self.assertIsNone(dec["bid"])
        self.assertIsNone(dec["expected_short_profit_try"])
        self.assertIsNotNone(dec["expected_spot_at_maturity"])
        self.assertEqual(dec["ask"], 52.3)
        self.assertEqual(dec["last"], 52.26)

    def test_quote_without_timestamp_never_gets_the_snapshot_time(self):
        """Derived: ``timestamp`` removed from USDTRYZ2026's quote."""
        del self.replay.quotes["USDTRYZ2026"]["timestamp"]
        snapshot = self.build()

        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        self.assertIsNone(dec["updated_at"])
        self.assertEqual(dec["last"], 52.26)

    def test_contract_without_a_stream_quote_uses_the_table(self):
        """Derived: USDTRYZ2026's stream quote never arrives (times out)."""
        self.replay.stream_timeouts.add("USDTRYZ2026")
        snapshot = self.build()

        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        self.assertEqual(dec["last"], 52.262)
        self.assertEqual(dec["source"], "İş Yatırım via borsapy")
        self.assertEqual(dec["status"], "available")
        self.assertEqual(dec["change"], 0.01)
        self.assertEqual(dec["change_percent"], 0.02)
        self.assertEqual(dec["turnover_try"], 11922511259.0)
        self.assertIsNone(dec["bid"])
        self.assertIsNone(dec["updated_at"])
        # One reconnect for the missing quote, and the rest stay streamed.
        self.assertEqual(self.replay.streams_opened, 2)
        jan = self.contract(snapshot, "USDTRY", "USDTRYF2027")
        self.assertEqual(jan["source"], "TradingView via borsapy")

    def test_expired_contract_from_discovery_is_dropped(self):
        """Derived: discovery also lists Oct 2026, which matured on 30 Oct."""
        self.replay.viop_contracts["USDTRY"].append(
            {
                "symbol": "USDTRYV2026",
                "full_name": "BIST:USDTRYV2026",
                "description": "Oct 2026",
                "base": "USDTRY",
                "month_code": "V",
                "year": "2026",
                "is_continuous": False,
                "exchange": "BIST",
                "type": "futures",
            }
        )
        snapshot = self.build()

        contracts = snapshot["markets"]["USDTRY"]["contracts"]
        symbols = [item["symbol"] for item in contracts]
        self.assertNotIn("USDTRYV2026", symbols)
        self.assertEqual(
            symbols,
            [s for s in self.recorded_symbols("USDTRY") if s != "USDTRYV2026"],
        )
        self.assertEqual(contracts[0]["days_to_maturity"], 10)  # Nov 2026

    def test_malformed_numbers_become_null(self):
        """Derived: quote and table fields replaced with NaN, None, dashes and inf."""
        dec_quote = self.replay.quotes["USDTRYZ2026"]
        dec_quote.update(
            bid="NaN", ask=None, open="—", high=math.inf, volume=math.nan, change="—"
        )
        self.replay.quotes["USDTRYF2027"]["last"] = "—"
        self.replay.quotes["EURTRYZ2026"]["change_percent"] = -math.inf
        self.replay.quotes["XAUTRYZ2026"]["low"] = "NaN"
        self.table_record("F_USDTRY1226")["volume_qty"] = "—"
        self.table_record("F_EURTRY1226")["volume_tl"] = None
        self.table_record("F_EURTRY1226")["change"] = "NaN"
        self.table_record("F_XAUTRYM1226")["price"] = math.inf
        snapshot = self.build()

        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        self.assertEqual(dec["last"], 52.26)
        for field in ("bid", "ask", "open", "high", "volume", "turnover_try"):
            self.assertIsNone(dec[field], field)
        self.assertIsNone(dec["expected_short_profit_try"])
        self.assertEqual(dec["change"], 0.01)  # quote's "—" falls back to the table

        jan = self.contract(snapshot, "USDTRY", "USDTRYF2027")
        self.assertEqual(jan["last"], 53.514)
        self.assertEqual(jan["source"], "İş Yatırım via borsapy")

        eur = self.contract(snapshot, "EURTRY", "EURTRYZ2026")
        self.assertEqual(eur["last"], 57.476)
        self.assertIsNone(eur["change_percent"])
        self.assertEqual(eur["change"], -0.128)

        gold = self.contract(snapshot, "XAUTRY", "XAUTRYZ2026")
        self.assertEqual(gold["last"], 6048.0)
        self.assertIsNone(gold["low"])

    def test_stream_unavailable_uses_table_prices_everywhere(self):
        """Derived: the TradingView stream refuses to connect."""
        self.replay.stream_connect_error = TimeoutError("Connection timed out after 10.0s")
        snapshot = self.build()

        for market in MARKET_KEYS:
            for item in snapshot["markets"][market]["contracts"]:
                with self.subTest(symbol=item["symbol"]):
                    self.assertEqual(item["source"], "İş Yatırım via borsapy")
                    self.assertEqual(item["last"], self.table_record(item["code"])["price"])
                    self.assertIsNone(item["bid"])
                    self.assertIsNone(item["updated_at"])
        self.assertEqual(self.replay.streams_opened, 2)
        self.assertTrue(any("TradingView stream unavailable" in line for line in self.printed))

    def test_viop_table_unavailable_keeps_stream_prices(self):
        """Derived: ``VIOP().futures`` raises."""
        self.replay.viop_futures_error = ConnectionError("İş Yatırım unreachable")
        snapshot = self.build()

        for market in MARKET_KEYS:
            contracts = snapshot["markets"][market]["contracts"]
            self.assertEqual(
                [item["symbol"] for item in contracts], self.recorded_symbols(market)
            )
            for item in contracts:
                self.assertEqual(item["source"], "TradingView via borsapy")
                self.assertIsNone(item["turnover_try"])
        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        self.assertEqual(dec["code"], "F_USDTRY1226")
        self.assertEqual(dec["last"], 52.26)
        gold = self.contract(snapshot, "XAUTRY", "XAUTRYZ2026")
        self.assertEqual(gold["code"], "F_XAUTRYM1226")

    def test_empty_xau_history_falls_back_to_gram_gold(self):
        """Derived: ``FX("XAU").history(period="1g", interval="15m")`` returns no rows."""
        self.replay.fx_15m["XAU"]["index"] = []
        self.replay.fx_15m["XAU"]["data"] = []
        snapshot = self.build()

        spot = snapshot["markets"]["XAUTRY"]["spot"]
        self.assertEqual(spot["last"], 5945.913)
        self.assertEqual(spot["open"], 6040.19)
        self.assertEqual(spot["updated_at"], "2026-11-19T03:00:00+03:00")
        self.assertEqual(spot["source"], "borsapy FX current fallback")
        self.assertTrue(any("XAU/TRY spot unavailable" in line for line in self.printed))

    def test_empty_trend_history_keeps_the_snapshot(self):
        """Derived: the daily USD history window returns no rows."""
        self.replay.fx_daily["USD"]["index"] = []
        self.replay.fx_daily["USD"]["data"] = []
        snapshot = self.build()

        usd = snapshot["markets"]["USDTRY"]
        self.assertIsNone(usd["spot_trend"])
        for item in usd["contracts"]:
            self.assertIsNone(item["expected_spot_at_maturity"])
            self.assertIsNone(item["expected_short_profit_try"])
        self.assertEqual(self.contract(snapshot, "USDTRY", "USDTRYZ2026")["last"], 52.26)


class FailWithoutPublishingTests(ReplayTestCase):
    def test_no_usable_usdtry_spot(self):
        """Derived: every USD 15-minute close is missing and ``.current`` has no last."""
        for row in self.replay.fx_15m["USD"]["data"]:
            row[3] = None  # Close
        self.replay.fx_current["USD"]["last"] = "—"
        output = self.output_path()

        with self.assertRaises(SystemExit) as caught:
            self.run_main(output)

        self.assert_nothing_published(output, caught.exception)
        self.assertIn("Could not fetch USD/TRY spot", str(caught.exception.code))

    def test_no_priced_eurtry_contract(self):
        """Derived: no EUR/TRY quote arrives and the table has no EUR/TRY rows."""
        self.replay.stream_timeouts.update(self.recorded_symbols("EURTRY"))
        self.replay.viop_futures["records"] = [
            record
            for record in self.replay.viop_futures["records"]
            if not record["code"].startswith("F_EURTRY")
        ]
        output = self.output_path()

        with self.assertRaises(SystemExit) as caught:
            self.run_main(output)

        self.assert_nothing_published(output, caught.exception)
        self.assertIn("no usable EUR/TRY futures prices", str(caught.exception.code))

    def test_no_stream_and_no_table(self):
        """Derived: the stream refuses to connect and ``VIOP().futures`` raises."""
        self.replay.stream_connect_error = ConnectionRefusedError("refused")
        self.replay.viop_futures_error = ConnectionError("İş Yatırım unreachable")
        output = self.output_path()

        with self.assertRaises(SystemExit) as caught:
            self.run_main(output)

        self.assert_nothing_published(output, caught.exception)
        self.assertIn("no usable USD/TRY futures prices", str(caught.exception.code))


class ShapeDriftTests(ReplayTestCase):
    """Upstream renames or remaps a field: fall back as designed or fail, never mislead."""

    def test_renamed_fx_close_column_falls_back_to_current(self):
        """Derived: the USD 15-minute history's ``Close`` column renamed to ``close``."""
        columns = self.replay.fx_15m["USD"]["columns"]
        columns[columns.index("Close")] = "close"
        snapshot = self.build()

        spot = snapshot["markets"]["USDTRY"]["spot"]
        # The recorded .current fallback, labelled as such, not a number read
        # from some other column of the drifted history.
        self.assertEqual(spot["last"], 50.4651)
        self.assertEqual(spot["open"], 50.4401)
        self.assertEqual(spot["source"], "borsapy FX current fallback")
        self.assertEqual(spot["updated_at"], "2026-11-19T03:00:00+03:00")
        self.assertTrue(any("USD/TRY spot unavailable" in line for line in self.printed))
        # Gold's synthetic reference follows the fallback USD/TRY spot.
        gold = snapshot["markets"]["XAUTRY"]["spot"]
        self.assertAlmostEqual(gold["last"], 3515.22 * 50.4651 / TROY_OUNCE_GRAMS)

    def test_renamed_viop_price_column_without_stream_fails(self):
        """Derived: the VİOP table's ``price`` column renamed and the stream down."""
        recording = self.replay.viop_futures
        recording["columns"][recording["columns"].index("price")] = "last_price"
        for record in recording["records"]:
            record["last_price"] = record.pop("price")
        self.replay.stream_connect_error = TimeoutError("Connection timed out after 10.0s")
        output = self.output_path()

        with self.assertRaises(SystemExit) as caught:
            self.run_main(output)

        self.assert_nothing_published(output, caught.exception)

    def test_renamed_viop_price_column_keeps_stream_prices(self):
        """Derived: the VİOP table's ``price`` column renamed."""
        recording = self.replay.viop_futures
        recording["columns"][recording["columns"].index("price")] = "last_price"
        for record in recording["records"]:
            record["last_price"] = record.pop("price")
        self.replay.stream_timeouts.add("USDTRYZ2026")
        snapshot = self.build()

        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        self.assertIsNone(dec["last"])
        self.assertEqual(dec["status"], "unavailable")
        jan = self.contract(snapshot, "USDTRY", "USDTRYF2027")
        self.assertEqual(jan["last"], 53.514)
        self.assertEqual(jan["source"], "TradingView via borsapy")

    def test_undone_viop_column_remap_drops_change_and_turnover(self):
        """Derived: the table as it would read if borsapy undid its 0.10.2 remap.

        ``volume_tl`` then carries the recorded TRY turnover and ``volume_qty``
        a contract count (turnover / price / 1,000, rounded; derived, not
        recorded). Read with the 0.10.2 mapping, that would publish a
        turnover as the price change and a count as TRY turnover.
        """
        for record in self.replay.viop_futures["records"]:
            turnover = record["volume_qty"]
            record["volume_tl"] = turnover
            record["volume_qty"] = float(round(turnover / record["price"] / 1_000))
        self.replay.stream_timeouts.add("USDTRYZ2026")
        snapshot = self.build()

        for market in MARKET_KEYS:
            for item in snapshot["markets"][market]["contracts"]:
                with self.subTest(symbol=item["symbol"]):
                    self.assertIsNone(item["turnover_try"])
        dec = self.contract(snapshot, "USDTRY", "USDTRYZ2026")
        self.assertEqual(dec["last"], 52.262)
        self.assertIsNone(dec["change"])  # not 11,922,511,259
        self.assertEqual(dec["change_percent"], 0.02)
        self.assertTrue(any("column remap" in line for line in self.printed))


if __name__ == "__main__":
    unittest.main()
