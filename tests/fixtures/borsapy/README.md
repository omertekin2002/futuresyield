# Recorded borsapy responses

Real responses from **borsapy 0.10.2** (the version pinned in
`requirements.txt`), captured on **2026-11-20 at 19:28:40 UTC (22:28:40
Istanbul time)**, a Friday, during the VİOP evening session.
`tests/test_update_data_replay.py` replays them through
`tests/borsapy_replay.py` in place of `scripts.update_data.bp`, and pins the
updater's clock to 22:30 Istanbul time that day.

| File | borsapy call | Kept |
| --- | --- | --- |
| `fx_history_15m.json` | `bp.FX(a).history(period="1g", interval="15m")` for `USD`, `EUR`, `XAU` | The last 2 bars of 19 Nov, the first 3 and last 3 bars of 20 Nov, and the bars that hold the futures' last trades: 17:45–18:15 for every asset (USD/TRY and gold last traded at 18:09) and 15:30 for `EUR` (EUR/TRY last traded at 15:35). Index name, time zone and every column as returned. |
| `fx_history_daily.json` | `bp.FX("USD").history(start="2026-05-10", end="2026-05-25")` | Every row. That is the window the updater asks for on 20 Nov (180 days back is 24 May, minus 14 days, plus 1). The index is tz-naive, as returned. |
| `fx_current.json` | `bp.FX(a).current` for `USD`, `EUR`, `gram-altin` | Every key. `update_time` is a naive `pandas.Timestamp` upstream, stored as its ISO string. |
| `viop_futures.json` | `bp.VIOP().futures` | Columns, dtypes, and every USDTRY, EURTRY and XAUTRYM row, including the weekly `TM_F_USDTRY261126` that the updater must ignore, plus two unrelated stock rows (`F_ENJSA1226`, `F_PETKM1226`). 150 other stock and index rows dropped. |
| `viop_contracts.json` | `bp.viop_contracts(s, full_info=True)` for `USDTRY`, `EURTRY`, `XAUTRY` | Every record, including the continuous `…1!` and `…2!` entries. |
| `stream_quotes.json` | `bp.TradingViewStream()`: `connect`, `subscribe` each dated contract, `wait_for_quote(s, 10)` | One quote per contract. Every key except `_raw`, the raw TradingView payload the updater never reads. |

The 15:30–18:15 bars were fetched with the same call on 22 Nov 2026, when it
still returned 20 Nov: the capture had trimmed them away. Completed bars don't
change upstream: every completed bar in the original capture matched that
later fetch, and only the 22:15 bar still in progress at capture had moved.

Quirks in the real data that the tests rely on:

- `bp.VIOP().futures` uses the 0.10.2 remap: `volume_tl` holds the absolute
  price change and `volume_qty` the TRY turnover.
- Several quotes (for example `USDTRYH2027`, `EURTRYZ2026`) arrived with
  `timestamp: None`. Some quotes carry `ask` as an integer.
- The daily history includes Mon 25 May, after the 180-day target.

Every other quirk and drift case (a missing `bid`, malformed numbers, an
expired contract, renamed columns and so on) is derived in the test by
editing a copy of these recordings, and each test's docstring says what it
changed. None of the files here is edited by hand.

## Re-recording after a borsapy upgrade

`HappyPathTests.test_snapshot_from_the_recording` fails when the installed
borsapy is not 0.10.2. After bumping it, re-record with the new version during
a VİOP session and update the assertions that change:

```python
import datetime, json
import borsapy as bp

for asset in ("USD", "EUR", "XAU"):
    frame = bp.FX(asset).history(period="1g", interval="15m")
    print(asset, frame.index.name, frame.index.tz, frame.columns.tolist())
    print(frame.tail(8).to_json(orient="split", date_format="iso"))

today = datetime.date.today()          # the snapshot's spot date
target = today - datetime.timedelta(days=180)
daily = bp.FX("USD").history(
    start=(target - datetime.timedelta(days=14)).isoformat(),
    end=(target + datetime.timedelta(days=1)).isoformat(),
)
print(daily.to_json(orient="split", date_format="iso"))

for asset in ("USD", "EUR", "gram-altin"):
    print(asset, bp.FX(asset).current)

futures = bp.VIOP().futures
print(futures.dtypes, futures.to_dict(orient="records"))

symbols = []
for symbol in ("USDTRY", "EURTRY", "XAUTRY"):
    contracts = bp.viop_contracts(symbol, full_info=True)
    print(json.dumps(contracts))
    symbols += [c["symbol"] for c in contracts if not c["is_continuous"]]

stream = bp.TradingViewStream()
stream.connect(timeout=10)
for symbol in symbols:
    stream.subscribe(symbol)
quotes = {s: stream.wait_for_quote(s, 10) for s in symbols}
stream.disconnect()
print(json.dumps(quotes, default=str))
```

Store each response in the matching file in the same shape (frames as
`index_name`, `index_tz`, `columns`, ISO `index` strings and `data` rows, with
NaN as `null`), trim as described above, and update the capture date here and
`NOW` in `tests/test_update_data_replay.py`.
