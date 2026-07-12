"""Slice 3 · B — the composition core: effective reports + build_close_review + GET /close.

Drives `create_app` with injected temp-path stores (the Slice 1/2 style) and
unit-tests the two effective-report constructors directly, exercising:

- the effective `CategorizationReport` (raw `categorize` + confirmations) and
  effective `ReconciliationReport` (raw `reconcile_account` + resolutions, incl.
  the no-statement waiver path) as **real framework dataclasses**;
- `views.build_close_review` — the one shared close projection: the framework's
  five-check `close_period` checklist over the effective reports (verbatim
  reasons), the tax + anomaly overlays, the D4 effective-prior substitution, the
  three app gates, and `signable`;
- `GET /close` — the read-only serialization of that projection.

The framework skills (`close_period` / `track_tax` / `flag_anomaly` /
`reconcile_account` / `categorize`) are called **as-is**; the app constructs only
framework-public dataclasses for the effective inputs and writes nothing here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.close_period import close_period
from bookkeeper.skills.flag_anomaly import flag_anomaly
from bookkeeper.skills.reconcile import GapKind
from bookkeeper.skills.track_tax import track_tax

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
    DECISION_CONFIRM,
    DECISION_REJECT,
    FileReconciliationStore,
    Reconciliation,
)
from bookkeeper_ui.statement_store import FileStatementStore, statement_line_key
from bookkeeper_ui.views import (
    build_close_review,
    build_effective_categorization,
    build_effective_reconciliation,
    build_ledger,
)
from bookkeeper_ui.waivers import FileWaiverStore, Waiver
from tests.conftest import make_stmt_line, make_txn

PERIOD = "2026-Q2"


# --- Harness + config builders ----------------------------------------------


@dataclass
class Harness:
    app: FastAPI
    config: BookkeeperConfig
    config_path: Path
    ledger_store: FileLedgerStore
    confirmation_store: FileConfirmationStore
    statement_store: FileStatementStore
    reconciliation_store: FileReconciliationStore
    close_store: FileCloseStore
    anomaly_review_store: FileAnomalyReviewStore
    waiver_store: FileWaiverStore


def _write_config(examples_dir: Path, tmp_path: Path, **overrides: object) -> tuple[Path, BookkeeperConfig]:
    """Write a config (the shipped example + overrides) to a tmp file, load it.

    Written to tmp so a test can assert the file is **never** written by the read
    path (D4: the effective-prior substitution is in-memory only). Supported
    overrides: `tax_regime`, `materiality_floor`, `prior_period_state`, and
    `drop_reconcile_vendor` / `drop_materiality` (to make a boundary inert).
    """
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    if "tax_regime" in overrides:
        data["tax_regime"] = overrides["tax_regime"]
    if "prior_period_state" in overrides:
        data["prior_period_state"] = overrides["prior_period_state"]
    if "materiality_floor" in overrides:
        data["materiality_floor"] = overrides["materiality_floor"]
    if overrides.get("drop_materiality"):
        data.pop("materiality_floor", None)
    if overrides.get("drop_reconcile_vendor"):
        data["confidence_thresholds"] = {
            k: v for k, v in (data.get("confidence_thresholds") or {}).items()
            if k != "reconcile_vendor"
        }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, BookkeeperConfig.from_mapping(data)


def _harness(examples_dir: Path, tmp_path: Path, **overrides: object) -> Harness:
    config_path, config = _write_config(examples_dir, tmp_path, **overrides)
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
        app, config, config_path, ledger_store, confirmation_store, statement_store,
        reconciliation_store, close_store, anomaly_review_store, waiver_store,
    )


@pytest.fixture
def harness(examples_dir, tmp_path) -> Harness:
    return _harness(examples_dir, tmp_path)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _get_close(app: FastAPI, period: str = PERIOD) -> httpx.Response:
    async with _client(app) as client:
        return await client.get("/close", params={"period": period})


# Grounded ledger vendors (probed against the shipped example config):
#   "AWS"            → owner-rule proposal (5100), tax 6.50 → target-001; never flagged.
#   "Zzxq Gibberish" → below the categorize floor → FLAGGED (blocks until confirmed).
#   "Delta Airlines" 1200 → owner-rule proposal (5200) AND an over_materiality anomaly.
def _aws() -> object:
    return make_txn(vendor="AWS", amount="50.00", tax="6.50", date=datetime(2026, 5, 1), description="cloud")


def _flagged() -> object:
    return make_txn(vendor="Zzxq Gibberish", amount="40.00", tax="5.20", date=datetime(2026, 5, 2), description="")


async def _populate_reconcile(h: Harness) -> None:
    """The all-buckets reconcile fixture (grounded, floor 0.7 from the example config).

    matched: Joe's Cafe/STMT-001 · to_confirm: Delta/STMT-002 · gaps: Staples
    amount_mismatch/STMT-003, STMT-004 unmatched_in_ledger, WeWork unmatched_on_statement.
    """
    for txn in (
        make_txn(vendor="Joe's Cafe", amount="12.00", tax="0", date=datetime(2026, 4, 10), description="Coffee"),
        make_txn(vendor="Delta Airlines", amount="500.00", tax="0", date=datetime(2026, 5, 2), description="Flight"),
        make_txn(vendor="Staples", amount="80.00", tax="0", date=datetime(2026, 4, 3), description="Paper"),
        make_txn(vendor="WeWork", amount="800.00", tax="0", date=datetime(2026, 6, 10), description="Rent"),
    ):
        await h.ledger_store.store(txn)
    for line in (
        make_stmt_line(statement_ref="STMT-001", description="SQ *JOE'S CAFE 415", amount="12.00", date=datetime(2026, 4, 11)),
        make_stmt_line(statement_ref="STMT-002", description="AMZN MKTP US*2Z3", amount="500.00", date=datetime(2026, 5, 3)),
        make_stmt_line(statement_ref="STMT-003", description="STAPLES STORE 123", amount="82.50", date=datetime(2026, 4, 3)),
        make_stmt_line(statement_ref="STMT-004", description="MYSTERY CHARGE", amount="45.00", date=datetime(2026, 5, 20)),
    ):
        await h.statement_store.store(line)


# ============================================================================
# AC2 — checklist fidelity: the five framework checks, verbatim reasons
# ============================================================================


async def test_get_close_renders_all_five_checks_in_order_verbatim(harness: Harness):
    """GET /close renders exactly the five framework checks, in order, with the
    framework's own reason strings — diffed against a direct `close_period` call."""
    await harness.ledger_store.store(_flagged())
    await harness.waiver_store.record(  # waive reconciliation so it is not the blocker under test
        Waiver(period=PERIOD, waived_at=datetime(2026, 7, 1, tzinfo=timezone.utc), waived_by="human", note="no feed")
    )

    resp = await _get_close(harness.app)
    assert resp.status_code == 200
    body = resp.json()

    # The names + order are the fixed five.
    assert [c["name"] for c in body["framework"]["checklist"]] == [
        "period_closeable",
        "period_coherent",
        "reconciliation_clean",
        "categorization_complete",
        "tax_clean",
    ]

    # Verbatim against a direct close_period over the same effective reports.
    eff_cat = await build_effective_categorization(
        config=harness.config, ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store, period=PERIOD,
    )
    eff_recon, _src = await build_effective_reconciliation(
        config=harness.config, ledger_store=harness.ledger_store,
        statement_store=harness.statement_store, reconciliation_store=harness.reconciliation_store,
        waiver_store=harness.waiver_store, period=PERIOD,
    )
    tax = await track_tax(harness.ledger_store, harness.config, PERIOD)
    report = close_period(eff_recon, tax, eff_cat, harness.config, PERIOD)

    by_name = {c["name"]: c for c in body["framework"]["checklist"]}
    for check in report.checklist:
        assert by_name[check.name]["met"] == check.met
        assert by_name[check.name]["reason"] == check.reason  # verbatim, not re-worded

    # The flagged txn blocks: status blocked, categorization_complete unmet, and a
    # blocker carrying the underlying category_flag item verbatim.
    assert body["framework"]["status"] == "blocked"
    assert by_name["categorization_complete"]["met"] is False
    cat_blockers = [b for b in body["framework"]["blockers"] if b["check"] == "categorization_complete"]
    assert len(cat_blockers) == 1
    assert cat_blockers[0]["item"]["type"] == "category_flag"
    assert cat_blockers[0]["item"]["transaction"]["vendor"] == "Zzxq Gibberish"
    model_cat_blocker = [b for b in report.blockers if b.check == "categorization_complete"][0]
    assert cat_blockers[0]["reason"] == model_cat_blocker.reason  # verbatim framework reason


