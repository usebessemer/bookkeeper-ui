"""The confirmation store: append-only trail, latest-wins, persistence."""

from __future__ import annotations

from datetime import datetime

from bookkeeper_ui.confirmations import (
    SOURCE_HUMAN,
    Confirmation,
    FileConfirmationStore,
)
from bookkeeper_ui.ledger_store import transaction_key
from tests.conftest import make_txn


async def test_records_and_reads_back_a_decision(tmp_path):
    """AC: the confirmation store persists and reads back a confirm/correct decision."""
    store = FileConfirmationStore(tmp_path / "confirmations.jsonl")
    txn_id = transaction_key(make_txn(vendor="Staples"))
    decision = Confirmation(
        transaction_id=txn_id,
        account="5000-office-supplies",
        source=SOURCE_HUMAN,
        decided_at=datetime(2026, 6, 1, 9, 0, 0),
    )
    await store.record(decision)

    (read_back,) = await store.all()
    assert read_back == decision


async def test_correction_supersedes_earlier_decision(tmp_path):
    """A correction is a new row; latest_by_transaction collapses to the last one."""
    store = FileConfirmationStore(tmp_path / "confirmations.jsonl")
    txn_id = transaction_key(make_txn(vendor="Delta Airlines"))
    confirm = Confirmation(txn_id, "5300-meals-entertainment", SOURCE_HUMAN, datetime(2026, 6, 1))
    correct = Confirmation(txn_id, "5200-travel", SOURCE_HUMAN, datetime(2026, 6, 2))
    await store.record(confirm)
    await store.record(correct)

    # Full audit trail keeps both, in order.
    assert await store.all() == [confirm, correct]
    # Current decision is the correction.
    latest = await store.latest_by_transaction()
    assert latest[txn_id].account == "5200-travel"


async def test_persists_across_instances(tmp_path):
    path = tmp_path / "confirmations.jsonl"
    txn_id = transaction_key(make_txn(vendor="WeWork"))
    await FileConfirmationStore(path).record(
        Confirmation(txn_id, "6100-rent", SOURCE_HUMAN, datetime(2026, 6, 10))
    )

    reopened = FileConfirmationStore(path)
    latest = await reopened.latest_by_transaction()
    assert latest[txn_id].account == "6100-rent"


async def test_empty_store_reads_empty(tmp_path):
    store = FileConfirmationStore(tmp_path / "confirmations.jsonl")
    assert await store.all() == []
    assert await store.latest_by_transaction() == {}
