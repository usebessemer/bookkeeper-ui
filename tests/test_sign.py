"""Slice 3 · D — the SIGN action: POST /sign + the durable self-contained close record.

The §5.7 human sign-off, the correctness core of Slice 3: re-verify the whole close
server-side, then append **one** durable, append-only, self-contained close record.
Drives `create_app` with injected temp-path stores (the Slice 1/2 style), exercising:

- the **period precondition** (a well-formed quarterly label with ≥1 ledger txn) and
  the **closed-period guard**, both **before** any composition (AC2 · AC8 · AC11);
- **in-handler re-verification** via the same `build_close_review` — the framework
  READY check plus the three app gates (all-confirmed / anomalies-reviewed /
  statement-or-waiver), a 409 enumerating what failed (AC3 · AC4 · AC5 · AC11);
- the **sole write** on pass — exactly one `CloseRecord`, every other store file and
  the config file byte-identical (AC6);
- **snapshot immutability** under later config drift + a confirmation correction —
  the #14 lesson made concrete (AC7);
- the **effective-prior advance** and its strictly-after ordering invariant, config
  file unmodified (AC8 · refinement #2);
- **money exactness** — every amount an exact-`Decimal` string, no `float` on any
  money path, a raw `Decimal` still rendered a string on the wire, a raw `float`
  refused (AC9 · refinement #3);
- **append-only / no double-close** (AC10) and the honest acknowledged-amount_mismatch
  snapshot rule (refinement #1).

The framework skills are called **as-is**; the app writes only through its own stores.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper.config import BookkeeperConfig

from bookkeeper_ui.anomaly_reviews import (
    AnomalyReview,
    FileAnomalyReviewStore,
    derive_flag_id,
)
from bookkeeper_ui.api import create_app
from bookkeeper_ui.closes import CloseRecord, FileCloseStore
from bookkeeper_ui.confirmations import (
    SOURCE_HUMAN,
    Confirmation,
    FileConfirmationStore,
)
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.reconciliations import (
    DECISION_ACKNOWLEDGE,
    FileReconciliationStore,
    Reconciliation,
)
from bookkeeper_ui.schemas import CloseRecordOut
from bookkeeper_ui.statement_store import FileStatementStore, statement_line_key
from bookkeeper_ui.views import build_close_record, build_close_review
from bookkeeper_ui.waivers import FileWaiverStore, Waiver
from bookkeeper.skills.flag_anomaly import flag_anomaly
from tests.conftest import make_stmt_line, make_txn

PERIOD = "2026-Q2"
AT = datetime(2026, 7, 1, tzinfo=timezone.utc)


# --- Harness + builders -----------------------------------------------------


@dataclass
class Harness:
    app: FastAPI
    config: BookkeeperConfig
    config_path: Path
    tmp: Path
    ledger_store: FileLedgerStore
    confirmation_store: FileConfirmationStore
    statement_store: FileStatementStore
    reconciliation_store: FileReconciliationStore
    close_store: FileCloseStore
    anomaly_review_store: FileAnomalyReviewStore
    waiver_store: FileWaiverStore

    def closes_path(self) -> Path:
        return self.tmp / "closes.jsonl"

    def untouched_paths(self) -> list[Path]:
        """Every file the sign path must leave byte-identical (all but `closes.jsonl`).

        The six store files it re-verifies over + the config file (AC6): a sign
        snapshots them, it never writes them.
        """
        return [
            self.tmp / name
            for name in (
                "ledger.jsonl",
                "confirmations.jsonl",
                "statements.jsonl",
                "reconciliations.jsonl",
                "anomaly_reviews.jsonl",
                "reconciliation_waivers.jsonl",
            )
        ] + [self.config_path]


def _write_config(examples_dir: Path, tmp_path: Path, **overrides: object) -> tuple[Path, BookkeeperConfig]:
    """Write the shipped example config (+ overrides) to a tmp file and load it."""
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    for key in ("tax_regime", "prior_period_state", "materiality_floor", "chart_of_accounts"):
        if key in overrides:
            data[key] = overrides[key]
    if overrides.get("drop_materiality"):
        data.pop("materiality_floor", None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, BookkeeperConfig.from_mapping(data)


def _harness(examples_dir: Path, tmp_path: Path, **overrides: object) -> Harness:
    config_path, config = _write_config(examples_dir, tmp_path, **overrides)
    return _harness_with_config(config_path, config, tmp_path)


def _harness_with_config(config_path: Path, config: BookkeeperConfig, tmp_path: Path) -> Harness:
    ledger_store = FileLedgerStore(tmp_path / "ledger.jsonl")
    confirmation_store = FileConfirmationStore(tmp_path / "confirmations.jsonl")
    statement_store = FileStatementStore(tmp_path / "statements.jsonl")
    reconciliation_store = FileReconciliationStore(tmp_path / "reconciliations.jsonl")
    close_store = FileCloseStore(tmp_path / "closes.jsonl")
    anomaly_review_store = FileAnomalyReviewStore(tmp_path / "anomaly_reviews.jsonl")
    waiver_store = FileWaiverStore(tmp_path / "reconciliation_waivers.jsonl")
    app = create_app(
        config=config,
        ledger_store=ledger_store,
        confirmation_store=confirmation_store,
        statement_store=statement_store,
        reconciliation_store=reconciliation_store,
        close_store=close_store,
        anomaly_review_store=anomaly_review_store,
        waiver_store=waiver_store,
    )
    return Harness(
        app, config, config_path, tmp_path, ledger_store, confirmation_store,
        statement_store, reconciliation_store, close_store, anomaly_review_store, waiver_store,
    )


@pytest.fixture
def harness(examples_dir, tmp_path) -> Harness:
    return _harness(examples_dir, tmp_path)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _sign(app: FastAPI, period: str = PERIOD, **body: object) -> httpx.Response:
    async with _client(app) as client:
        return await client.post("/sign", json={"period": period, **body})


async def _get_close(app: FastAPI, period: str = PERIOD) -> httpx.Response:
    async with _client(app) as client:
        return await client.get("/close", params={"period": period})


def _date_in(period: str) -> datetime:
    """A concrete mid-quarter date whose `period_of` is `period` (a `YYYY-Qn` label)."""
    year, q = period.split("-Q")
    month = (int(q) - 1) * 3 + 1  # Q1→Jan, Q2→Apr, Q3→Jul, Q4→Oct
    return datetime(int(year), month, 15, 10, 0, 0)


async def _make_signable(h: Harness, period: str = PERIOD) -> object:
    """Make `period` fully signable via the waiver path — the minimal green close.

    One AWS transaction (owner-rule proposal → 5100, under the 1000 floor so no
    anomaly, tax 6.50 → target-001) confirmed to its proposed account, and a
    reconciliation waiver (no statement). Framework READY + all three gates met.
    """
    txn = make_txn(
        vendor="AWS", amount="50.00", tax="6.50", date=_date_in(period), description="cloud"
    )
    await h.ledger_store.store(txn)
    await h.confirmation_store.record(
        Confirmation(
            transaction_id=transaction_key(txn),
            account="5100-software-subscriptions",
            source=SOURCE_HUMAN,
            decided_at=AT,
        )
    )
    await h.waiver_store.record(
        Waiver(period=period, waived_at=AT, waived_by="human", note="no feed")
    )
    return txn


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _no_money_float(node: object) -> None:
    """Assert no money field anywhere in a JSON tree is a `float` (the wire is authoritative)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("amount", "tax", "delta", "reclaimable", "period_total"):
                assert not isinstance(v, float), f"money field {k!r} is a float: {v!r}"
            _no_money_float(v)
    elif isinstance(node, list):
        for v in node:
            _no_money_float(v)


