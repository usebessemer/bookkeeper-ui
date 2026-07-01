"""Import → store → fetch round-trip, and the CSV/JSON boundary rules (AC)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bookkeeper_ui.importer import (
    TransactionImportError,
    import_csv,
    import_json,
    row_to_transaction,
)
from bookkeeper_ui.ledger_store import FileLedgerStore


async def test_csv_import_roundtrip_deterministic_order(examples_dir, tmp_path):
    """AC: import the sample CSV → fetch_for_period returns them, deterministic order."""
    store = FileLedgerStore(tmp_path / "ledger.jsonl")
    imported = import_csv(examples_dir / "transactions.csv")
    for txn in imported:
        await store.store(txn)

    # The sample set has 5 Q2 transactions and 1 Q1 transaction.
    q2 = await store.fetch_for_period("2026-Q2")
    q1 = await store.fetch_for_period("2026-Q1")
    assert [t.vendor for t in q2] == [
        "Staples",
        "AWS",
        "Delta Airlines",
        "Blue Bottle Coffee",
        "WeWork",
    ]
    assert [t.vendor for t in q1] == ["GitHub"]


def test_csv_and_json_imports_are_equal(examples_dir):
    """The two import paths produce identical Transaction objects for the same data."""
    from_csv = import_csv(examples_dir / "transactions.csv")
    from_json = import_json(examples_dir / "transactions.json")
    # Compare on the business fields (artifact_bytes differ: CSV vs JSON source rows).
    def business(t):
        return (t.attribution_target_id, t.vendor, t.amount, t.tax, t.date, t.description)

    assert [business(t) for t in from_csv] == [business(t) for t in from_json]


def test_blank_tax_coalesces_to_zero():
    """Absent / blank tax becomes Decimal('0') — the framework never holds None-money."""
    txn = row_to_transaction(
        {
            "date": "2026-04-15",
            "vendor": "AWS",
            "amount": "240.00",
            "tax": "",
            "attribution_target_id": "target-001",
        }
    )
    assert txn.tax == Decimal("0")
    assert isinstance(txn.tax, Decimal)


def test_money_is_exact_decimal():
    txn = row_to_transaction(
        {
            "date": "2026-04-03",
            "vendor": "Staples",
            "amount": "82.50",
            "tax": "6.60",
            "attribution_target_id": "target-001",
        }
    )
    assert txn.amount == Decimal("82.50")
    assert txn.tax == Decimal("6.60")


def test_missing_required_field_raises():
    with pytest.raises(TransactionImportError):
        row_to_transaction({"vendor": "Staples", "amount": "10.00"})  # no date/target


def test_bad_amount_raises():
    with pytest.raises(TransactionImportError):
        row_to_transaction(
            {
                "date": "2026-04-03",
                "vendor": "Staples",
                "amount": "not-a-number",
                "attribution_target_id": "target-001",
            }
        )


async def test_reimport_is_idempotent(examples_dir, tmp_path):
    """Importing the same file twice does not duplicate rows (idempotent sink)."""
    store = FileLedgerStore(tmp_path / "ledger.jsonl")
    for _ in range(2):
        for txn in import_csv(examples_dir / "transactions.csv"):
            await store.store(txn)

    total = len(await store.fetch_for_period("2026-Q1")) + len(
        await store.fetch_for_period("2026-Q2")
    )
    assert total == 6
