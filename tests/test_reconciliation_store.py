"""The file reconciliation-resolution store: append-only trail, last-write-wins overlay.

The reconcile analog of `test_confirmation_store.py`. Pins the audit-trail
discipline the projection relies on: `all()` keeps every row in order, a
correction is a *new* row (never an overwrite), and `latest_by_item()` collapses
the trail to the current decision per `(transaction_id, statement_line_id)` item —
either side nullable, but the two never conflated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from bookkeeper_ui.reconciliations import (
    DECISION_ACKNOWLEDGE,
    DECISION_CONFIRM,
    DECISION_REJECT,
    SOURCE_HUMAN,
    FileReconciliationStore,
    Reconciliation,
)


def _r(**overrides) -> Reconciliation:
    base = dict(
        transaction_id="txn-1",
        statement_line_id="stmt-1",
        decision=DECISION_CONFIRM,
        note="",
        source=SOURCE_HUMAN,
        decided_at=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Reconciliation(**base)  # type: ignore[arg-type]


def _store(tmp_path) -> FileReconciliationStore:
    return FileReconciliationStore(tmp_path / "reconciliations.jsonl")


async def test_empty_store_reads_empty(tmp_path):
    store = _store(tmp_path)
    assert await store.all() == []
    assert await store.latest_by_item() == {}


async def test_record_then_all_preserves_insertion_order(tmp_path):
    store = _store(tmp_path)
    await store.record(_r(statement_line_id="stmt-1", decision=DECISION_CONFIRM))
    await store.record(_r(statement_line_id="stmt-2", decision=DECISION_REJECT, note="not us"))
    trail = await store.all()
    assert [r.statement_line_id for r in trail] == ["stmt-1", "stmt-2"]


async def test_latest_by_item_is_last_write_wins(tmp_path):
    """A correction is a second row; `latest_by_item` returns the later one,
    `all()` still returns both (the audit trail is never rewritten)."""
    store = _store(tmp_path)
    await store.record(_r(decision=DECISION_CONFIRM, note="looks right"))
    await store.record(_r(decision=DECISION_REJECT, note="actually not the same charge"))

    assert len(await store.all()) == 2  # both rows kept for audit
    latest = await store.latest_by_item()
    assert len(latest) == 1  # collapsed to one current decision for the item
    current = latest[("txn-1", "stmt-1")]
    assert current.decision == DECISION_REJECT
    assert current.note == "actually not the same charge"


async def test_one_sided_ids_key_apart(tmp_path):
    """A pair, a ledger-only gap, and a statement-only gap are three distinct items."""
    store = _store(tmp_path)
    await store.record(_r(transaction_id="t", statement_line_id="s", decision=DECISION_CONFIRM))
    await store.record(_r(transaction_id="t", statement_line_id=None, decision=DECISION_ACKNOWLEDGE, note="timing"))
    await store.record(_r(transaction_id=None, statement_line_id="s", decision=DECISION_ACKNOWLEDGE, note="dupe"))

    latest = await store.latest_by_item()
    assert set(latest) == {("t", "s"), ("t", None), (None, "s")}


async def test_null_ids_round_trip_as_none_not_the_string(tmp_path):
    """A null id persists and reloads as `None`, never the string ``"None"``.

    A `str(None)` at the boundary would key a one-sided gap under ``"None"`` and
    silently break the overlay; the round-trip must keep it null.
    """
    path = tmp_path / "reconciliations.jsonl"
    store = FileReconciliationStore(path)
    await store.record(_r(transaction_id=None, statement_line_id="only-stmt", decision=DECISION_ACKNOWLEDGE, note="x"))

    # On disk the id is JSON null, not a string.
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["transaction_id"] is None

    # And a fresh instance reads it back as None (keyed correctly).
    (loaded,) = await FileReconciliationStore(path).all()
    assert loaded.transaction_id is None
    assert loaded.statement_line_id == "only-stmt"
    assert (await FileReconciliationStore(path).latest_by_item())[(None, "only-stmt")].note == "x"


async def test_persists_across_new_store_instances(tmp_path):
    path = tmp_path / "reconciliations.jsonl"
    await FileReconciliationStore(path).record(_r(note="persisted"))
    reopened = FileReconciliationStore(path)
    (loaded,) = await reopened.all()
    assert loaded.note == "persisted"
    assert loaded.decided_at == datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