async def test_blocked_never_renders_ready(harness: Harness):
    """A period with an unresolved flag never renders READY on any field."""
    await harness.ledger_store.store(_flagged())
    body = (await _get_close(harness.app)).json()
    assert body["framework"]["status"] == "blocked"
    assert body["signable"] is False
    assert body["summary"] is None  # no proposed close when BLOCKED


# ============================================================================
# AC3 — effective-reports honesty
# ============================================================================


async def test_effective_categorization_moves_confirmed_flag_to_human_proposal(harness: Harness):
    """A flagged txn with a confirmation moves out of `flagged` into `proposals` as a
    human proposal (account=confirmed, source='human'); one without stays flagged."""
    flagged = _flagged()
    await harness.ledger_store.store(flagged)
    await harness.ledger_store.store(_aws())  # a raw proposal — stays a proposal

    before = await build_effective_categorization(
        config=harness.config, ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store, period=PERIOD,
    )
    assert [f.transaction.vendor for f in before.flagged] == ["Zzxq Gibberish"]
    assert before.period == PERIOD  # stamps the closing period (period_coherent)

    await harness.confirmation_store.record(
        Confirmation(
            transaction_id=transaction_key(flagged),
            account="5000-office-supplies",
            source=SOURCE_HUMAN,
            decided_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
    )

    after = await build_effective_categorization(
        config=harness.config, ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store, period=PERIOD,
    )
    assert after.flagged == ()  # no longer blocks
    human = [p for p in after.proposals if p.transaction.vendor == "Zzxq Gibberish"]
    assert len(human) == 1
    assert human[0].proposed_account == "5000-office-supplies"
    assert human[0].source == "human"  # the app's free-text convention, not a framework const
    assert human[0].confidence == 1.0
    # The raw AWS proposal is untouched (agent trail preserved).
    assert any(p.transaction.vendor == "AWS" and p.source == "owner-rule" for p in after.proposals)


async def test_get_close_unblocks_after_confirming_flagged(harness: Harness):
    """The flag blocks categorization_complete until confirmed; then it clears, and
    the ledger renders the txn confirmed/human (status renders from build_ledger)."""
    flagged = _flagged()
    await harness.ledger_store.store(flagged)
    await harness.waiver_store.record(
        Waiver(period=PERIOD, waived_at=datetime(2026, 7, 1, tzinfo=timezone.utc), waived_by="human", note="")
    )

    before = (await _get_close(harness.app)).json()
    cc = {c["name"]: c for c in before["framework"]["checklist"]}["categorization_complete"]
    assert cc["met"] is False

    await harness.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(flagged), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    )
    after = (await _get_close(harness.app)).json()
    cc = {c["name"]: c for c in after["framework"]["checklist"]}["categorization_complete"]
    assert cc["met"] is True

    ledger = await build_ledger(
        config=harness.config, ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store, period=PERIOD,
    )
    row = [e for e in ledger.entries if e.transaction.vendor == "Zzxq Gibberish"][0]
    assert row.status == "confirmed"
    assert row.source == "human"
    assert row.confidence is None  # never rendered as an agent claim


