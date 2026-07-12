"""The close store: self-contained snapshot round-trip, append-only, money-as-string.

Pins issue-A's foundation contract for `FileCloseStore` / `CloseRecord`: a signed
close persists and reads back verbatim, the trail is append-only, `by_period` /
`latest` collapse it correctly, and money in the snapshot lands as an exact
`Decimal` string — never a JSON number (a raw `float` is refused outright).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from bookkeeper_ui.closes import SOURCE_HUMAN, CloseRecord, FileCloseStore


def _close(period: str, **overrides: object) -> CloseRecord:
    """A minimal, valid `CloseRecord` for `period` — overridable per test."""
    fields: dict[str, object] = {
        "period": period,
        "signed_at": datetime(2026, 7, 11, 9, 0, 0),
        "signed_by": SOURCE_HUMAN,
        "checklist": [{"name": "period_closeable", "met": True, "reason": "ok"}],
        "transactions": [{"transaction_id": "abc", "account": "5000-office-supplies"}],
        "tax": {"regime": "HST", "period_total": "12.34"},
        "reconciliation": {"matched": 2, "gaps": 0},
        "anomalies": [{"flag_id": "f1", "kind": "duplicate", "reason": "r"}],
        "effective_prior_period_state": "2026-Q1",
        "config_prior_period_state": "2026-Q1",
    }
    fields.update(overrides)
    return CloseRecord(**fields)  # type: ignore[arg-type]


async def test_records_and_reads_back_a_close(tmp_path):
    """AC2: a signed close persists and reads back verbatim (the self-contained snapshot)."""
    store = FileCloseStore(tmp_path / "closes.jsonl")
    close = _close("2026-Q2")
    await store.record(close)

    (read_back,) = await store.all()
    assert read_back == close


async def test_all_is_insertion_order_and_append_only(tmp_path):
    """AC2: `all()` is the full trail in sign order; nothing rewrites earlier rows."""
    store = FileCloseStore(tmp_path / "closes.jsonl")
    q1, q2 = _close("2026-Q1", signed_at=datetime(2026, 4, 1)), _close("2026-Q2")
    await store.record(q1)
    await store.record(q2)

    assert await store.all() == [q1, q2]


async def test_by_period_and_latest(tmp_path):
    """`by_period` maps period→record; `latest` is the last appended close."""
    store = FileCloseStore(tmp_path / "closes.jsonl")
    q1, q2 = _close("2026-Q1", signed_at=datetime(2026, 4, 1)), _close("2026-Q2")
    await store.record(q1)
    await store.record(q2)

    by_period = await store.by_period()
    assert by_period["2026-Q1"] == q1 and by_period["2026-Q2"] == q2
    assert await store.latest() == q2


async def test_by_period_last_write_wins_defensively(tmp_path):
    """A period is signed once (enforced in D), but if the trail ever carries two,
    `by_period` collapses to the later while `all()` keeps both for audit."""
    store = FileCloseStore(tmp_path / "closes.jsonl")
    first = _close("2026-Q2", signed_by="human", reconciliation={"matched": 1, "gaps": 0})
    second = _close("2026-Q2", reconciliation={"matched": 9, "gaps": 0})
    await store.record(first)
    await store.record(second)

    assert await store.all() == [first, second]  # both retained
    assert (await store.by_period())["2026-Q2"] == second  # last write wins


async def test_money_in_snapshot_is_string_never_float(tmp_path):
    """AC6: money in the snapshot lands as an exact `Decimal` string, never a number.

    A `Decimal` handed in the payload is stringified at the store boundary — so
    the on-disk row carries strings for every money figure, at any depth.
    """
    import json

    path = tmp_path / "closes.jsonl"
    await FileCloseStore(path).record(
        _close(
            "2026-Q2",
            tax={
                "regime": "HST",
                "period_total": Decimal("12.34"),
                "per_target": [{"reclaimable": Decimal("5.00")}],
            },
        )
    )

    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["tax"]["period_total"] == "12.34"
    assert isinstance(record["tax"]["period_total"], str)
    assert isinstance(record["tax"]["per_target"][0]["reclaimable"], str)


async def test_raw_float_money_is_refused(tmp_path):
    """AC6: a raw `float` on any money path is refused — a lossy float never lands."""
    store = FileCloseStore(tmp_path / "closes.jsonl")
    close = _close("2026-Q2", tax={"regime": "HST", "period_total": 12.34})
    with pytest.raises(TypeError):
        await store.record(close)


async def test_null_prior_state_round_trips(tmp_path):
    """A close struck with no prior period on record stores/reads `None`, not "None"."""
    store = FileCloseStore(tmp_path / "closes.jsonl")
    await store.record(
        _close("2026-Q2", effective_prior_period_state=None, config_prior_period_state=None)
    )

    (read_back,) = await store.all()
    assert read_back.effective_prior_period_state is None
    assert read_back.config_prior_period_state is None


async def test_persists_across_instances(tmp_path):
    path = tmp_path / "closes.jsonl"
    await FileCloseStore(path).record(_close("2026-Q2"))

    reopened = FileCloseStore(path)
    assert "2026-Q2" in await reopened.by_period()


async def test_empty_store_reads_empty(tmp_path):
    store = FileCloseStore(tmp_path / "closes.jsonl")
    assert await store.all() == []
    assert await store.by_period() == {}
    assert await store.latest() is None
