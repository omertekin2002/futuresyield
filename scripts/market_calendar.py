"""Türkiye holiday calendar shared by the updater and the history archive.

Kept free of borsapy so the archive job can run with only `holidays` installed.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays
from holidays.constants import HALF_DAY, PUBLIC


ISTANBUL = ZoneInfo("Europe/Istanbul")


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