async def test_effective_reconciliation_confirm_reject_acknowledge(harness: Harness):
    """A confirmed to_confirm pair counts as matched; a rejected one decomposes into
    two blocking gaps; an acknowledged gap is dropped; unresolved items stay."""
    await _populate_reconcile(harness)

    async def effective():
        return await build_effective_reconciliation(
            config=harness.config, ledger_store=harness.ledger_store,
            statement_store=harness.statement_store, reconciliation_store=harness.reconciliation_store,
            waiver_store=harness.waiver_store, period=PERIOD,
        )

    # Baseline: raw report shape, source="statement".
    report, source = await effective()
    assert source == "statement"
    assert {m.statement_line.statement_ref for m in report.matched} == {"STMT-001"}
    assert {p.pair.statement_line.statement_ref for p in report.to_confirm} == {"STMT-002"}
    assert len(report.gaps) == 3  # Staples mismatch, STMT-004 in_ledger, WeWork on_statement

    # Identify the Delta/STMT-002 pair and the STMT-004 gap keys.
    delta_txn = [t for t in await harness.ledger_store.fetch_for_period(PERIOD) if t.vendor == "Delta Airlines"][0]
    stmt002 = [s for s in await harness.statement_store.fetch_statement(PERIOD) if s.statement_ref == "STMT-002"][0]
    stmt004 = [s for s in await harness.statement_store.fetch_statement(PERIOD) if s.statement_ref == "STMT-004"][0]

    # Confirm the pair → matched, to_confirm empty.
    await harness.reconciliation_store.record(
        Reconciliation(transaction_key(delta_txn), statement_line_key(stmt002), DECISION_CONFIRM, "", SOURCE_HUMAN, datetime(2026, 7, 1, tzinfo=timezone.utc))
    )
    report, _ = await effective()
    assert {m.statement_line.statement_ref for m in report.matched} == {"STMT-001", "STMT-002"}
    assert report.to_confirm == ()
    assert len(report.gaps) == 3  # unchanged

    # Correct to a reject → two one-sided gaps, both real (blocking).
    await harness.reconciliation_store.record(
        Reconciliation(transaction_key(delta_txn), statement_line_key(stmt002), DECISION_REJECT, "not us", SOURCE_HUMAN, datetime(2026, 7, 2, tzinfo=timezone.utc))
    )
    report, _ = await effective()
    assert {m.statement_line.statement_ref for m in report.matched} == {"STMT-001"}
    assert report.to_confirm == ()
    kinds = [g.kind for g in report.gaps]
    assert kinds.count(GapKind.UNMATCHED_ON_STATEMENT) == 2  # WeWork + rejected Delta txn
    assert kinds.count(GapKind.UNMATCHED_IN_LEDGER) == 2  # STMT-004 + rejected STMT-002
    assert len(report.gaps) == 5

    # Acknowledge the STMT-004 one-sided gap → dropped from gaps.
    await harness.reconciliation_store.record(
        Reconciliation(None, statement_line_key(stmt004), DECISION_ACKNOWLEDGE, "seen", SOURCE_HUMAN, datetime(2026, 7, 3, tzinfo=timezone.utc))
    )
    report, _ = await effective()
    assert not any(
        g.kind == GapKind.UNMATCHED_IN_LEDGER and g.statement_line is not None and g.statement_line.statement_ref == "STMT-004"
        for g in report.gaps
    )


