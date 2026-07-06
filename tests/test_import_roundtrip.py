"""Import → store → fetch round-trip, and the CSV/JSON boundary rules (AC)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bookkeeper_ui.importer import (
    TransactionImportError,
    import_bytes,
    import_csv,
    import_json,
    row_to_transaction,
)
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key


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


@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN"])
def test_non_finite_amount_raises(bad):
    """Decimal accepts "Infinity"/"NaN" strings — the importer must not (#8)."""
    with pytest.raises(TransactionImportError, match="finite"):
        row_to_transaction(
            {
                "date": "2026-04-03",
                "vendor": "Staples",
                "amount": bad,
                "attribution_target_id": "target-001",
            }
        )


def test_non_finite_tax_raises():
    """`tax` goes through the same coercion — non-finite is rejected there too (#8)."""
    with pytest.raises(TransactionImportError, match="tax .* finite"):
        row_to_transaction(
            {
                "date": "2026-04-03",
                "vendor": "Staples",
                "amount": "10.00",
                "tax": "NaN",
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


# --- import_bytes: the upload boundary (#2) --------------------------------


def test_import_bytes_json_number_amount_is_exact_decimal():
    """An *unquoted* JSON amount stays exact — never a lossy float (B1).

    JSON is parsed with `parse_float=Decimal`, so a big or awkward number cannot
    round-trip through a binary float before becoming money.
    """
    (txn,) = import_bytes(
        b'[{"date": "2026-04-03", "vendor": "X", '
        b'"amount": 12345678901234567.89, "attribution_target_id": "t"}]',
        "u.json",
    )
    assert txn.amount == Decimal("12345678901234567.89")
    # And a plain "82.50" keeps its trailing zero, so the stable key is stable.
    (half,) = import_bytes(
        b'[{"date": "2026-04-03", "vendor": "X", '
        b'"amount": 82.50, "attribution_target_id": "t"}]',
        "u.json",
    )
    assert half.amount == Decimal("82.50")


def test_import_bytes_json_infinity_literal_raises():
    """A bare JSON `Infinity` literal (json's parse_constant path, not
    parse_float) must be rejected, not stored as `Decimal('Infinity')` (#8)."""
    with pytest.raises(TransactionImportError, match="finite"):
        import_bytes(
            b'[{"date": "2026-04-03", "vendor": "X", '
            b'"amount": Infinity, "attribution_target_id": "t"}]',
            "u.json",
        )


def test_import_bytes_malformed_json_raises():
    """A malformed JSON upload is a named error, not a 500 (B2)."""
    with pytest.raises(TransactionImportError):
        import_bytes(b"{not valid json", "u.json")


def test_import_bytes_non_object_row_raises():
    """A JSON row that is not an object is named, not an AttributeError 500 (B2)."""
    with pytest.raises(TransactionImportError):
        import_bytes(b"[1, 2, 3]", "u.json")


def test_import_bytes_missing_transactions_key_raises():
    """A typo'd wrapper ({"txns": ...}) is a named error, not a silent 0-row import (F2)."""
    with pytest.raises(TransactionImportError):
        import_bytes(b'{"txns": []}', "u.json")


def test_import_bytes_non_utf8_raises():
    """Non-UTF-8 bytes are a named error, not an unhandled decode 500 (B2)."""
    with pytest.raises(TransactionImportError):
        import_bytes(b"\xff\xfe\x00garbage", "u.csv")


def test_import_bytes_unsupported_suffix_raises():
    with pytest.raises(TransactionImportError):
        import_bytes(b"anything", "notes.txt")


def test_import_bytes_ragged_csv_row_raises():
    """A CSV row with more values than headers is named, not a serialization 500 (F1)."""
    with pytest.raises(TransactionImportError):
        import_bytes(
            b"date,vendor,amount,attribution_target_id\n"
            b"2026-04-03,X,10.00,t,STRAY_EXTRA_FIELD\n",
            "u.csv",
        )


def test_csv_path_and_bytes_agree_on_crlf_quoted_field(tmp_path):
    """The path and bytes CSV importers key the same file identically (F3).

    A CRLF file with a quoted embedded newline must parse the same whether it
    arrives as a path (`import_csv`) or an upload (`import_bytes`), or the same
    logical transaction would key two different ledger rows across the two paths.
    """
    raw = (
        b"date,vendor,amount,attribution_target_id\r\n"
        b'2026-04-03,"Acme\r\nWest",10.00,t\r\n'
    )
    path = tmp_path / "crlf.csv"
    path.write_bytes(raw)

    (from_bytes,) = import_bytes(raw, "crlf.csv")
    (from_path,) = import_csv(path)
    assert from_bytes.vendor == "Acme\r\nWest"  # embedded newline preserved
    assert transaction_key(from_bytes) == transaction_key(from_path)
