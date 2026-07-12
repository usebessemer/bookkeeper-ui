"""Closed-period write guards on every existing write path (issue-A AC5).

Closed truth is `FileCloseStore.by_period()` — a period is closed iff a close
record exists for it. Here the close is *seeded directly* into the store (the sign
flow is issue D). Once a period is closed, every write path refuses anything that
lands in it — imports (whole upload, nothing persisted, JSON 400 / UI partial),
`/resolve` and `/reconcile/resolve` (JSON 409, UI renders the refusal) — while an
*unknown* id keeps its exact pre-Slice-3 N1 behaviour (404), and an unrelated open
period is never over-refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper_ui.api import create_app
from bookkeeper_ui.closes import CloseRecord, FileCloseStore
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore, statement_line_key
from tests.conftest import make_stmt_line, make_txn

_ACCOUNT = "5000-office-supplies"  # a valid chart account (passes the §5.2 guard)


@dataclass
class GuardHarness:
    app: FastAPI
    ledger_store: FileLedgerStore
    statement_store: FileStatementStore
    close_store: FileCloseStore


@pytest.fixture
def harness(tmp_path, examples_dir) -> GuardHarness:
    ledger_store = FileLedgerStore(tmp_path / "ledger.jsonl")
    statement_store = FileStatementStore(tmp_path / "statements.jsonl")
    close_store = FileCloseStore(tmp_path / "closes.jsonl")
    app = create_app(
        config=load_config(examples_dir / "config.json"),
        ledger_store=ledger_store,
        confirmation_store=FileConfirmationStore(tmp_path / "confirmations.jsonl"),
        statement_store=statement_store,
        reconciliation_store=FileReconciliationStore(tmp_path / "reconciliations.jsonl"),
        close_store=close_store,
    )
    return GuardHarness(app, ledger_store, statement_store, close_store)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _close(store: FileCloseStore, period: str) -> None:
    """Seed a signed close for `period` directly (the sign flow is issue D)."""
    await store.record(
        CloseRecord(
            period=period,
            signed_at=datetime(2026, 7, 11, 9, 0, 0),
            signed_by="human",
            checklist=[],
            transactions=[],
            tax={},
            reconciliation={},
            anomalies=[],
            effective_prior_period_state=None,
            config_prior_period_state=None,
        )
    )


# --- /resolve (confirm/correct) ---------------------------------------------


async def test_resolve_refuses_known_transaction_in_closed_period(harness):
    """A known transaction in a closed period → 409 (books write-guarded)."""
    txn = make_txn(vendor="Staples", date=datetime(2026, 5, 15))  # 2026-Q2
    await harness.ledger_store.store(txn)
    await _close(harness.close_store, "2026-Q2")

    async with _client(harness.app) as client:
        resp = await client.post(
            "/resolve", json={"transaction_id": transaction_key(txn), "account": _ACCOUNT}
        )
    assert resp.status_code == 409
    assert "2026-Q2" in resp.json()["detail"]


async def test_resolve_unknown_transaction_still_404(harness):
    """AC5 (N1 untouched): an unknown id in no closed period keeps its 404."""
    await _close(harness.close_store, "2026-Q2")

    async with _client(harness.app) as client:
        resp = await client.post(
            "/resolve", json={"transaction_id": "not-a-real-id", "account": _ACCOUNT}
        )
    assert resp.status_code == 404


async def test_resolve_open_period_transaction_still_succeeds(harness):
    """A transaction in an *open* period resolves normally — no over-refusal."""
    txn = make_txn(vendor="Staples", date=datetime(2026, 5, 15))  # 2026-Q2, open
    await harness.ledger_store.store(txn)
    await _close(harness.close_store, "2026-Q3")  # a different period is closed

    async with _client(harness.app) as client:
        resp = await client.post(
            "/resolve", json={"transaction_id": transaction_key(txn), "account": _ACCOUNT}
        )
    assert resp.status_code == 200


# --- /import + /statements/import (whole-upload refusal) ---------------------


async def test_import_refuses_whole_upload_touching_closed_period(harness, examples_dir):
    """AC5: an import with any row in a closed period persists nothing, names the rows."""
    await _close(harness.close_store, "2026-Q2")  # 5 of the 6 example rows land here

    async with _client(harness.app) as client:
        resp = await client.post(
            "/import",
            files={"file": ("t.csv", (examples_dir / "transactions.csv").read_bytes(), "text/csv")},
        )
    assert resp.status_code == 400
    assert "2026-Q2" in resp.json()["detail"]
    # Nothing persisted — not even the one row (2026-Q1) that was in an open period.
    assert await harness.ledger_store.fetch_for_period("2026-Q2") == []
    assert await harness.ledger_store.fetch_for_period("2026-Q1") == []


async def test_import_into_only_open_periods_succeeds(harness, examples_dir):
    """Closing an unrelated period never blocks an import that avoids it."""
    await _close(harness.close_store, "2026-Q4")  # no example row lands here

    async with _client(harness.app) as client:
        resp = await client.post(
            "/import",
            files={"file": ("t.csv", (examples_dir / "transactions.csv").read_bytes(), "text/csv")},
        )
    assert resp.status_code == 200
    assert len(await harness.ledger_store.fetch_for_period("2026-Q2")) == 5


async def test_statement_import_refuses_whole_upload_touching_closed_period(harness, examples_dir):
    """AC5: a statement import touching a closed period persists nothing, names the rows."""
    await _close(harness.close_store, "2026-Q2")  # every example statement line lands here

    async with _client(harness.app) as client:
        resp = await client.post(
            "/statements/import",
            files={"file": ("s.csv", (examples_dir / "statements.csv").read_bytes(), "text/csv")},
        )
    assert resp.status_code == 400
    assert "2026-Q2" in resp.json()["detail"]
    assert await harness.statement_store.fetch_statement("2026-Q2") == []


# --- /reconcile/resolve (either side in a closed period) ---------------------


async def test_reconcile_resolve_refuses_transaction_side_in_closed_period(harness):
    """A resolution whose transaction side is in a closed period → 409."""
    txn = make_txn(vendor="Staples", date=datetime(2026, 5, 15))
    await harness.ledger_store.store(txn)
    await _close(harness.close_store, "2026-Q2")

    async with _client(harness.app) as client:
        resp = await client.post(
            "/reconcile/resolve",
            json={
                "transaction_id": transaction_key(txn),
                "statement_line_id": None,
                "decision": "acknowledge",
                "note": "seen",
            },
        )
    assert resp.status_code == 409
    assert "2026-Q2" in resp.json()["detail"]


async def test_reconcile_resolve_refuses_statement_side_in_closed_period(harness):
    """A resolution whose statement side is in a closed period → 409."""
    line = make_stmt_line(statement_ref="BANK-1", date=datetime(2026, 5, 15))
    await harness.statement_store.store(line)
    await _close(harness.close_store, "2026-Q2")

    async with _client(harness.app) as client:
        resp = await client.post(
            "/reconcile/resolve",
            json={
                "transaction_id": None,
                "statement_line_id": statement_line_key(line),
                "decision": "acknowledge",
                "note": "seen",
            },
        )
    assert resp.status_code == 409


async def test_reconcile_resolve_unknown_id_still_404(harness):
    """N1 untouched: an unknown id in no closed period keeps its 404."""
    await _close(harness.close_store, "2026-Q2")

    async with _client(harness.app) as client:
        resp = await client.post(
            "/reconcile/resolve",
            json={
                "transaction_id": "not-a-real-id",
                "statement_line_id": None,
                "decision": "acknowledge",
                "note": "seen",
            },
        )
    assert resp.status_code == 404


# --- the UI twin renders the refusal (not a machine 4xx) --------------------


async def test_ui_resolve_renders_refusal_for_closed_period(harness):
    """AC5: `/ui/resolve` renders the refusal into the page (a 200 partial)."""
    txn = make_txn(vendor="Staples", date=datetime(2026, 5, 15))
    await harness.ledger_store.store(txn)
    await _close(harness.close_store, "2026-Q2")

    async with _client(harness.app) as client:
        resp = await client.post(
            "/ui/resolve",
            data={"transaction_id": transaction_key(txn), "account": _ACCOUNT, "period": "2026-Q2"},
        )
    assert resp.status_code == 200
    assert "closed" in resp.text.lower()
    assert "2026-Q2" in resp.text


async def test_ui_reconcile_resolve_renders_refusal_for_closed_period(harness):
    """The reconcile UI twin also renders the refusal into the page (a 200 partial)."""
    txn = make_txn(vendor="Staples", date=datetime(2026, 5, 15))
    await harness.ledger_store.store(txn)
    await _close(harness.close_store, "2026-Q2")

    async with _client(harness.app) as client:
        resp = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "acknowledge",
                "transaction_id": transaction_key(txn),
                "note": "seen",
                "period": "2026-Q2",
            },
        )
    assert resp.status_code == 200
    assert "closed" in resp.text.lower()
    assert "2026-Q2" in resp.text


# --- the guards are inert when no close store is wired (Slice 1/2 behaviour) -


async def test_guards_inert_without_close_store(tmp_path, examples_dir):
    """With no close store injected (the shipped call sites), no period is closed."""
    ledger_store = FileLedgerStore(tmp_path / "ledger.jsonl")
    app = create_app(
        config=load_config(examples_dir / "config.json"),
        ledger_store=ledger_store,
        confirmation_store=FileConfirmationStore(tmp_path / "confirmations.jsonl"),
        statement_store=FileStatementStore(tmp_path / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(tmp_path / "reconciliations.jsonl"),
    )
    txn = make_txn(vendor="Staples", date=datetime(2026, 5, 15))
    await ledger_store.store(txn)

    async with _client(app) as client:
        resp = await client.post(
            "/resolve", json={"transaction_id": transaction_key(txn), "account": _ACCOUNT}
        )
    assert resp.status_code == 200  # nothing is closed → resolve succeeds