async def test_effective_reconciliation_stamps_period(harness: Harness):
    """Both the overlaid and the waiver paths stamp period=<closing period>."""
    await _populate_reconcile(harness)
    report, _ = await build_effective_reconciliation(
        config=harness.config, ledger_store=harness.ledger_store, statement_store=harness.statement_store,
        reconciliation_store=harness.reconciliation_store, waiver_store=harness.waiver_store, period=PERIOD,
    )
    assert report.period == PERIOD


# ============================================================================
# AC4 — effective prior-period state (D4)
# ============================================================================


async def _seed_close(store: FileCloseStore, period: str) -> None:
    await store.record(
        CloseRecord(
            period=period, signed_at=datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc), signed_by="human",
            checklist=[], transactions=[], tax={}, reconciliation={}, anomalies=[],
            effective_prior_period_state=None, config_prior_period_state=None,
        )
    )


async def test_effective_prior_blocks_earlier_period_config_unwritten(examples_dir, tmp_path):
    """A signed 2026-Q2 makes a 2026-Q1 review BLOCKED by the framework's own
    period_closeable reason, 2026-Q3 closeable — and the config file is never written."""
    h = _harness(examples_dir, tmp_path)  # example config: prior_period_state unset
    config_bytes = h.config_path.read_bytes()
    await _seed_close(h.close_store, "2026-Q2")

    # 2026-Q1 — at/before the effective prior 2026-Q2 → period_closeable unmet.
    q1 = (await _get_close(h.app, "2026-Q1")).json()
    pc = {c["name"]: c for c in q1["framework"]["checklist"]}["period_closeable"]
    assert pc["met"] is False
    # Verbatim vs a direct close_period with prior=2026-Q2.
    import dataclasses
    eff_cat = await build_effective_categorization(config=h.config, ledger_store=h.ledger_store, confirmation_store=h.confirmation_store, period="2026-Q1")
    eff_recon, _ = await build_effective_reconciliation(config=h.config, ledger_store=h.ledger_store, statement_store=h.statement_store, reconciliation_store=h.reconciliation_store, waiver_store=h.waiver_store, period="2026-Q1")
    tax = await track_tax(h.ledger_store, h.config, "2026-Q1")
    direct = close_period(eff_recon, tax, eff_cat, dataclasses.replace(h.config, prior_period_state="2026-Q2"), "2026-Q1")
    assert pc["reason"] == {c.name: c for c in direct.checklist}["period_closeable"].reason
    assert q1["effective_prior_period_state"] == "2026-Q2"
    assert q1["config_prior_period_state"] is None  # config value untouched

    # 2026-Q3 — after 2026-Q2 → period_closeable met.
    q3 = (await _get_close(h.app, "2026-Q3")).json()
    pc3 = {c["name"]: c for c in q3["framework"]["checklist"]}["period_closeable"]
    assert pc3["met"] is True
    assert q3["effective_prior_period_state"] == "2026-Q2"

    # The config file was never written by the read path (D4: in-memory replace only).
    assert h.config_path.read_bytes() == config_bytes