# ============================================================================
# Happy path — the durable, self-contained snapshot
# ============================================================================


async def test_sign_happy_path_writes_and_echoes_the_full_snapshot(harness: Harness):
    """A signable period signs: 200, exactly one row, and the echoed record is the
    self-contained snapshot (checklist, per-txn state incl. the agent proposal, tax,
    waived reconciliation, summary, prior states), byte-equal to the stored record."""
    txn = await _make_signable(harness)

    resp = await _sign(harness.app)
    assert resp.status_code == 200
    body = resp.json()

    assert body["period"] == PERIOD
    assert body["signed_by"] == "owner"  # default
    assert body["signed_at"].endswith("+00:00")  # ISO 8601 UTC

    # The five framework checks, verbatim + met.
    assert [c["name"] for c in body["checklist"]] == [
        "period_closeable", "period_coherent", "reconciliation_clean",
        "categorization_complete", "tax_clean",
    ]
    assert all(c["met"] for c in body["checklist"])

    # Per-transaction final state: the resolved account + human source, plus the
    # agent's original proposal (a confirmed owner-rule proposal per build_ledger).
    (row,) = body["transactions"]
    assert row["transaction_key"] == transaction_key(txn)
    assert row["vendor"] == "AWS"
    assert row["account"] == "5100-software-subscriptions"
    assert row["source"] == "human"
    assert row["status"] == "confirmed"
    assert row["amount"] == "50.00" and row["tax"] == "6.50"
    assert row["proposed"] == {"account": "5100-software-subscriptions", "source": "owner-rule"}

    # Tax + reconciliation (waived) + summary snapshots.
    assert body["tax"]["regime"] == "HST"
    assert body["tax"]["period_total"] == "6.50"
    assert body["tax"]["per_target"] == [
        {"attribution_target_id": "target-001", "reclaimable": "6.50", "transaction_count": 1}
    ]
    assert body["reconciliation"] == {"waived": True, "waived_at": AT.isoformat(), "waived_by": "human"}
    assert body["summary"]["framework"] == {"processed": 1, "auto_filed": 1, "reviewed": 0, "open": 0}
    assert body["summary"]["app_truth"] == {"confirmed": 1, "proposed": 0, "flagged": 0, "total": 1}
    assert body["anomalies"] == []
    assert body["effective_prior_period_state"] is None
    assert body["config_prior_period_state"] is None

    # Exactly one row; the stored record round-trips byte-equal to the echoed one.
    lines = harness.closes_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    stored = (await harness.close_store.all())[0]
    assert CloseRecordOut.from_record(stored).model_dump() == body


