"""Slice 2 · B — the reconcile API + resolution store + overlaid projection, end to end.

Drives `create_app` with injected temp-path stores (the Slice 1 style), exercising:
statement import → the raw `reconcile_account` report (called as-is) → validated
resolutions → the overlaid view and its ledger fold. The framework skill is called
unmodified; the app writes only through its own reconciliation store.

The all-buckets fixture (`_populate_all_buckets`) is hand-built so a single Q2
reconcile run surfaces every bucket at once — one confident match, one divergent
`to_confirm`, and all three gap kinds — with a `reconcile_vendor` floor of 0.7
configured in-test (the shipped example config leaves it unset).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.reconcile import reconcile_account

from bookkeeper_ui.api import create_app
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore
from tests.conftest import make_stmt_line, make_txn

PERIOD = "2026-Q2"


# --- Harness + config builders ----------------------------------------------


@dataclass
class ReconHarness:
    app: FastAPI
    config: BookkeeperConfig
    ledger_store: FileLedgerStore
    statement_store: FileStatementStore
    reconciliation_store: FileReconciliationStore
    ledger_path: Path
    statements_path: Path
    reconciliations_path: Path


def _config(examples_dir: Path, *, reconcile_vendor: float | None) -> BookkeeperConfig:
    """The shipped example config with the `reconcile_vendor` floor set or omitted.

    The shipped `examples/config.json` deliberately leaves the floor unset; the
    tests that need it live build it here rather than mutating the example (per C's
    AC2: never mutate the shipped example to change a boundary under test).
    """
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    thresholds = dict(data.get("confidence_thresholds") or {})
    if reconcile_vendor is None:
        thresholds.pop("reconcile_vendor", None)
    else:
        thresholds["reconcile_vendor"] = reconcile_vendor
    data["confidence_thresholds"] = thresholds
    return BookkeeperConfig.from_mapping(data)


def _harness(tmp_path: Path, config: BookkeeperConfig) -> ReconHarness:
    ledger_path = tmp_path / "ledger.jsonl"
    statements_path = tmp_path / "statements.jsonl"
    reconciliations_path = tmp_path / "reconciliations.jsonl"
    ledger_store = FileLedgerStore(ledger_path)
    statement_store = FileStatementStore(statements_path)
    reconciliation_store = FileReconciliationStore(reconciliations_path)
    app = create_app(
        config=config,
        ledger_store=ledger_store,
        confirmation_store=FileConfirmationStore(tmp_path / "confirmations.jsonl"),
        statement_store=statement_store,
        reconciliation_store=reconciliation_store,
    )
    return ReconHarness(
        app,
        config,
        ledger_store,
        statement_store,
        reconciliation_store,
        ledger_path,
        statements_path,
        reconciliations_path,
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _populate_all_buckets(harness: ReconHarness) -> None:
    """File the hand-built Q2 fixture: one reconcile run yields every bucket.

    - Joe's Cafe / STMT-001 — confident match (mangled `SQ *` descriptor normalizes
      to the same vendor, similarity 1.0 ≥ 0.7 floor).
    - Delta Airlines / STMT-002 — amount+date agree, vendors diverge → `to_confirm`.
    - Staples 80.00 vs STMT-003 82.50 — date+vendor agree, amounts differ →
      `amount_mismatch` (signed delta -2.50).
    - STMT-004 — a statement charge with no ledger txn → `unmatched_in_ledger`.
    - WeWork — a captured txn with no statement line → `unmatched_on_statement`.
    """
    for txn in (
        make_txn(vendor="Joe's Cafe", amount="12.00", date=datetime(2026, 4, 10), description="Coffee"),
        make_txn(vendor="Delta Airlines", amount="500.00", date=datetime(2026, 5, 2), description="Flight"),
        make_txn(vendor="Staples", amount="80.00", date=datetime(2026, 4, 3), description="Paper"),
        make_txn(vendor="WeWork", amount="800.00", date=datetime(2026, 6, 10), description="Rent"),
    ):
        await harness.ledger_store.store(txn)

    for line in (
        make_stmt_line(statement_ref="STMT-001", description="SQ *JOE'S CAFE 415", amount="12.00", date=datetime(2026, 4, 11)),
        make_stmt_line(statement_ref="STMT-002", description="AMZN MKTP US*2Z3", amount="500.00", date=datetime(2026, 5, 3)),
        make_stmt_line(statement_ref="STMT-003", description="STAPLES STORE 123", amount="82.50", date=datetime(2026, 4, 3)),
        make_stmt_line(statement_ref="STMT-004", description="MYSTERY CHARGE", amount="45.00", date=datetime(2026, 5, 20)),
    ):
        await harness.statement_store.store(line)


@pytest.fixture
def floor_set(tmp_path, examples_dir) -> ReconHarness:
    return _harness(tmp_path, _config(examples_dir, reconcile_vendor=0.7))


@pytest.fixture
def floor_unset(tmp_path, examples_dir) -> ReconHarness:
    return _harness(tmp_path, _config(examples_dir, reconcile_vendor=None))


# --- AC3: report fidelity (buckets, order, verbatim reasons, signed delta) ---


async def test_reconcile_report_reproduces_framework_buckets_and_order(floor_set: ReconHarness):
    """POST /reconcile serializes the framework report exactly — same buckets, same
    order, reasons verbatim, delta a signed exact string, matched carries no score."""
    await _populate_all_buckets(floor_set)
    # The framework report is the source of truth to diff the wire against.
    model = await reconcile_account(
        floor_set.ledger_store, floor_set.statement_store, floor_set.config, PERIOD
    )

    async with _client(floor_set.app) as client:
        resp = await client.post("/reconcile", params={"period": PERIOD})
    assert resp.status_code == 200
    report = resp.json()
    assert report["period"] == PERIOD

    # matched — one confident pair, the trail only (NO confidence / similarity field).
    assert [m["statement_line"]["statement_ref"] for m in report["matched"]] == ["STMT-001"]
    assert "confidence" not in report["matched"][0]
    assert "vendor_similarity" not in report["matched"][0]
    assert report["matched"][0]["transaction"]["vendor"] == "Joe's Cafe"

    # to_confirm — one divergent pair; vendor_similarity is a JSON number; reason verbatim.
    assert [p["statement_line"]["statement_ref"] for p in report["to_confirm"]] == ["STMT-002"]
    ptc = report["to_confirm"][0]
    assert isinstance(ptc["vendor_similarity"], float)
    assert ptc["reason"] == model.to_confirm[0].reason  # verbatim from the framework

    # gaps — grouped amount_mismatch, then unmatched_in_ledger, then unmatched_on_statement.
    assert [g["kind"] for g in report["gaps"]] == [
        "amount_mismatch",
        "unmatched_in_ledger",
        "unmatched_on_statement",
    ]
    mismatch, in_ledger, on_statement = report["gaps"]

    # amount_mismatch: both sides, signed exact-Decimal delta as a string.
    assert mismatch["transaction"]["vendor"] == "Staples"
    assert mismatch["statement_line"]["statement_ref"] == "STMT-003"
    assert mismatch["delta"] == "-2.50"  # 80.00 - 82.50, signed, exact, trailing zero
    assert mismatch["delta"] == str(model.gaps[0].delta)
    assert mismatch["reason"] == model.gaps[0].reason  # verbatim

    # one-sided gaps carry only their side and a null delta.
    assert in_ledger["transaction"] is None
    assert in_ledger["statement_line"]["statement_ref"] == "STMT-004"
    assert in_ledger["delta"] is None
    assert on_statement["statement_line"] is None
    assert on_statement["transaction"]["vendor"] == "WeWork"
    assert on_statement["delta"] is None


# --- AC4: boundary honesty (the reconcile_vendor floor) ----------------------


async def test_unset_floor_surfaces_every_linked_pair_as_to_confirm(floor_unset: ReconHarness):
    """With `reconcile_vendor` unset the boundary is inert: nothing lands in
    `matched`; every amount+date link surfaces as `to_confirm` with the inert reason."""
    await _populate_all_buckets(floor_unset)
    async with _client(floor_unset.app) as client:
        resp = await client.post("/reconcile", params={"period": PERIOD})
    report = resp.json()

    assert report["matched"] == []  # inert → nothing silently accepted
    # Both amount+date links (Joe's Cafe AND Delta) now surface for confirmation.
    refs = {p["statement_line"]["statement_ref"] for p in report["to_confirm"]}
    assert refs == {"STMT-001", "STMT-002"}
    for pair in report["to_confirm"]:
        assert "not configured (inert)" in pair["reason"]


async def test_set_floor_lands_high_similarity_pair_in_matched(floor_set: ReconHarness):
    """With the floor set, a high-similarity mangled-descriptor pair is confident `matched`."""
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        resp = await client.post("/reconcile", params={"period": PERIOD})
    report = resp.json()
    assert [m["statement_line"]["statement_ref"] for m in report["matched"]] == ["STMT-001"]


# --- AC2: detection-only (POST /reconcile + GET /reconcile/view write nothing) ---


async def test_reconcile_surfaces_are_detection_only(floor_set: ReconHarness):
    """POST /reconcile and GET /reconcile/view leave all three JSONL files
    byte-identical; the only reconcile write path is /reconcile/resolve."""
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        # Record one resolution first, so reconciliations.jsonl exists and is snapshotted too.
        report = (await client.post("/reconcile", params={"period": PERIOD})).json()
        pair = report["to_confirm"][0]
        resolve = await client.post(
            "/reconcile/resolve",
            json={
                "transaction_id": pair["transaction"]["id"],
                "statement_line_id": pair["statement_line"]["id"],
                "decision": "confirm",
            },
        )
        assert resolve.status_code == 200

        before = {
            p: p.read_bytes()
            for p in (
                floor_set.ledger_path,
                floor_set.statements_path,
                floor_set.reconciliations_path,
            )
        }

        assert (await client.post("/reconcile", params={"period": PERIOD})).status_code == 200
        assert (await client.get("/reconcile/view", params={"period": PERIOD})).status_code == 200

    for path, snapshot in before.items():
        assert path.read_bytes() == snapshot, f"{path.name} changed under a read-only surface"


# --- AC5: resolution validation (422 shape guards, then 404 existence) -------


async def _ids(client: httpx.AsyncClient) -> dict[str, dict[str, str]]:
    """Real (transaction_id, statement_line_id) per bucket, from the raw report."""
    report = (await client.post("/reconcile", params={"period": PERIOD})).json()
    mismatch = report["gaps"][0]
    return {
        "to_confirm": {
            "transaction_id": report["to_confirm"][0]["transaction"]["id"],
            "statement_line_id": report["to_confirm"][0]["statement_line"]["id"],
        },
        "amount_mismatch": {
            "transaction_id": mismatch["transaction"]["id"],
            "statement_line_id": mismatch["statement_line"]["id"],
        },
    }


async def test_resolve_422_on_unknown_decision(floor_set: ReconHarness):
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        ids = (await _ids(client))["to_confirm"]
        resp = await client.post(
            "/reconcile/resolve",
            json={**ids, "decision": "frobnicate", "note": "x"},
        )
    assert resp.status_code == 422
    assert "unknown decision" in resp.json()["detail"]
    assert not floor_set.reconciliations_path.exists()


async def test_resolve_422_on_pair_decision_missing_an_id(floor_set: ReconHarness):
    """`confirm`/`reject` resolve a pair — a single id is a shape 422 (before existence)."""
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        ids = (await _ids(client))["to_confirm"]
        for decision in ("confirm", "reject"):
            resp = await client.post(
                "/reconcile/resolve",
                json={"transaction_id": ids["transaction_id"], "decision": decision, "note": "x"},
            )
            assert resp.status_code == 422
            assert "both" in resp.json()["detail"]
    assert not floor_set.reconciliations_path.exists()


async def test_resolve_422_on_both_ids_null(floor_set: ReconHarness):
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        resp = await client.post(
            "/reconcile/resolve",
            json={"decision": "acknowledge", "note": "seen it"},
        )
    assert resp.status_code == 422
    assert "at least one id" in resp.json()["detail"]
    assert not floor_set.reconciliations_path.exists()


@pytest.mark.parametrize("decision", ["reject", "acknowledge"])
async def test_resolve_422_on_blank_required_note(floor_set: ReconHarness, decision: str):
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        ids = (await _ids(client))["to_confirm"]
        resp = await client.post(
            "/reconcile/resolve",
            json={**ids, "decision": decision, "note": "   "},  # whitespace-only is blank
        )
    assert resp.status_code == 422
    assert "note" in resp.json()["detail"]
    assert not floor_set.reconciliations_path.exists()


async def test_resolve_404_on_unknown_ids_before_any_write(floor_set: ReconHarness):
    """N1 (strict 404): a supplied id absent from its store is refused before any write —
    the ledger side and the statement side each, mirroring #21's /resolve rule."""
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        ids = (await _ids(client))["to_confirm"]

        # Real txn id + bogus statement id → 404 on the statement side.
        resp = await client.post(
            "/reconcile/resolve",
            json={
                "transaction_id": ids["transaction_id"],
                "statement_line_id": "not-a-real-statement-id",
                "decision": "confirm",
            },
        )
        assert resp.status_code == 404
        assert "statement line" in resp.json()["detail"]

        # Bogus txn id + real statement id → 404 on the ledger side.
        resp = await client.post(
            "/reconcile/resolve",
            json={
                "transaction_id": "not-a-real-transaction-id",
                "statement_line_id": ids["statement_line_id"],
                "decision": "confirm",
            },
        )
        assert resp.status_code == 404
        assert "not in the ledger" in resp.json()["detail"]

    assert not floor_set.reconciliations_path.exists()  # nothing dangled against nothing