async def test_effective_prior_falls_back_to_config_when_no_close(examples_dir, tmp_path):
    """With no signed close, the effective prior is the config file value, exposed as-is."""
    h = _harness(examples_dir, tmp_path, prior_period_state="2026-Q1")
    body = (await _get_close(h.app, PERIOD)).json()
    assert body["config_prior_period_state"] == "2026-Q1"
    assert body["effective_prior_period_state"] == "2026-Q1"


# ============================================================================
# AC5 — waiver semantics (read side)
# ============================================================================


async def test_no_statement_no_waiver_blocks(harness: Harness):
    """Zero statement lines + no waiver → source 'missing' and gaps over the empty
    statement (every ledger txn an unmatched_on_statement gap), so it blocks."""
    await harness.ledger_store.store(_aws())
    body = (await _get_close(harness.app)).json()
    assert body["reconciliation_source"] == "missing"
    rc = {c["name"]: c for c in body["framework"]["checklist"]}["reconciliation_clean"]
    assert rc["met"] is False


async def test_waiver_makes_reconciliation_clean_and_waived(harness: Harness):
    """A waiver row → an empty effective report rendered 'waived' (never 'reconciled'),
    reconciliation_clean met."""
    await harness.ledger_store.store(_aws())
    await harness.waiver_store.record(
        Waiver(period=PERIOD, waived_at=datetime(2026, 7, 1, tzinfo=timezone.utc), waived_by="human", note="no feed this period")
    )
    body = (await _get_close(harness.app)).json()
    assert body["reconciliation_source"] == "waived"
    rc = {c["name"]: c for c in body["framework"]["checklist"]}["reconciliation_clean"]
    assert rc["met"] is True
    assert body["app_gates"]["statement_or_waiver"] == {"met": True, "source": "waived"}


async def test_statement_present_ignores_waiver(harness: Harness):
    """With statement lines present the waiver is unavailable: source 'statement',
    any prior waiver row ignored."""
    await _populate_reconcile(harness)
    await harness.waiver_store.record(  # a stale waiver — must be ignored
        Waiver(period=PERIOD, waived_at=datetime(2026, 7, 1, tzinfo=timezone.utc), waived_by="human", note="stale")
    )
    body = (await _get_close(harness.app)).json()
    assert body["reconciliation_source"] == "statement"


# ============================================================================
# AC6 — UnknownTaxRegime surfaced
# ============================================================================


async def test_unknown_tax_regime_surfaces_as_error(examples_dir, tmp_path):
    """An unregistered tax_regime makes GET /close surface the framework error (400),
    never a 200 with a swallowed tax."""
    h = _harness(examples_dir, tmp_path, tax_regime="VAT")
    await h.ledger_store.store(_aws())
    resp = await _get_close(h.app)
    assert resp.status_code == 400
    assert "Unknown tax_regime" in resp.json()["detail"]


async def test_hst_regime_returns_200(harness: Harness):
    """The example config (HST) registers, so GET /close is a clean 200 with tax."""
    await harness.ledger_store.store(_aws())
    resp = await _get_close(harness.app)
    assert resp.status_code == 200
    assert resp.json()["tax"]["regime"] == "HST"


# ============================================================================
# AC7 — anomaly rendering (derived id, disposition, materiality inactivity)
# ============================================================================