async def test_signed_by_free_text_is_recorded(harness: Harness):
    """A supplied `signed_by` is recorded verbatim; blank/whitespace falls back to owner."""
    await _make_signable(harness)
    resp = await _sign(harness.app, signed_by="Alice Owner")
    assert resp.status_code == 200
    assert resp.json()["signed_by"] == "Alice Owner"


async def test_signed_by_blank_defaults_to_owner(examples_dir, tmp_path):
    h = _harness(examples_dir, tmp_path)
    await _make_signable(h)
    resp = await _sign(h.app, signed_by="   ")
    assert resp.status_code == 200
    assert resp.json()["signed_by"] == "owner"


async def test_signed_period_renders_closed_via_get_close(harness: Harness):
    """After signing, GET /close echoes the stored snapshot as the rendered truth —
    a read of the record, not a recomputation (`framework` is null, `signable` False).

    (Wiring the existing GET /ledger / GET /reconcile screens to *show* the closed
    banner is issue E; D delivers the closed state that those surfaces read.)"""
    await _make_signable(harness)
    signed = (await _sign(harness.app)).json()

    close = (await _get_close(harness.app)).json()
    assert close["closed"] is True
    assert close["framework"] is None  # not recomputed
    assert close["signable"] is False
    assert close["close_record"] == signed  # the snapshot is the rendered truth


async def test_sign_with_clean_statement_records_statement_source(harness: Harness):
    """A period made clean by a matching statement (not a waiver) signs, and its
    reconciliation snapshot records the honest effective-report counts, not 'waived'."""
    txn = make_txn(vendor="AWS", amount="50.00", tax="6.50", date=datetime(2026, 5, 1), description="cloud")
    await harness.ledger_store.store(txn)
    await harness.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(txn), account="5100-software-subscriptions",
                     source=SOURCE_HUMAN, decided_at=AT)
    )
    await harness.statement_store.store(
        make_stmt_line(statement_ref="STMT-AWS", description="AWS", amount="50.00", date=datetime(2026, 5, 1))
    )
    resp = await _sign(harness.app)
    assert resp.status_code == 200
    recon = resp.json()["reconciliation"]
    assert recon["waived"] is False
    assert recon["source"] == "statement"
    assert recon == {"waived": False, "source": "statement", "matched": 1, "to_confirm": 0, "gaps": 0}


# ============================================================================
# AC2 — period precondition (before any composition or write)
# ============================================================================


@pytest.mark.parametrize("bad", ["garbage", "", "2026", "2026-13", "2026-Q5", "2026-Q0", "2026-05", "  "])
async def test_unparseable_period_is_400_and_writes_nothing(harness: Harness, bad: str):
    """A non-quarterly / empty label → 400, before any composition; nothing is written,
    so a malformed label can never become the effective prior."""
    resp = await _sign(harness.app, period=bad)
    assert resp.status_code == 400
    assert "quarterly" in resp.json()["detail"]
    assert not harness.closes_path().exists()
    assert await harness.close_store.latest() is None


async def test_wellformed_zero_transaction_period_is_409_and_writes_nothing(harness: Harness):
    """A well-formed quarterly label with no ledger transactions → 409 (nothing to
    close), before any close record is written."""
    resp = await _sign(harness.app, period="2026-Q2")
    assert resp.status_code == 409
    assert "no ledger transactions" in resp.json()["detail"]
    assert not harness.closes_path().exists()


async def test_precondition_runs_before_composition_on_unknown_regime(examples_dir, tmp_path):
    """The period precondition fires before `build_close_review` — a bad label on an
    app whose tax_regime would fail-fast still gets a 400 for the label, not a tax error."""
    h = _harness(examples_dir, tmp_path, tax_regime="VAT")
    await h.ledger_store.store(make_txn(vendor="AWS", amount="50.00", date=_date_in(PERIOD)))
    resp = await _sign(h.app, period="not-a-period")
    assert resp.status_code == 400
    assert "quarterly" in resp.json()["detail"]  # the label, not "Unknown tax_regime"


# ============================================================================
# AC3 — anomaly gate (blocks even a framework-READY period; floor drift orphans acks)
# ============================================================================


async def _delta_over_floor(h: Harness, amount: str = "1200.00", date: datetime | None = None) -> object:
    """A Delta charge over the example floor 1000 → owner-rule proposal AND an
    over_materiality anomaly; confirmed so gate A is not the blocker under test."""
    txn = make_txn(vendor="Delta Airlines", amount=amount, tax="0",
                   date=date or _date_in(PERIOD), description="Flight")
    await h.ledger_store.store(txn)
    await h.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(txn), account="5200-travel",
                     source=SOURCE_HUMAN, decided_at=AT)
    )
    return txn


