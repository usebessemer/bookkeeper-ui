"""Deriving the ledger period a transaction belongs to.

The framework treats `period` as an **opaque string** the read side is queried
by (`LedgerSource.fetch_for_period(period)`) — e.g. ``"2026-Q2"`` (see the
framework's own `tests/fakes.py`, which key transactions by that string). The
framework never derives a period from a date; that mapping is the store
adapter's job. This file store has to *place* each imported transaction into a
period from its `date`, and then answer `fetch_for_period` consistently with how
it filed. That single mapping lives here: **calendar quarters**,
``"{year}-Q{quarter}"``.

Kept as one tiny, documented function so #2/#3 and the later slices share one
period convention rather than each re-deriving it.
"""

from __future__ import annotations

from datetime import datetime


def period_of(date: datetime) -> str:
    """The calendar-quarter period string a transaction dated `date` belongs to.

    ``datetime(2026, 4, 3)`` → ``"2026-Q2"``. Quarters are the framework's own
    period granularity in its tests (``"2026-Q2"``), so a store that files by
    this and a skill that reads ``fetch_for_period("2026-Q2")`` line up.
    """
    quarter = (date.month - 1) // 3 + 1
    return f"{date.year}-Q{quarter}"
