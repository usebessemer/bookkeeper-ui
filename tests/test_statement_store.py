"""The file statement store: port conformance, order, idempotency, round-trip."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from bookkeeper.ports import StatementSource

from bookkeeper_ui.statement_store import FileStatementStore, statement_line_key
from tests.conftest import make_stmt_line


def _store(tmp_path):
    return FileStatementStore(tmp_path / "statements.jsonl")


def test_satisfies_statement_source_port(tmp_path):
    """The store type-checks as a StatementSource (AC 1)."""
    assert isinstance(_store(tmp_path), StatementSource)


async def test_store_then_fetch_preserves_insertion_order(tmp_path):
    store = _store(tmp_path)
    refs = ["First", "Second", "Third"]
    for i, ref in enumerate(refs):
        await store.store(make_stmt_line(statement_ref=ref, date=datetime(2026, 5, 1 + i)))

    fetched = await store.fetch_statement("2026-Q2")
    assert [s.statement_ref for s in fetched] == refs  # deterministic insertion order


async def test_store_is_idempotent_on_stable_key(tmp_path):
    """Re-storing the same line is a no-op — no duplicate row (AC 3)."""
    store = _store(tmp_path)
    line = make_stmt_line(statement_ref="BANK-1")
    await store.store(line)
    await store.store(line)  # exact re-store
    await store.store(make_stmt_line(statement_ref="BANK-1"))  # equal-by-value, same key

    fetched = await store.fetch_statement("2026-Q2")
    assert len(fetched) == 1


async def test_distinct_refs_key_apart_even_when_otherwise_identical(tmp_path):
    """Two charges sharing amount/date/description but with distinct refs both persist.

    The statement key includes `statement_ref`, so genuinely-distinct feed lines
    are never collapsed into one — no silent under-count.
    """
    store = _store(tmp_path)
    await store.store(make_stmt_line(statement_ref="BANK-1", amount="5.00", description="COFFEE"))
    await store.store(make_stmt_line(statement_ref="BANK-2", amount="5.00", description="COFFEE"))

    assert len(await store.fetch_statement("2026-Q2")) == 2


async def test_fetch_filters_by_period(tmp_path):
    store = _store(tmp_path)
    await store.store(make_stmt_line(statement_ref="Q1", date=datetime(2026, 2, 14)))
    await store.store(make_stmt_line(statement_ref="Q2", date=datetime(2026, 5, 2)))

    q1 = await store.fetch_statement("2026-Q1")
    q2 = await store.fetch_statement("2026-Q2")
    assert [s.statement_ref for s in q1] == ["Q1"]
    assert [s.statement_ref for s in q2] == ["Q2"]


async def test_empty_period_returns_empty_list(tmp_path):
    store = _store(tmp_path)
    assert await store.fetch_statement("2026-Q3") == []


async def test_decimal_round_trips_exact(tmp_path):
    """Exact Decimal money survives the JSONL round-trip (AC 2)."""
    store = _store(tmp_path)
    await store.store(make_stmt_line(amount="1234.56", description="odd — charge"))

    (fetched,) = await store.fetch_statement("2026-Q2")
    assert fetched.amount == Decimal("1234.56")
    assert isinstance(fetched.amount, Decimal)
    assert fetched.description == "odd — charge"


async def test_amount_stored_as_string_not_float(tmp_path):
    """Money is serialized as a string on disk — never a JSON number (AC 5)."""
    import json

    path = tmp_path / "statements.jsonl"
    await FileStatementStore(path).store(make_stmt_line(amount="82.50"))

    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["amount"] == "82.50"
    assert isinstance(record["amount"], str)


async def test_persists_across_new_store_instances(tmp_path):
    """A fresh store on the same path reads back what an earlier instance filed."""
    path = tmp_path / "statements.jsonl"
    await FileStatementStore(path).store(make_stmt_line(statement_ref="Persisted"))

    reopened = FileStatementStore(path)
    (fetched,) = await reopened.fetch_statement("2026-Q2")
    assert fetched.statement_ref == "Persisted"
    # And idempotency holds across instances (keys reloaded from disk).
    await reopened.store(make_stmt_line(statement_ref="Persisted"))
    assert len(await reopened.fetch_statement("2026-Q2")) == 1


def test_statement_line_key_is_stable_and_natural():
    """The same logical line keys identically; a changed field keys differently."""
    base = make_stmt_line(statement_ref="BANK-1", amount="10.00", description="X")
    assert statement_line_key(base) == statement_line_key(
        make_stmt_line(statement_ref="BANK-1", amount="10.00", description="X")
    )
    assert statement_line_key(base) != statement_line_key(
        make_stmt_line(statement_ref="BANK-1", amount="10.01", description="X")
    )
