"""Replay recorded borsapy responses in place of ``scripts.update_data.bp``.

The recordings in ``tests/fixtures/borsapy/`` are real borsapy 0.10.2
responses (see the README there). ``BorsapyReplay`` loads a fresh copy of them;
a test edits that copy to derive a quirk or drift variant, then patches
``scripts.update_data.bp`` with ``replay.module()``. Nothing else in the updater
is replaced, so every ``fetch_*`` function runs for real against the replay.

A call the recording can't answer (a different history period, an asset that
was never recorded) raises and is logged in ``replay.unrecorded``. The updater
swallows some upstream errors on purpose, so tests check that list to make sure
a changed upstream call can't pass by silently falling back.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "borsapy"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def frame_from_recording(recording: dict[str, Any]) -> pd.DataFrame:
    """Rebuild a borsapy history DataFrame from its JSON recording."""
    index = pd.DatetimeIndex(
        pd.to_datetime(recording["index"], format="ISO8601") if recording["index"] else [],
        name=recording.get("index_name"),
    )
    timezone = recording.get("index_tz")
    if timezone:
        index = (
            index.tz_convert(timezone) if index.tz is not None else index.tz_localize(timezone)
        )
    return pd.DataFrame(recording["data"], columns=recording["columns"], index=index)


class UnrecordedCall(LookupError):
    """The updater asked borsapy for something the recording doesn't hold."""


class BorsapyReplay:
    """A stand-in for the ``borsapy`` module that serves recorded responses."""

    def __init__(self) -> None:
        # FX(asset).history(period="1g", interval="15m"), keyed by asset.
        self.fx_15m: dict[str, dict[str, Any]] = load_fixture("fx_history_15m.json")
        # FX(asset).history(start=..., end=...) daily closes, keyed by asset.
        self.fx_daily: dict[str, dict[str, Any]] = load_fixture("fx_history_daily.json")
        # FX(asset).current, keyed by asset; update_time is a naive Timestamp.
        self.fx_current: dict[str, dict[str, Any]] = load_fixture("fx_current.json")
        # VIOP().futures: {"columns", "dtypes", "records"}.
        self.viop_futures: dict[str, Any] = load_fixture("viop_futures.json")
        self.viop_futures_error: Exception | None = None
        # viop_contracts(symbol, full_info=True), keyed by futures symbol.
        self.viop_contracts: dict[str, list[dict[str, Any]]] = load_fixture(
            "viop_contracts.json"
        )
        # TradingViewStream quotes, keyed by contract symbol.
        self.quotes: dict[str, dict[str, Any]] = load_fixture("stream_quotes.json")
        self.stream_connect_error: Exception | None = None
        # Symbols whose quote never arrives (wait_for_quote times out).
        self.stream_timeouts: set[str] = set()
        # Symbols whose wait_for_quote raises the given error instead.
        self.quote_errors: dict[str, Exception] = {}

        self.unrecorded: list[str] = []
        self.streams_opened = 0

    def unrecorded_call(self, description: str) -> UnrecordedCall:
        self.unrecorded.append(description)
        return UnrecordedCall(f"no recorded borsapy response for {description}")

    def module(self) -> SimpleNamespace:
        replay = self

        class FX:
            def __init__(self, asset: str) -> None:
                self.asset = asset

            def history(
                self,
                period: str | None = None,
                interval: str | None = None,
                start: str | None = None,
                end: str | None = None,
            ) -> pd.DataFrame:
                call = (
                    f"FX({self.asset!r}).history(period={period!r}, "
                    f"interval={interval!r}, start={start!r}, end={end!r})"
                )
                if (period, interval, start, end) == ("1g", "15m", None, None):
                    if self.asset not in replay.fx_15m:
                        raise replay.unrecorded_call(call)
                    return frame_from_recording(replay.fx_15m[self.asset])
                if period is None and interval is None and start and end:
                    if self.asset not in replay.fx_daily:
                        raise replay.unrecorded_call(call)
                    frame = frame_from_recording(replay.fx_daily[self.asset])
                    # Serve only recorded rows whose day is inside the requested
                    # window (borsapy includes the end day), so a shifted window
                    # can't silently get the recorded closes.
                    days = frame.index.normalize()
                    return frame[
                        (days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))
                    ]
                raise replay.unrecorded_call(call)

            @property
            def current(self) -> dict[str, Any]:
                if self.asset not in replay.fx_current:
                    raise replay.unrecorded_call(f"FX({self.asset!r}).current")
                raw = copy.deepcopy(replay.fx_current[self.asset])
                if isinstance(raw.get("update_time"), str):
                    raw["update_time"] = pd.Timestamp(raw["update_time"])
                return raw

        class VIOP:
            @property
            def futures(self) -> pd.DataFrame:
                if replay.viop_futures_error is not None:
                    raise replay.viop_futures_error
                recording = replay.viop_futures
                return pd.DataFrame.from_records(
                    copy.deepcopy(recording["records"]),
                    columns=recording["columns"],
                )

        def viop_contracts(symbol: str, full_info: bool = False) -> list[dict[str, Any]]:
            if not full_info or symbol not in replay.viop_contracts:
                raise replay.unrecorded_call(
                    f"viop_contracts({symbol!r}, full_info={full_info!r})"
                )
            return copy.deepcopy(replay.viop_contracts[symbol])

        class TradingViewStream:
            def __init__(self) -> None:
                replay.streams_opened += 1
                self.connected = False
                self.subscribed: set[str] = set()

            def connect(self, timeout: float = 10.0) -> bool:
                if replay.stream_connect_error is not None:
                    raise replay.stream_connect_error
                self.connected = True
                return True

            def subscribe(self, symbol: str, exchange: str = "BIST") -> None:
                self.subscribed.add(symbol.upper())

            def _received(self, symbol: str) -> dict[str, Any] | None:
                if (
                    not self.connected
                    or symbol not in self.subscribed
                    or symbol in replay.stream_timeouts
                    or symbol in replay.quote_errors
                ):
                    return None
                quote = replay.quotes.get(symbol)
                return copy.deepcopy(quote) if quote is not None else None

            def wait_for_quote(self, symbol: str, timeout: float = 5.0) -> dict[str, Any]:
                symbol = symbol.upper()
                if symbol in replay.quote_errors:
                    raise replay.quote_errors[symbol]
                quote = self._received(symbol)
                if quote is None:
                    raise TimeoutError(f"Timeout waiting for quote: {symbol}")
                return quote

            def get_quote(self, symbol: str) -> dict[str, Any] | None:
                return self._received(symbol.upper())

            def disconnect(self) -> None:
                self.connected = False

        return SimpleNamespace(
            FX=FX,
            VIOP=VIOP,
            viop_contracts=viop_contracts,
            TradingViewStream=TradingViewStream,
        )
