"""Statement import → store → fetch round-trip, and the CSV/JSON boundary rules (AC)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bookkeeper_ui.statement_importer import (
    StatementImportError,
    import_bytes,
    import_csv,
    import_file,
    import_json,
    row_to_statement_line,
)
from bookkeeper_ui.statement_store import FileStatementStore, statement_line_key


async def test_csv_import_roundtrip_deterministic_order(examples_dir, tmp_path):
    """AC 1/2: import the sample CSV → fetch_statement returns them, deterministic order."""
    store = FileStatementStore(tmp_path / "statements.jsonl")
    for line in import_csv(examples_dir / "statements.csv"):
        await store.store(line)

    # The sample statement is five Q2 lines matching the Q2 ledger.
    q2 = await store.fetch_statement("2026-Q2")
    assert [s.statement_ref for s in q2] == [
        "STMT-2026Q2-001",
        "STMT-2026Q2-002",
        "STMT-2026Q2-003",
        "STMT-2026Q2-004",
        "STMT-2026Q2-005",
    ]
    assert q2[0].amount == Decimal("82.50")


def test_csv_and_json_imports_are_equal(examples_dir):
    """The two import paths produce identical StatementLine objects for the same data (AC 3).

    `StatementLine` carries no source-artifact field, so the two paths compare
    fully equal (unlike the transaction importer, whose `artifact_bytes` differ).
    """
    assert import_csv(examples_dir / "statements.csv") == import_json(
        examples_dir / "statements.json"
    )


def test_import_file_dispatches_by_suffix(examples_dir):
    """`import_file` (the path the example-data / #C reuse) dispatches .csv and .json."""
    assert import_file(examples_dir / "statements.csv") == import_file(
        examples_dir / "statements.json"
    )


def test_blank_description_coalesces_to_empty_string():
    """Absent / blank description becomes "" — StatementLine.description is a plain str."""
    line = row_to_statement_line(
        {"statement_ref": "BANK-1", "date": "2026-04-15", "amount": "240.00"}
    )
    assert line.description == ""


def test_money_is_exact_decimal():
    line = row_to_statement_line(
        {"statement_ref": "BANK-1", "date": "2026-04-03", "amount": "82.50"}
    )
    assert line.amount == Decimal("82.50")
    assert isinstance(line.amount, Decimal)


def test_missing_required_field_raises():
    """AC 4: a missing required field is a named error naming the row."""
    with pytest.raises(StatementImportError, match="row 0"):
        row_to_statement_line({"date": "2026-04-03", "amount": "10.00"})  # no ref


def test_missing_statement_ref_raises():
    """`statement_ref` is required — the §1 link to the feed cannot be absent."""
    with pytest.raises(StatementImportError, match="statement_ref"):
        row_to_statement_line({"date": "2026-04-03", "amount": "10.00", "description": "X"})


def test_bad_amount_raises():
    with pytest.raises(StatementImportError):
        row_to_statement_line(
            {"statement_ref": "BANK-1", "date": "2026-04-03", "amount": "not-a-number"}
        )


def test_bad_date_raises():
    with pytest.raises(StatementImportError, match="ISO 8601"):
        row_to_statement_line(
            {"statement_ref": "BANK-1", "date": "not-a-date", "amount": "10.00"}
        )


@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN"])
def test_non_finite_amount_raises(bad):
    """Decimal accepts "Infinity"/"NaN" strings — the importer must not (breaks match)."""
    with pytest.raises(StatementImportError, match="finite"):
        row_to_statement_line(
            {"statement_ref": "BANK-1", "date": "2026-04-03", "amount": bad}
        )


async def test_reimport_is_idempotent(examples_dir, tmp_path):
    """AC 3: importing the same file twice does not duplicate rows (idempotent store)."""
    store = FileStatementStore(tmp_path / "statements.jsonl")
    for _ in range(2):
        for line in import_csv(examples_dir / "statements.csv"):
            await store.store(line)

    assert len(await store.fetch_statement("2026-Q2")) == 5


async def test_reimport_across_formats_is_idempotent(examples_dir, tmp_path):
    """AC 3: the same logical line re-imported from a different format is the same key.

    Format is never part of identity — importing the CSV then the (identical) JSON
    adds zero rows.
    """
    store = FileStatementStore(tmp_path / "statements.jsonl")
    for line in import_csv(examples_dir / "statements.csv"):
        await store.store(line)
    for line in import_json(examples_dir / "statements.json"):
        await store.store(line)

    assert len(await store.fetch_statement("2026-Q2")) == 5


# --- import_bytes: the upload boundary (reused by #B POST /statements/import) ---


def test_import_bytes_json_number_amount_is_exact_decimal():
    """An *unquoted* JSON amount stays exact — never a lossy float (AC 2)."""
    (line,) = import_bytes(
        b'[{"statement_ref": "B1", "date": "2026-04-03", "amount": 12345678901234567.89}]',
        "u.json",
    )
    assert line.amount == Decimal("12345678901234567.89")
    # And a plain 82.50 keeps its trailing zero, so the stable key is stable.
    (half,) = import_bytes(
        b'[{"statement_ref": "B2", "date": "2026-04-03", "amount": 82.50}]',
        "u.json",
    )
    assert half.amount == Decimal("82.50")


def test_import_bytes_json_infinity_literal_raises():
    """A bare JSON `Infinity` literal must be rejected, not stored as Decimal('Infinity')."""
    with pytest.raises(StatementImportError, match="finite"):
        import_bytes(
            b'[{"statement_ref": "B1", "date": "2026-04-03", "amount": Infinity}]',
            "u.json",
        )


def test_import_bytes_json_wrapper_key_is_lines():
    """The wrapper key is `lines` (not `transactions`); the wrapper form is accepted."""
    (line,) = import_bytes(
        b'{"lines": [{"statement_ref": "B1", "date": "2026-04-03", "amount": "10.00"}]}',
        "u.json",
    )
    assert line.statement_ref == "B1"


def test_import_bytes_missing_lines_key_raises():
    """A typo'd wrapper ({"transactions": ...}) is a named error naming `lines`."""
    with pytest.raises(StatementImportError, match="lines"):
        import_bytes(b'{"transactions": []}', "u.json")


def test_import_bytes_malformed_json_raises():
    with pytest.raises(StatementImportError):
        import_bytes(b"{not valid json", "u.json")


def test_import_bytes_non_object_row_raises():
    with pytest.raises(StatementImportError):
        import_bytes(b"[1, 2, 3]", "u.json")


def test_import_bytes_non_utf8_raises():
    with pytest.raises(StatementImportError):
        import_bytes(b"\xff\xfe\x00garbage", "u.csv")


def test_import_bytes_unsupported_suffix_raises():
    with pytest.raises(StatementImportError):
        import_bytes(b"anything", "notes.txt")


def test_import_bytes_ragged_csv_row_raises():
    """A CSV row with more values than headers is named, not a silently mis-aligned line."""
    with pytest.raises(StatementImportError):
        import_bytes(
            b"statement_ref,date,amount,description\n"
            b"B1,2026-04-03,10.00,coffee,STRAY_EXTRA_FIELD\n",
            "u.csv",
        )


def test_bad_row_persists_nothing(tmp_path):
    """AC 4: a malformed file imports *nothing* — it fails before any store."""
    store = FileStatementStore(tmp_path / "statements.jsonl")
    with pytest.raises(StatementImportError):
        import_bytes(
            b"statement_ref,date,amount\nB1,2026-04-03,10.00\nB2,bad-date,20.00\n",
            "u.csv",
        )
    # import raised before returning any lines, so nothing reached the store.
    assert not (tmp_path / "statements.jsonl").exists()


def test_csv_path_and_bytes_agree_on_crlf_quoted_field(tmp_path):
    """The path and bytes CSV importers key the same file identically.

    A CRLF file with a quoted embedded newline must parse the same whether it
    arrives as a path (`import_csv`) or an upload (`import_bytes`), or the same
    logical line would key two different statement rows across the two paths.
    """
    raw = (
        b"statement_ref,date,amount,description\r\n"
        b'B1,2026-04-03,10.00,"ACME\r\nWEST"\r\n'
    )
    path = tmp_path / "crlf.csv"
    path.write_bytes(raw)

    (from_bytes,) = import_bytes(raw, "crlf.csv")
    (from_path,) = import_csv(path)
    assert from_bytes.description == "ACME\r\nWEST"  # embedded newline preserved
    assert statement_line_key(from_bytes) == statement_line_key(from_path)