async def test_unacknowledged_anomaly_blocks_sign_even_when_framework_ready(harness: Harness):
    """An unacknowledged over_materiality flag makes the period unsignable even though
    the framework report is READY (anomalies gate nothing in the framework); the 409
    names the gate. Acknowledging every flag makes it signable."""
    await _make_signable(harness)          # AWS confirmed + waiver
    delta = await _delta_over_floor(harness)  # confirmed, but raises an unacked anomaly

    resp = await _sign(harness.app)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["framework"]["status"] == "ready"  # framework is fine; the app gate isn't
    assert detail["signable"] is False
    assert detail["app_gates"]["anomalies_reviewed"] == {"met": False, "unacknowledged_count": 1}
    assert not harness.closes_path().exists()

    # Acknowledge the current flag (A's exact recipe) → gate B clears → signable.
    report = await flag_anomaly(harness.ledger_store, harness.config, PERIOD)
    flag = report.flags[0]
    await harness.anomaly_review_store.record(
        AnomalyReview(
            flag_id=derive_flag_id(flag), kind=flag.kind.value, reason=flag.reason,
            transaction_ids=(transaction_key(delta),), note="expected",
            acknowledged_at=AT, source=SOURCE_HUMAN,
        )
    )
    ok = await _sign(harness.app)
    assert ok.status_code == 200
    # The anomaly + its disposition are snapshotted.
    (anomaly,) = ok.json()["anomalies"]
    assert anomaly["kind"] == "over_materiality"
    assert anomaly["id"] == derive_flag_id(flag)
    assert anomaly["transaction_ids"] == [transaction_key(delta)]
    assert anomaly["acknowledged_at"] == AT.isoformat()
    assert anomaly["note"] == "expected"


async def test_gate_matches_freshly_rerun_flag_set_stale_ack_is_inert(harness: Harness):
    """Gate B matches acks against the freshly re-run flag set: an ack whose flag_id
    matches no current flag is an inert orphan and never satisfies the gate."""
    await _make_signable(harness)
    await _delta_over_floor(harness)
    # An ack for a flag that does not exist (a stale/forged id) — inert.
    await harness.anomaly_review_store.record(
        AnomalyReview(flag_id="stale-orphan-id", kind="over_materiality", reason="old",
                      transaction_ids=("x",), note="", acknowledged_at=AT, source=SOURCE_HUMAN)
    )
    resp = await _sign(harness.app)
    assert resp.status_code == 409
    assert resp.json()["detail"]["app_gates"]["anomalies_reviewed"]["unacknowledged_count"] == 1


async def test_floor_change_orphans_stale_ack_and_reblocks_sign(examples_dir, tmp_path):
    """Changing `materiality_floor` re-derives the over_materiality flag id (only that
    reason embeds the floor), orphaning the prior ack: a floor-1000 ack no longer
    satisfies the gate at floor 1100, so the period re-blocks until re-acked. A lower
    item that fell under the raised floor is no longer flagged at all."""
    h = _harness(examples_dir, tmp_path, materiality_floor="1000.00")
    await _make_signable(h)  # AWS 50, under either floor
    big = await _delta_over_floor(h, amount="1200.00")
    mid = make_txn(vendor="AWS", amount="1050.00", tax="0", date=datetime(2026, 6, 2), description="big cloud")
    await h.ledger_store.store(mid)
    await h.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(mid), account="5100-software-subscriptions",
                     source=SOURCE_HUMAN, decided_at=AT)
    )

    # At floor 1000 both Big (1200) and Mid (1050) are over-material → two flags.
    flags_1000 = (await flag_anomaly(h.ledger_store, h.config, PERIOD)).flags
    over_1000 = [f for f in flags_1000 if f.kind.value == "over_materiality"]
    assert len(over_1000) == 2
    for flag in over_1000:  # ack both → signable at floor 1000
        await h.anomaly_review_store.record(
            AnomalyReview(flag_id=derive_flag_id(flag), kind=flag.kind.value, reason=flag.reason,
                          transaction_ids=tuple(transaction_key(t) for t in flag.transactions),
                          note="ok", acknowledged_at=AT, source=SOURCE_HUMAN)
        )
    # Signable at floor 1000 — asserted via the read path so we do NOT write a close
    # (signing here would close the period and the raised-floor rebuild below would
    # hit the closed-guard instead of the anomaly gate under test).
    assert (await _get_close(h.app)).json()["signable"] is True

    # Fresh harness at floor 1100 (raised), same stores: Mid (1050) is no longer
    # flagged; Big (1200) is still flagged but its reason (and id) now embeds 1100,
    # so the floor-1000 ack is an orphan.
    raised_config = dataclasses.replace(h.config, materiality_floor=Decimal("1100.00"))
    h2 = _harness_with_config(h.config_path, raised_config, tmp_path)
    flags_1100 = (await flag_anomaly(h2.ledger_store, h2.config, PERIOD)).flags
    over_1100 = [f for f in flags_1100 if f.kind.value == "over_materiality"]
    assert [transaction_key(f.transactions[0]) for f in over_1100] == [transaction_key(big)]  # Mid dropped
    new_id = derive_flag_id(over_1100[0])
    assert new_id not in {derive_flag_id(f) for f in over_1000}  # a new id — stale ack orphaned

    resp = await _sign(h2.app)
    assert resp.status_code == 409
    assert resp.json()["detail"]["app_gates"]["anomalies_reviewed"]["unacknowledged_count"] == 1

    # Re-ack against the new (floor-1100) id → signable again.
    await h2.anomaly_review_store.record(
        AnomalyReview(flag_id=new_id, kind="over_materiality", reason=over_1100[0].reason,
                      transaction_ids=(transaction_key(big),), note="re-reviewed",
                      acknowledged_at=AT, source=SOURCE_HUMAN)
    )
    assert (await _sign(h2.app)).status_code == 200