async def test_anomaly_rendered_with_derived_id_and_acknowledgment(harness: Harness):
    """The over_materiality flag renders with its derived id + members; an ack from the
    review store shows acknowledged; materiality_check_active True when the floor is set."""
    delta = make_txn(vendor="Delta Airlines", amount="1200.00", tax="0", date=datetime(2026, 5, 3), description="flight")
    await harness.ledger_store.store(delta)

    report = await flag_anomaly(harness.ledger_store, harness.config, PERIOD)
    flag = report.flags[0]
    flag_id = derive_flag_id(flag)

    body = (await _get_close(harness.app)).json()
    assert body["materiality_check_active"] is True
    anomalies = body["anomalies"]
    assert len(anomalies) == 1
    assert anomalies[0]["id"] == flag_id
    assert anomalies[0]["kind"] == "over_materiality"
    assert anomalies[0]["transactions"][0]["vendor"] == "Delta Airlines"
    assert anomalies[0]["acknowledged"] is False
    assert body["app_gates"]["anomalies_reviewed"] == {"met": False, "unacknowledged_count": 1}

    # Acknowledge it → gate B clears and the flag renders acknowledged.
    await harness.anomaly_review_store.record(
        AnomalyReview(
            flag_id=flag_id, kind=flag.kind.value, reason=flag.reason,
            transaction_ids=(transaction_key(delta),), note="reviewed, expected",
            acknowledged_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source=SOURCE_HUMAN,
        )
    )
    body = (await _get_close(harness.app)).json()
    assert body["anomalies"][0]["acknowledged"] is True
    assert body["anomalies"][0]["note"] == "reviewed, expected"
    assert body["app_gates"]["anomalies_reviewed"]["met"] is True


async def test_materiality_inactive_when_floor_unset(examples_dir, tmp_path):
    """With materiality_floor unset the size check is inert: no over_materiality flag,
    and materiality_check_active marks it inactive (never implies it ran)."""
    h = _harness(examples_dir, tmp_path, drop_materiality=True)
    await h.ledger_store.store(
        make_txn(vendor="Delta Airlines", amount="1200.00", tax="0", date=datetime(2026, 5, 3), description="flight")
    )
    body = (await _get_close(h.app)).json()
    assert body["materiality_check_active"] is False
    assert all(a["kind"] != "over_materiality" for a in body["anomalies"])


# ============================================================================
# AC8 — money: every amount an exact-Decimal string
# ============================================================================