async def test_resolve_shape_guard_precedes_existence_check(floor_set: ReconHarness):
    """A `confirm` with only a (bogus) statement id is a 422 shape error, never a 404 —
    the shape guards run first and `contains()` is never called on a null id."""
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        resp = await client.post(
            "/reconcile/resolve",
            json={"statement_line_id": "does-not-exist", "decision": "confirm", "note": "x"},
        )
    assert resp.status_code == 422  # missing the transaction id — shape, not existence
    assert not floor_set.reconciliations_path.exists()


async def test_valid_resolutions_append_never_rewrite(floor_set: ReconHarness):
    """A correction is a second row: `all()` returns both, `latest_by_item` the later one."""
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        ids = (await _ids(client))["to_confirm"]
        first = await client.post("/reconcile/resolve", json={**ids, "decision": "confirm"})
        assert first.status_code == 200
        assert first.json()["source"] == "human"
        second = await client.post(
            "/reconcile/resolve",
            json={**ids, "decision": "reject", "note": "on reflection, not the same charge"},
        )
        assert second.status_code == 200

    assert len(await floor_set.reconciliation_store.all()) == 2  # both rows kept
    latest = await floor_set.reconciliation_store.latest_by_item()
    current = latest[(ids["transaction_id"], ids["statement_line_id"])]
    assert current.decision == "reject"