# ============================================================================
# AC4 — all-confirmed gate (a READY period with an unconfirmed proposal isn't signable)
# ============================================================================


async def test_unconfirmed_proposal_not_signable_409_names_pending_count(harness: Harness):
    """An unconfirmed owner-rule proposal keeps the framework READY (proposals do not
    block) but fails gate A; the 409 names the pending count and writes nothing."""
    await harness.ledger_store.store(  # AWS proposal, never confirmed
        make_txn(vendor="AWS", amount="50.00", tax="6.50", date=_date_in(PERIOD), description="cloud")
    )
    await harness.waiver_store.record(Waiver(period=PERIOD, waived_at=AT, waived_by="human", note=""))

    resp = await _sign(harness.app)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["framework"]["status"] == "ready"
    assert detail["app_gates"]["all_confirmed"] == {"met": False, "pending_count": 1}
    assert not harness.closes_path().exists()


# ============================================================================
# AC5 — waiver (blocks with no statement + no waiver; a statement is not waivable)
# ============================================================================


async def test_no_statement_no_waiver_blocks_sign(harness: Harness):
    """Zero statement lines and no waiver → gate C fails (source 'missing'), so the
    period is not signable even with everything else clean."""
    txn = make_txn(vendor="AWS", amount="50.00", tax="6.50", date=_date_in(PERIOD), description="cloud")
    await harness.ledger_store.store(txn)
    await harness.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(txn), account="5100-software-subscriptions",
                     source=SOURCE_HUMAN, decided_at=AT)
    )
    resp = await _sign(harness.app)
    assert resp.status_code == 409
    assert resp.json()["detail"]["app_gates"]["statement_or_waiver"] == {"met": False, "source": "missing"}
    assert not harness.closes_path().exists()


async def test_statement_present_is_not_waivable(harness: Harness):
    """A period with a statement on file is never waivable (issue C's 409) — reconcile
    it, do not waive it. (The sign gate C is then satisfied by the statement itself.)"""
    await harness.statement_store.store(
        make_stmt_line(statement_ref="STMT-1", description="AWS", amount="50.00", date=datetime(2026, 5, 1))
    )
    async with _client(harness.app) as client:
        resp = await client.post("/reconciliation/waive", json={"period": PERIOD})
    assert resp.status_code == 409
    assert "statement on file" in resp.json()["detail"]


# ============================================================================
# AC6 — sign writes exactly one record; all six other stores + config byte-identical
# ============================================================================


async def test_sign_writes_exactly_one_row_everything_else_byte_identical(harness: Harness):
    """A passing sign appends exactly one line to closes.jsonl; the six other store
    files and the config file are byte-identical before/after (the in-memory
    dataclasses.replace is not a file write)."""
    await _make_signable(harness)
    before = {p: _sha(p) for p in harness.untouched_paths()}
    assert not harness.closes_path().exists()

    resp = await _sign(harness.app)
    assert resp.status_code == 200

    after = {p: _sha(p) for p in harness.untouched_paths()}
    assert after == before, "the sign path touched a file it must only read"
    assert len(harness.closes_path().read_text(encoding="utf-8").splitlines()) == 1


# ============================================================================
# AC7 — snapshot immutability under config drift + a confirmation correction (#14)
# ============================================================================


async def test_snapshot_immutable_under_config_drift_and_correction(examples_dir, tmp_path):
    """Sign a period, then (a) drift the config (rename a chart account, change the
    floor + prior_period_state) and (b) append a confirmation correction for a signed
    transaction: the stored close record stays byte-identical and the closed-period
    render still shows the sign-time account, never the corrected one."""
    h = _harness(examples_dir, tmp_path)
    txn = await _make_signable(h)  # AWS confirmed → 5100-software-subscriptions
    assert (await _sign(h.app)).status_code == 200
    closes_bytes = h.closes_path().read_bytes()

    # (b) A later confirmation *correction* for the signed transaction.
    await h.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(txn), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    )
    # (a) Config drift: rename a chart account, change the floor + prior state.
    drifted = dataclasses.replace(
        h.config,
        chart_of_accounts=tuple(
            "5100-cloud-costs" if a == "5100-software-subscriptions" else a
            for a in h.config.chart_of_accounts
        ),
        materiality_floor=Decimal("5.00"),
        prior_period_state="2020-Q1",
    )
    h2 = _harness_with_config(h.config_path, drifted, tmp_path)

    # The stored record bytes are untouched by either mutation.
    assert h.closes_path().read_bytes() == closes_bytes

    # The closed-period render is the sign-time snapshot — the original account.
    close = (await _get_close(h2.app)).json()
    assert close["closed"] is True
    (row,) = close["close_record"]["transactions"]
    assert row["account"] == "5100-software-subscriptions"  # sign-time, NOT the correction
    assert row["source"] == "human"