async def test_all_money_is_string_including_delta(harness: Harness):
    """Tax totals, per-target reclaimable, gap deltas, and transaction amounts are all
    exact-Decimal strings — no JSON number on any money path."""
    await _populate_reconcile(harness)
    await harness.ledger_store.store(_aws())  # gives a non-zero tax total on target-001
    body = (await _get_close(harness.app)).json()

    assert isinstance(body["tax"]["period_total"], str)
    for t in body["tax"]["per_target"]:
        assert isinstance(t["reclaimable"], str)

    # The amount_mismatch blocker carries a signed exact-Decimal delta string.
    recon_blockers = [b for b in body["framework"]["blockers"] if b["check"] == "reconciliation_clean"]
    mismatch = [b for b in recon_blockers if b["item"] and b["item"].get("kind") == "amount_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["item"]["delta"] == "-2.50"
    assert isinstance(mismatch[0]["item"]["delta"], str)
    assert isinstance(mismatch[0]["item"]["transaction"]["amount"], str)

    # No float leaks onto any money path in the raw JSON (the wire text is authoritative).
    def _no_money_float(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("amount", "tax", "delta", "reclaimable", "period_total"):
                    assert not isinstance(v, float), f"money field {k} is a float"
                _no_money_float(v)
        elif isinstance(node, list):
            for v in node:
                _no_money_float(v)

    _no_money_float(body)


# ============================================================================
# AC9 — one projection: per-transaction status derives from build_ledger
# ============================================================================


async def test_gate_a_pending_matches_build_ledger(harness: Harness):
    """Gate A's pending_count is exactly the build_ledger non-confirmed count — the
    close review and the ledger read the one per-transaction truth."""
    await harness.ledger_store.store(_aws())      # proposed → pending
    await harness.ledger_store.store(_flagged())  # flagged → pending
    body = (await _get_close(harness.app)).json()

    ledger = await build_ledger(
        config=harness.config, ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store, period=PERIOD,
    )
    expected = sum(1 for e in ledger.entries if e.status != "confirmed")
    assert body["app_gates"]["all_confirmed"]["pending_count"] == expected == 2
    assert body["app_gates"]["all_confirmed"]["met"] is False


# ============================================================================
# AC10 — empty-period edge: framework-READY but not signable
# ============================================================================


async def test_empty_period_framework_ready_but_not_signable(harness: Harness):
    """A period with zero transactions and no statement reads framework READY, but
    reconciliation_source is 'missing', gate C fails, and signable is False."""
    body = (await _get_close(harness.app)).json()
    assert body["framework"]["status"] == "ready"  # vacuously clean
    assert body["reconciliation_source"] == "missing"
    assert body["app_gates"]["statement_or_waiver"]["met"] is False
    assert body["signable"] is False


# ============================================================================
# signable — READY + all three gates
# ============================================================================


async def test_signable_when_ready_and_all_gates_met(harness: Harness):
    """AWS confirmed + a waiver + no anomalies + clean tax → READY and all gates met
    → signable True."""
    aws = _aws()
    await harness.ledger_store.store(aws)
    await harness.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(aws), account="5100-software-subscriptions",
                     source=SOURCE_HUMAN, decided_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    )
    await harness.waiver_store.record(
        Waiver(period=PERIOD, waived_at=datetime(2026, 7, 1, tzinfo=timezone.utc), waived_by="human", note="")
    )
    body = (await _get_close(harness.app)).json()
    assert body["framework"]["status"] == "ready"
    assert body["signable"] is True
    assert body["app_gates"]["all_confirmed"]["met"] is True
    assert body["app_gates"]["anomalies_reviewed"]["met"] is True
    assert body["app_gates"]["statement_or_waiver"]["met"] is True
    assert body["summary"]["open"] == 0


async def test_framework_ready_but_gate_a_pending_not_signable(harness: Harness):
    """An unconfirmed owner-rule proposal keeps the framework READY (proposals don't
    block) but fails gate A → not signable."""
    await harness.ledger_store.store(_aws())  # a proposal, never confirmed
    await harness.waiver_store.record(
        Waiver(period=PERIOD, waived_at=datetime(2026, 7, 1, tzinfo=timezone.utc), waived_by="human", note="")
    )
    body = (await _get_close(harness.app)).json()
    assert body["framework"]["status"] == "ready"
    assert body["app_gates"]["all_confirmed"]["met"] is False
    assert body["signable"] is False


# ============================================================================
# Closed-period echo — the stored snapshot is the rendered truth (not a recompute)
# ============================================================================


async def test_closed_period_echoes_stored_record(harness: Harness):
    """An already-closed period returns its stored close record, signable False, and
    does not recompute the live composition."""
    await harness.ledger_store.store(_flagged())  # would otherwise block a live compute
    await harness.close_store.record(
        CloseRecord(
            period=PERIOD, signed_at=datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc), signed_by="human",
            checklist=[{"name": "period_closeable", "met": True, "reason": "ok"}],
            transactions=[], tax={"period_total": "6.50"}, reconciliation={"source": "waived"},
            anomalies=[], effective_prior_period_state="2026-Q1", config_prior_period_state=None,
        )
    )
    body = (await _get_close(harness.app)).json()
    assert body["closed"] is True
    assert body["signable"] is False
    assert body["close_record"]["period"] == PERIOD
    assert body["close_record"]["signed_by"] == "human"
    assert body["close_record"]["tax"] == {"period_total": "6.50"}
    assert body["framework"] is None  # not a recomputation


async def test_build_ledger_carries_closed_flag(harness: Harness):
    """build_ledger with a close store carries the period-level closed + sign audit;
    without it, closed is False (the Slice 1/2 callers are unaffected)."""
    await harness.ledger_store.store(_aws())
    await _seed_close(harness.close_store, PERIOD)

    with_store = await build_ledger(
        config=harness.config, ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store, period=PERIOD, close_store=harness.close_store,
    )
    assert with_store.closed is True
    assert with_store.signed_by == "human"
    assert with_store.signed_at is not None

    without_store = await build_ledger(
        config=harness.config, ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store, period=PERIOD,
    )
    assert without_store.closed is False
    assert without_store.signed_at is None
