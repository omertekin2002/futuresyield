"""Türkiye holiday and VİOP session calendar shared by the updater and the archive.

Kept free of borsapy so the archive job can run with only `holidays` installed.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays
from holidays.constants import HALF_DAY, PUBLIC


ISTANBUL = ZoneInfo("Europe/Istanbul")

# Borsa İstanbul VİOP trading hours (Istanbul time). The opening session starts
# at 09:20; the evening session, which includes FX and precious-metal futures,
# ends at 23:00. Half days have no evening session and close at 12:40.
SESSION_OPEN = time(9, 20)
SESSION_CLOSE = time(23, 0)
HALF_DAY_CLOSE = time(12, 40)
# VİOP quotes may be delayed by about 15 minutes, so a snapshot only counts as
# end-of-day once the close has had time to reach the data feed.
QUOTE_DELAY = timedelta(minutes=15)


@lru_cache(maxsize=None)
def public_holidays(year: int) -> frozenset[date]:
    """Return Türkiye public holidays, when VİOP does not trade at all."""
    return frozenset(
        holidays.country_holidays("TR", years=year, observed=False, categories=PUBLIC)
    )


@lru_cache(maxsize=None)
def half_days(year: int) -> frozenset[date]:
    """Return Türkiye half-day official holidays, when VİOP closes at midday."""
    return frozenset(
        holidays.country_holidays("TR", years=year, observed=False, categories=HALF_DAY)
    )


@lru_cache(maxsize=None)
def market_closures(year: int) -> frozenset[date]:
    """Return Türkiye public and half-day holidays that close VİOP for expiry."""
    return public_holidays(year) | half_days(year)


def is_trading_day(day: date) -> bool:
    """Return whether VİOP holds a session on this day (half days included)."""
    return day.weekday() < 5 and day not in public_holidays(day.year)


def previous_trading_day(day: date) -> date:
    candidate = day - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def session_close(day: date) -> time:
    return HALF_DAY_CLOSE if day in half_days(day.year) else SESSION_CLOSE


def session_date(moment: datetime) -> date:
    """Return the trading session whose quotes a snapshot taken then holds.

    From a trading day's 09:20 open until midnight that is the day itself, half
    days included. Before the open, and on weekends and public holidays, it is
    the previous trading day, whose session last closed.
    """
    local = moment.astimezone(ISTANBUL)
    day = local.date()
    if is_trading_day(day) and local >= datetime.combine(day, SESSION_OPEN, ISTANBUL):
        return day
    return previous_trading_day(day)


def trading_date(generated_at: datetime) -> date | None:
    """Return the closed trading session a snapshot represents.

    A snapshot taken after day D's close (plus the quote delay) counts as D, as
    does one taken before the next session opens, including over weekends and
    holidays. A snapshot taken while a session is running returns None. Any
    date returned is the snapshot's `session_date`.
    """
    local = generated_at.astimezone(ISTANBUL)
    day = session_date(local)
    settled = datetime.combine(day, session_close(day), ISTANBUL) + QUOTE_DELAY
    if local < settled:
        return None
    return day