# ============================================================================
# AC8 — prior-period advance + the strictly-after ordering invariant (refinement #2)
# ============================================================================


async def test_prior_advance_orders_closes_and_leaves_config_unwritten(examples_dir, tmp_path):
    """After signing 2026-Q2: a 2026-Q1 sign is refused by the framework's own
    period_closeable (config file unmodified), 2026-Q3 is signable, and re-signing
    2026-Q2 is refused by the closed-guard. `close_store.latest()` stays the
    strictly-after max, so an out-of-order append would trip this test."""
    h = _harness(examples_dir, tmp_path)  # prior_period_state unset
    config_before = h.config_path.read_bytes()

    await _make_signable(h, "2026-Q2")
    assert (await _sign(h.app, "2026-Q2")).status_code == 200

    # 2026-Q1 — at/before the effective prior 2026-Q2 → framework period_closeable unmet.
    await _make_signable(h, "2026-Q1")
    q1 = await _sign(h.app, "2026-Q1")
    assert q1.status_code == 409
    detail = q1.json()["detail"]
    assert detail["framework"]["status"] == "blocked"
    pc = {c["name"]: c for c in detail["framework"]["checklist"]}["period_closeable"]
    assert pc["met"] is False
    assert "at or before the last closed period" in pc["reason"]  # the framework's own reason
    assert detail["effective_prior_period_state"] == "2026-Q2"

    # 2026-Q3 — after 2026-Q2 → signable.
    await _make_signable(h, "2026-Q3")
    assert (await _sign(h.app, "2026-Q3")).status_code == 200

    # Re-signing 2026-Q2 → closed-guard 409 (never a second row).
    resign = await _sign(h.app, "2026-Q2")
    assert resign.status_code == 409
    assert "already closed" in resign.json()["detail"]

    # The ordering invariant: latest == the strictly-after max; Q1 never appended.
    assert (await h.close_store.latest()).period == "2026-Q3"
    assert set(await h.close_store.by_period()) == {"2026-Q2", "2026-Q3"}

    # The config file was never written by any sign or refusal (D4 is in-memory).
    assert h.config_path.read_bytes() == config_before


# ============================================================================
# AC9 — money: every amount an exact-Decimal string, no float on any path
# ============================================================================


async def test_all_money_in_close_record_is_string_never_float(harness: Harness):
    """Every money figure in the returned record and in closes.jsonl is an exact
    string — tax totals, per-target reclaimable, and transaction amounts/tax — never
    a JSON number."""
    await _make_signable(harness)
    body = (await _sign(harness.app)).json()

    assert isinstance(body["tax"]["period_total"], str)
    for t in body["tax"]["per_target"]:
        assert isinstance(t["reclaimable"], str)
    for row in body["transactions"]:
        assert isinstance(row["amount"], str) and isinstance(row["tax"], str)
    _no_money_float(body)

    # The on-disk row is authoritative too.
    disk = json.loads(harness.closes_path().read_text(encoding="utf-8").splitlines()[0])
    assert disk["tax"]["period_total"] == "6.50"
    _no_money_float(disk)


# ============================================================================
# AC10 — append-only + no double-close
# ============================================================================


async def test_no_double_close_second_sign_is_409_and_no_second_row(harness: Harness):
    """Signing an already-closed period → 409 (closed-guard); the trail keeps exactly
    one row and the earlier bytes are untouched (append-only)."""
    await _make_signable(harness)
    assert (await _sign(harness.app)).status_code == 200
    after_first = harness.closes_path().read_bytes()

    second = await _sign(harness.app)
    assert second.status_code == 409
    assert "already closed" in second.json()["detail"]
    assert harness.closes_path().read_bytes() == after_first  # unchanged, one row
    assert len(after_first.splitlines()) == 1


# ============================================================================
# AC11 — in-handler re-verification (a stale client cannot sign a failing period)
# ============================================================================


async def test_reverification_reblocks_when_state_regresses_after_looking_signable(harness: Harness):
    """The sign handler recomputes the whole close at POST time: a period that was
    signable becomes unsignable once an unconfirmed transaction is added, and the sign
    is refused — no trust in any prior 'signable' state."""
    await _make_signable(harness)
    assert (await _get_close(harness.app)).json()["signable"] is True

    # A new unconfirmed proposal regresses gate A (pending > 0).
    await harness.ledger_store.store(
        make_txn(vendor="AWS", amount="9.00", tax="0", date=datetime(2026, 4, 4), description="extra")
    )
    resp = await _sign(harness.app)
    assert resp.status_code == 409
    assert resp.json()["detail"]["app_gates"]["all_confirmed"]["met"] is False
    assert not harness.closes_path().exists()