# --- AC6: one projection (view and ledger annotation agree) ------------------


async def test_view_and_ledger_annotation_agree(floor_set: ReconHarness):
    """A confirmed pair reads `confirmed` in both /reconcile/view and the ledger
    `reconciliation` annotation; an acknowledged gap reads `gap_acknowledged` in both."""
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        ids = await _ids(client)
        # Confirm the divergent pair (Delta) and acknowledge the amount_mismatch (Staples).
        await client.post("/reconcile/resolve", json={**ids["to_confirm"], "decision": "confirm"})
        await client.post(
            "/reconcile/resolve",
            json={**ids["amount_mismatch"], "decision": "acknowledge", "note": "vendor confirmed the $2.50"},
        )

        view = (await client.get("/reconcile/view", params={"period": PERIOD})).json()
        ledger = (await client.get("/ledger", params={"period": PERIOD})).json()

    # The view: the pair stays in to_confirm carrying "confirmed"; the gap "gap_acknowledged".
    confirmed_pair = next(p for p in view["to_confirm"] if p["transaction"]["vendor"] == "Delta Airlines")
    assert confirmed_pair["status"] == "confirmed"
    assert confirmed_pair["note"] == "" or confirmed_pair["note"] is None  # confirm needs no note
    ack_gap = next(g for g in view["gaps"] if g["kind"] == "amount_mismatch")
    assert ack_gap["status"] == "gap_acknowledged"
    assert ack_gap["note"] == "vendor confirmed the $2.50"

    # The ledger annotation folds to the same status per transaction.
    by_vendor = {e["transaction"]["vendor"]: e for e in ledger["entries"]}
    assert by_vendor["Delta Airlines"]["reconciliation"] == "confirmed"
    assert by_vendor["Staples"]["reconciliation"] == "gap_acknowledged"
    assert by_vendor["Joe's Cafe"]["reconciliation"] == "matched"
    assert by_vendor["WeWork"]["reconciliation"] == "gap_open"


