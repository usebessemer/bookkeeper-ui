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

import re
from datetime import datetime

# A well-formed quarterly period label: exactly the shape `period_of` emits —
# a four-digit year, ``-Q``, a single quarter 1–4. Matches the framework's own
# quarterly parse (`close_period._QUARTER_RE`) exactly, so a label that passes
# here is one `close_period`'s strictly-after `period_closeable` guard can order.
_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")


def period_of(date: datetime) -> str:
    """The calendar-quarter period string a transaction dated `date` belongs to.

    ``datetime(2026, 4, 3)`` → ``"2026-Q2"``. Quarters are the framework's own
    period granularity in its tests (``"2026-Q2"``), so a store that files by
    this and a skill that reads ``fetch_for_period("2026-Q2")`` line up.
    """
    quarter = (date.month - 1) // 3 + 1
    return f"{date.year}-Q{quarter}"


def is_quarterly_period(label: str) -> bool:
    """Whether `label` is a well-formed quarterly period label (``YYYY-Qn``, n 1–4).

    The sign precondition (issue D): a period is signable only under a label of
    the `period_of` convention — the one the file store actually files by, and the
    one the framework's `period_closeable` guard can order strictly. A garbage or
    empty label is refused **up front** (before any composition) so a signed close
    is never appended under a label the D4 effective-prior read can't order — an
    unparseable prior would fail-safe-BLOCK every future close forever.
    """
    return bool(_QUARTER_RE.match((label or "").strip()))