async def test_closed_guard_precedes_gate_reverification(harness: Harness):
    """The closed-guard runs before the composition is trusted: once closed, a sign is
    a 409 'already closed' (not a gate evaluation), even though the closed review
    returns the stored snapshot rather than a fresh gate eval."""
    await _make_signable(harness)
    assert (await _sign(harness.app)).status_code == 200
    resp = await _sign(harness.app)
    assert resp.status_code == 409
    assert "already closed" in resp.json()["detail"]  # the closed-guard, not a gate body


async def test_sign_requires_wired_close_store(examples_dir, tmp_path):
    """A Slice-1/2 app (no close store wired) refuses the sign with a 503 rather than
    silently no-opping a write."""
    _config_path, config = _write_config(examples_dir, tmp_path)
    ledger_store = FileLedgerStore(tmp_path / "ledger.jsonl")
    app = create_app(
        config=config,
        ledger_store=ledger_store,
        confirmation_store=FileConfirmationStore(tmp_path / "confirmations.jsonl"),
        statement_store=FileStatementStore(tmp_path / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(tmp_path / "reconciliations.jsonl"),
    )
    resp = await _sign(app)
    assert resp.status_code == 503


# ============================================================================
# Refinement #1 — an acknowledged amount_mismatch is never signed over
# ============================================================================


async def test_acknowledged_amount_mismatch_blocks_sign(harness: Harness):
    """An AMOUNT_MISMATCH is a live money disagreement: an acknowledge does not clear
    it (only the two one-sided gap kinds clear on acknowledge). The framework blocks
    on it, so a period carrying an acknowledged amount_mismatch is never signable —
    the sign re-runs build_close_review and inherits reconciliation_clean = unmet."""
    # A ledger txn + a statement line at a different amount, same date+vendor → an
    # amount_mismatch gap (delta ≠ 0).
    staples = make_txn(vendor="Staples", amount="80.00", tax="0", date=datetime(2026, 4, 3), description="Paper")
    await harness.ledger_store.store(staples)
    await harness.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(staples), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=AT)
    )
    stmt = make_stmt_line(statement_ref="STMT-STAP", description="STAPLES STORE 123", amount="82.50", date=datetime(2026, 4, 3))
    await harness.statement_store.store(stmt)
    # Acknowledge the amount_mismatch — it must NOT clear for close.
    await harness.reconciliation_store.record(
        Reconciliation(transaction_key(staples), statement_line_key(stmt), DECISION_ACKNOWLEDGE,
                       "seen", SOURCE_HUMAN, AT)
    )
    resp = await _sign(harness.app)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    rc = {c["name"]: c for c in detail["framework"]["checklist"]}["reconciliation_clean"]
    assert rc["met"] is False  # still blocks
    assert not harness.closes_path().exists()


# ============================================================================
# Refinement #3 — in-memory money-string discipline (raw Decimal → string; float refused)
# ============================================================================


def _raw_record(**payload: object) -> CloseRecord:
    return CloseRecord(
        period=PERIOD, signed_at=AT, signed_by="human",
        checklist=[], transactions=[], tax=payload.get("tax", {}),
        reconciliation=payload.get("reconciliation", {}), anomalies=[],
        effective_prior_period_state=None, config_prior_period_state=None,
    )


def test_raw_decimal_in_payload_renders_as_exact_string_on_the_wire():
    """A CloseRecord whose tax/reconciliation payloads carry a raw `Decimal` still
    serializes to the exact string on the wire (CloseRecordOut is a pydantic model,
    so it never coerces a Decimal to a lossy float the way jsonable_encoder would)."""
    record = _raw_record(
        tax={"period_total": Decimal("6.50")},
        reconciliation={"delta": Decimal("-2.50")},
    )
    wire = json.loads(CloseRecordOut.from_record(record).model_dump_json())
    assert wire["tax"]["period_total"] == "6.50"
    assert wire["reconciliation"]["delta"] == "-2.50"
    assert isinstance(wire["tax"]["period_total"], str)
    _no_money_float(wire)


