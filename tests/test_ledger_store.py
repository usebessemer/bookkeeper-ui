"""The file ledger store: ports conformance, order, idempotency, round-trip."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from bookkeeper.ports import LedgerSink, LedgerSource

from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from tests.conftest import make_txn


def _store(tmp_path):
    return FileLedgerStore(tmp_path / "ledger.jsonl")


def test_satisfies_both_ports(tmp_path):
    """The store type-checks as both LedgerSink and LedgerSource (AC)."""
    store = _store(tmp_path)
    assert isinstance(store, LedgerSink)
    assert isinstance(store, LedgerSource)


async def test_store_then_fetch_preserves_insertion_order(tmp_path):
    store = _store(tmp_path)
    vendors = ["First", "Second", "Third"]
    for i, vendor in enumerate(vendors):
        await store.store(make_txn(vendor=vendor, date=datetime(2026, 5, 1 + i)))

    fetched = await store.fetch_for_period("2026-Q2")
    assert [t.vendor for t in fetched] == vendors  # deterministic insertion order


async def test_store_is_idempotent_on_stable_key(tmp_path):
    """Re-storing the same transaction is a no-op — no duplicate row (LedgerSink)."""
    store = _store(tmp_path)
    txn = make_txn(vendor="Acme")
    await store.store(txn)
    await store.store(txn)  # exact re-store
    await store.store(make_txn(vendor="Acme"))  # equal-by-value, same stable key

    fetched = await store.fetch_for_period("2026-Q2")
    assert len(fetched) == 1


async def test_fetch_filters_by_period(tmp_path):
    store = _store(tmp_path)
    await store.store(make_txn(vendor="Q1 txn", date=datetime(2026, 2, 14)))
    await store.store(make_txn(vendor="Q2 txn", date=datetime(2026, 5, 2)))

    q1 = await store.fetch_for_period("2026-Q1")
    q2 = await store.fetch_for_period("2026-Q2")
    assert [t.vendor for t in q1] == ["Q1 txn"]
    assert [t.vendor for t in q2] == ["Q2 txn"]


async def test_empty_period_returns_empty_list(tmp_path):
    store = _store(tmp_path)
    assert await store.fetch_for_period("2026-Q3") == []


async def test_decimal_and_bytes_round_trip(tmp_path):
    """Exact Decimal money and artifact_bytes survive the JSONL round-trip."""
    store = _store(tmp_path)
    txn = make_txn(
        amount="1234.56",
        tax="98.70",
        artifact_bytes=b"raw,source,row\n\x00\xff",
    )
    await store.store(txn)

    (fetched,) = await store.fetch_for_period("2026-Q2")
    assert fetched.amount == Decimal("1234.56")
    assert fetched.tax == Decimal("98.70")
    assert isinstance(fetched.amount, Decimal)
    assert fetched.artifact_bytes == b"raw,source,row\n\x00\xff"


async def test_persists_across_new_store_instances(tmp_path):
    """A fresh store on the same path reads back what an earlier instance filed."""
    path = tmp_path / "ledger.jsonl"
    await FileLedgerStore(path).store(make_txn(vendor="Persisted"))

    reopened = FileLedgerStore(path)
    (fetched,) = await reopened.fetch_for_period("2026-Q2")
    assert fetched.vendor == "Persisted"
    # And idempotency holds across instances (keys reloaded from disk).
    await reopened.store(make_txn(vendor="Persisted"))
    assert len(await reopened.fetch_for_period("2026-Q2")) == 1


def test_transaction_key_ignores_artifact_bytes():
    """The stable key is the business identity, independent of the source blob."""
    with_bytes = make_txn(artifact_bytes=b"one representation")
    without = make_txn(artifact_bytes=b"")
    assert transaction_key(with_bytes) == transaction_key(without)