async def test_view_statuses_default_open(floor_set: ReconHarness):
    """Unresolved items carry their open status and null note/decided_at."""
    await _populate_all_buckets(floor_set)
    async with _client(floor_set.app) as client:
        view = (await client.get("/reconcile/view", params={"period": PERIOD})).json()
    assert view["statement_lines"] == 4
    assert [m["status"] for m in view["matched"]] == ["matched"]
    (pair,) = view["to_confirm"]
    assert pair["status"] == "to_confirm"
    assert pair["note"] is None and pair["decided_at"] is None
    assert {g["status"] for g in view["gaps"]} == {"gap_open"}


# --- AC7: the no-statement guard ---------------------------------------------


async def test_no_statement_guard(floor_set: ReconHarness):
    """Transactions imported but no statement → the explicit no-statement view and a
    null ledger annotation on every entry — while raw POST /reconcile still returns
    the skill's truthful all-`unmatched_on_statement` report (no short-circuit)."""
    # Only transactions, no statement lines.
    for txn in (
        make_txn(vendor="Joe's Cafe", amount="12.00", date=datetime(2026, 4, 10)),
        make_txn(vendor="WeWork", amount="800.00", date=datetime(2026, 6, 10)),
    ):
        await floor_set.ledger_store.store(txn)

    async with _client(floor_set.app) as client:
        view = (await client.get("/reconcile/view", params={"period": PERIOD})).json()
        ledger = (await client.get("/ledger", params={"period": PERIOD})).json()
        raw = (await client.post("/reconcile", params={"period": PERIOD})).json()

    # The view: explicit no-statement shape.
    assert view["statement_lines"] == 0
    assert view["matched"] == [] and view["to_confirm"] == [] and view["gaps"] == []
    # The ledger fold: reconciliation null on every entry (never "everything is a gap").
    assert all(e["reconciliation"] is None for e in ledger["entries"])
    assert len(ledger["entries"]) == 2
    # But the raw skill endpoint does not short-circuit: it truthfully reports all gaps.
    assert [g["kind"] for g in raw["gaps"]] == ["unmatched_on_statement", "unmatched_on_statement"]
    assert raw["matched"] == [] and raw["to_confirm"] == []