async def test_raw_float_money_is_refused_on_the_close_store_write():
    """A raw `float` on any money path is refused at the store boundary — a lossy
    float can never land in closes.jsonl (the sign path builds pre-stringified, so
    this guard is the backstop)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        store = FileCloseStore(Path(d) / "closes.jsonl")
        with pytest.raises(TypeError):
            await store.record(_raw_record(tax={"period_total": 6.50}))


# ============================================================================
# Refinement #3 (belt) + #2 (D4 ordering) — regression pins added at lead review
# ============================================================================


async def test_close_record_out_stringifies_a_raw_decimal_on_the_wire():
    """Refinement #3 belt: even if a raw `Decimal` slipped past build_close_record's
    stringification into a snapshot payload, `CloseRecordOut` (the /sign response_model)
    renders it as an exact STRING — never FastAPI's lossy float. Guards against a future
    refactor to a raw-dict response silently reintroducing the money-as-float bug."""
    from bookkeeper_ui.schemas import CloseRecordOut

    record = CloseRecord(
        period=PERIOD, signed_at=AT, signed_by="owner",
        checklist=[], transactions=[{"amount": Decimal("100.00")}],
        tax={"period_total": Decimal("82.50"), "per_target": [{"reclaimable": Decimal("5.00")}]},
        reconciliation={}, anomalies=[],
        effective_prior_period_state=None, config_prior_period_state=None,
    )
    wire = CloseRecordOut.from_record(record).model_dump(mode="json")
    assert wire["tax"]["period_total"] == "82.50"
    assert isinstance(wire["tax"]["period_total"], str)
    assert isinstance(wire["tax"]["per_target"][0]["reclaimable"], str)
    assert isinstance(wire["transactions"][0]["amount"], str)
    _no_money_float(wire)


async def test_build_close_record_stringifies_tax_money_at_construction(harness: Harness):
    """D3 spec pin (refinement #3, undefended): `build_close_record` stringifies tax
    money **at construction** — not only via the downstream `CloseRecordOut` / store
    `_jsonable` nets. On the freshly-built record the tax `period_total` + per-target
    `reclaimable` are already exact `str`s, equal to the store read-back.

    Bites if the at-construction `str()` on `views.build_close_record` (L849/854) is
    dropped: a raw `Decimal` is neither a `str` nor `== '6.10'`, and no longer equals
    the (re-stringified) round-trip — the build-equals-read-back invariant the spec
    mandates. The full suite otherwise misses this: wire + disk stay correct because
    `CloseRecordOut` and `_jsonable` re-stringify downstream. 6.10 also float-corrupts
    (`str(float(Decimal('6.10')))` == '6.1'), so a `str(float())` variant trips too."""
    # A signable AWS close (tax 6.10 → target-001) — under the 1000 floor, confirmed,
    # waived: framework READY + all three gates, so build_close_record is reachable.
    txn = make_txn(vendor="AWS", amount="50.00", tax="6.10", date=_date_in(PERIOD), description="cloud")
    await harness.ledger_store.store(txn)
    await harness.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(txn), account="5100-software-subscriptions",
                     source=SOURCE_HUMAN, decided_at=AT)
    )
    await harness.waiver_store.record(Waiver(period=PERIOD, waived_at=AT, waived_by="human", note="no feed"))

    review = await build_close_review(
        config=harness.config,
        ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store,
        statement_store=harness.statement_store,
        reconciliation_store=harness.reconciliation_store,
        close_store=harness.close_store,
        anomaly_review_store=harness.anomaly_review_store,
        waiver_store=harness.waiver_store,
        period=PERIOD,
    )
    assert review.signable is True

    record = await build_close_record(
        review=review, waiver_store=harness.waiver_store, signed_by="owner", signed_at=AT,
    )

    # AT CONSTRUCTION: money is already an exact string — no downstream net involved.
    assert record.tax["period_total"] == "6.10"
    assert isinstance(record.tax["period_total"], str)
    (target,) = record.tax["per_target"]
    assert target["reclaimable"] == "6.10"
    assert isinstance(target["reclaimable"], str)

    # Build-equals-read-back: the in-memory record's money equals the store round-trip.
    # (`_jsonable` would re-stringify a stray Decimal — so this must already hold BEFORE
    # that net, which only the at-construction str() guarantees.)
    await harness.close_store.record(record)
    (stored,) = await harness.close_store.all()
    assert stored.tax["period_total"] == record.tax["period_total"]
    assert stored.tax["per_target"][0]["reclaimable"] == record.tax["per_target"][0]["reclaimable"]


async def test_effective_prior_is_latest_not_first_close_on_a_forward_jump(examples_dir, tmp_path):
    """Refinement #2 (D4 ordering tripwire): the effective prior must be the LATEST
    (strictly-after max) signed close, not the first. Sign 2026-Q2 then 2026-Q4 (skipping
    Q3); a Q3 close is then blocked by the framework's period_closeable against the latest
    close Q4 — whereas if the effective-prior read the FIRST close (Q2), Q3 (> Q2) would
    wrongly read signable. A monotone-consecutive walk (the AC8 test) cannot distinguish
    latest from first; this forward jump does."""
    h = _harness(examples_dir, tmp_path)
    await _make_signable(h, "2026-Q2")
    assert (await _sign(h.app, "2026-Q2")).status_code == 200
    await _make_signable(h, "2026-Q4")
    assert (await _sign(h.app, "2026-Q4")).status_code == 200
    assert (await h.close_store.latest()).period == "2026-Q4"

    await _make_signable(h, "2026-Q3")
    q3 = await _sign(h.app, "2026-Q3")
    assert q3.status_code == 409  # Q3 is at/before the latest close Q4 → blocked
    detail = q3.json()["detail"]
    pc = {c["name"]: c for c in detail["framework"]["checklist"]}["period_closeable"]
    assert pc["met"] is False
    assert detail["effective_prior_period_state"] == "2026-Q4"  # latest, not first (Q2)
    assert "2026-Q3" not in await h.close_store.by_period()  # nothing appended