# --- Statement import + inspection surface -----------------------------------


async def test_statement_import_and_fetch_roundtrip_and_idempotent(floor_set: ReconHarness):
    """POST /statements/import persists lines (money as strings) and is idempotent;
    GET /statements returns them in read order."""
    body = (
        b'[{"statement_ref": "S-1", "date": "2026-05-02", "amount": 45.99, "description": "SQ *THING"},'
        b' {"statement_ref": "S-2", "date": "2026-05-03", "amount": "100.00", "description": "OTHER"}]'
    )
    async with _client(floor_set.app) as client:
        resp = await client.post(
            "/statements/import",
            files={"file": ("stmt.json", body, "application/json")},
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["imported"] == 2
        # Unquoted JSON number reaches the wire as an exact string, never a lossy float.
        assert result["lines"][0]["amount"] == "45.99"
        assert result["lines"][0]["id"]  # the statement_line_key, the id to resolve against

        # Re-import is idempotent — the store adds no rows.
        await client.post("/statements/import", files={"file": ("stmt.json", body, "application/json")})
        fetched = (await client.get("/statements", params={"period": PERIOD})).json()
        assert [line["statement_ref"] for line in fetched["lines"]] == ["S-1", "S-2"]


async def test_statement_import_bad_file_is_400(floor_set: ReconHarness):
    async with _client(floor_set.app) as client:
        resp = await client.post(
            "/statements/import",
            files={"file": ("notes.txt", b"nope", "text/plain")},
        )
    assert resp.status_code == 400
    assert not floor_set.statements_path.exists()  # nothing partially imported
