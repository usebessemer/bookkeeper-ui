"""Slice 4 · A — the accountant-package preview: build_package + PackageOut + GET /package.

Drives `create_app` with injected temp-path stores (the Slice 1/2/3 style) and
exercises the read side of the Contract A deliverable:

- `views.build_package` — delegates to the shared Slice-3 `build_close_review` for
  the effective `CloseReport`, hands it to `generate_accountant_package` **as-is**,
  and adds the app's additive confirmation overlay;
- `PackageOut` — the wire shape (exact-string money, ISO dates, entries by
  reference via `transaction_id`);
- `GET /package` — the read-only serialization (200 proposed | 200 blocked | 400 on
  an unknown tax regime).

The framework skill (`generate_accountant_package`) is called **as-is**; the app
constructs only the wire schemas and writes nothing here. Scaffolding mirrors
`tests/test_close_review.py` (its `Harness` + `_write_config`); data builders come
from `tests/conftest.py` (`make_txn` / `make_stmt_line`).
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
from bookkeeper.skills.track_tax import track_tax

from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore
from bookkeeper_ui.api import create_app
from bookkeeper_ui.closes import CloseRecord, FileCloseStore
from bookkeeper_ui.confirmations import (
    SOURCE_HUMAN,
    Confirmation,
    FileConfirmationStore,
)
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.schemas import TaxSummaryOut
from bookkeeper_ui.statement_store import FileStatementStore
from bookkeeper_ui.views import build_effective_categorization, build_package
from bookkeeper_ui.waivers import FileWaiverStore, Waiver
from tests.conftest import make_stmt_line, make_txn

PERIOD = "2026-Q2"


# --- Harness + config builders (mirrors test_close_review.py) -----------------


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
    """Write a config (the shipped example + overrides) to a tmp file, load it."""
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    if "tax_regime" in overrides:
        data["tax_regime"] = overrides["tax_regime"]
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


async def _get_package(app: FastAPI, period: str = PERIOD) -> httpx.Response:
    async with _client(app) as client:
        return await client.get("/package", params={"period": period})


# Grounded against the shipped example config (chart + owner_policies + HST):
#   "AWS"            → owner-rule proposal (5100-software-subscriptions), conf 1.0.
#   "Rent"           → chart-match proposal (6100-rent), conf 0.9.
#   "Travel Agency"  → chart-match proposal (5200-travel), conf 0.9.
#   "Zzxq Gibberish" → below the categorize floor → FLAGGED (blocks until confirmed).
def _aws(amount: str = "50.00", tax: str = "0") -> object:
    return make_txn(vendor="AWS", amount=amount, tax=tax, date=datetime(2026, 5, 1), description="cloud")


def _rent() -> object:
    return make_txn(vendor="Rent", amount="20.00", tax="0", date=datetime(2026, 5, 2), description="")


def _flagged() -> object:
    return make_txn(vendor="Zzxq Gibberish", amount="5.00", tax="0", date=datetime(2026, 5, 3), description="")


async def _waive(h: Harness) -> None:
    """Waive reconciliation so a no-statement period reads clean (source='waived')."""
    await h.waiver_store.record(
        Waiver(period=PERIOD, waived_at=datetime(2026, 7, 1, tzinfo=timezone.utc), waived_by="human", note="")
    )


async def _ready_three_sources(h: Harness) -> object:
    """A READY close with all three proposal sources + a waiver → a PROPOSED package.

    AWS (owner-rule) · Rent (chart-match) · Zzxq (flagged → confirmed → human). The
    confirmed flag clears `categorization_complete`; the waiver clears reconciliation.
    Returns the flagged (now human-confirmed) transaction for further assertions.
    """
    await h.ledger_store.store(_aws())
    await h.ledger_store.store(_rent())
    flagged = _flagged()
    await h.ledger_store.store(flagged)
    await h.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(flagged), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    )
    await _waive(h)
    return flagged


# ============================================================================
# AC-6 — READY correctness
# ============================================================================


async def test_ready_close_yields_proposed_package_in_order(harness: Harness):
    """A READY close → status 'proposed'; entries follow the effective categorization
    report's order; summary has open==0 and processed==auto_filed+reviewed;
    accounting_method / jurisdiction stamp the config verbatim."""
    await _ready_three_sources(harness)
    resp = await _get_package(harness.app)
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "proposed"
    assert body["unmet_close"] is None

    # Entry order == the effective categorization proposals' order (ledger read order,
    # then the human proposal appended for the confirmed flag).
    eff_cat = await build_effective_categorization(
        config=harness.config, ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store, period=PERIOD,
    )
    assert [e["transaction"]["vendor"] for e in body["entries"]] == [
        p.transaction.vendor for p in eff_cat.proposals
    ]

    # Summary invariants of a PROPOSED (READY-built) package.
    summary = body["summary"]
    assert summary["open"] == 0
    assert summary["processed"] == summary["auto_filed"] + summary["reviewed"]

    # Config basis stamped verbatim.
    assert body["accounting_method"] == harness.config.accounting_method  # "cash"
    assert body["jurisdiction"] == harness.config.jurisdiction  # "US"


# ============================================================================
# AC-7 — trust trail truthful (each source serialized exactly; human via SOURCE_HUMAN)
# ============================================================================


async def test_trust_trail_sources_are_truthful(harness: Harness):
    """Each entry's proposed_account / confidence / source equal the underlying
    CategoryProposal exactly: an owner-rule shows 'owner-rule', a chart-match shows
    'chart-match', and a human-confirmed flag serializes source==SOURCE_HUMAN with its
    true confidence 1.0 (nothing defaulted or invented)."""
    await _ready_three_sources(harness)
    body = (await _get_package(harness.app)).json()
    by_vendor = {e["transaction"]["vendor"]: e for e in body["entries"]}

    aws = by_vendor["AWS"]
    assert aws["proposed_account"] == "5100-software-subscriptions"
    assert aws["source"] == "owner-rule"
    assert aws["confidence"] == 1.0

    rent = by_vendor["Rent"]
    assert rent["proposed_account"] == "6100-rent"
    assert rent["source"] == "chart-match"
    assert rent["confidence"] == pytest.approx(0.9)

    human = by_vendor["Zzxq Gibberish"]
    assert human["source"] == SOURCE_HUMAN  # the imported constant, not a bare "human"
    assert human["proposed_account"] == "5000-office-supplies"
    assert human["confidence"] == 1.0  # true value kept honest on the JSON (no suppression here)


# ============================================================================
# AC-8 — tax breakout verbatim (exact-string money, no float artefacts)
# ============================================================================


async def test_tax_breakout_is_verbatim_and_exact(harness: Harness):
    """The package's tax_breakout equals TaxSummaryOut.from_model(track_tax(...)):
    per_target order, reclaimable strings, transaction_counts, period_total, and
    regime — with exact-Decimal strings (0.10 + 0.20 → '0.30', never 0.300...4)."""
    await harness.ledger_store.store(_aws(amount="50.00", tax="0.10"))
    await harness.ledger_store.store(_aws(amount="60.00", tax="0.20"))
    await _waive(harness)

    body = (await _get_package(harness.app)).json()
    assert body["status"] == "proposed"

    expected = TaxSummaryOut.from_model(await track_tax(harness.ledger_store, harness.config, PERIOD))
    assert body["tax_breakout"] == expected.model_dump()

    # Spot-check the exact-string sum and no flags on a PROPOSED package.
    assert body["tax_breakout"]["period_total"] == "0.30"
    tgt = [t for t in body["tax_breakout"]["per_target"] if t["attribution_target_id"] == "target-001"][0]
    assert tgt["reclaimable"] == "0.30"
    assert tgt["transaction_count"] == 2
    assert body["tax_breakout"]["regime"] == "HST"
    assert body["tax_breakout"]["flagged"] == []


# ============================================================================
# AC-9 — reconciliation trail (matched pairs render; counts 0 on PROPOSED)
# ============================================================================


async def test_reconciliation_trail_matched_pairs_render(harness: Harness):
    """A clean matched pair renders on the package with its statement_ref; on a
    PROPOSED package to_confirm_count and gap_count are 0."""
    # Travel Agency 100.00 categorizes (chart-match, 5200-travel) AND reconciles
    # cleanly against a same-amount/same-date statement line whose description matches
    # the vendor (similarity 1.0 ≥ the 0.7 floor → matched, not to_confirm).
    txn = make_txn(vendor="Travel Agency", amount="100.00", tax="0",
                   date=datetime(2026, 5, 5), description="travel")
    await harness.ledger_store.store(txn)
    await harness.statement_store.store(
        make_stmt_line(statement_ref="STMT-100", amount="100.00",
                       date=datetime(2026, 5, 5), description="Travel Agency")
    )

    body = (await _get_package(harness.app)).json()
    assert body["status"] == "proposed"
    recon = body["reconciliation"]
    assert [m["statement_ref"] for m in recon["matched"]] == ["STMT-100"]
    assert recon["matched"][0]["transaction_id"] == transaction_key(txn)
    assert recon["matched"][0]["amount"] == "100.00"
    assert recon["matched"][0]["statement_description"] == "Travel Agency"
    assert recon["to_confirm_count"] == 0
    assert recon["gap_count"] == 0


# ============================================================================
# AC-10 — empty period: framework-READY → PROPOSED even though not signable
# ============================================================================


async def test_empty_period_is_proposed_not_gated_on_signable(harness: Harness):
    """A period with zero transactions closes framework-READY → a PROPOSED package
    with zero entries and an all-zero summary — even though the app's gate C
    (statement-or-waiver) is unmet so the close is NOT signable. The projection passes
    close_report.status, never review.signable."""
    resp = await _get_package(harness.app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    assert body["entries"] == []
    assert body["summary"] == {"processed": 0, "auto_filed": 0, "reviewed": 0, "open": 0}
    assert body["tax_breakout"] is not None  # an empty-but-present breakout
    assert body["reconciliation"] is not None
    assert body["divergence_count"] == 0

    # Prove the close is genuinely NOT signable (gate C unmet) — so a signable-gate
    # would have wrongly blocked; the package is PROPOSED anyway.
    async with _client(harness.app) as client:
        close = (await client.get("/close", params={"period": PERIOD})).json()
    assert close["signable"] is False
    assert close["app_gates"]["statement_or_waiver"]["met"] is False


# ============================================================================
# AC-12 — money exactness (exact str(Decimal), never a lossy number)
# ============================================================================


async def test_money_fields_are_exact_strings(harness: Harness):
    """Every money field on the package is the exact str(Decimal) from the store —
    trailing zeros preserved ('82.50', not '82.5'), never a JSON number."""
    await harness.ledger_store.store(_aws(amount="82.50", tax="6.50"))
    await _waive(harness)
    body = (await _get_package(harness.app)).json()

    entry = body["entries"][0]
    assert entry["transaction"]["amount"] == "82.50"
    assert entry["transaction"]["tax"] == "6.50"
    assert entry["tax"] == "6.50"
    # Types are strings, not JSON numbers.
    assert isinstance(entry["transaction"]["amount"], str)
    assert isinstance(entry["tax"], str)
    assert isinstance(body["tax_breakout"]["period_total"], str)


# ============================================================================
# AC-13 — overlay honesty + closed-period BLOCKED
# ============================================================================


async def test_overlay_is_additive_and_never_rewrites_framework_fields(harness: Harness):
    """A matching confirmation sets confirmed_account with diverges=false; a corrected
    one sets diverges=true; the framework fields (proposed_account / confidence /
    source) are byte-identical with and without confirmations; divergence_count equals
    the number of diverging entries."""
    aws = _aws()  # owner-rule → 5100
    await harness.ledger_store.store(aws)
    await harness.ledger_store.store(_rent())  # chart-match → 6100
    await _waive(harness)

    # Baseline: no confirmations → nulls, no divergence.
    base = (await _get_package(harness.app)).json()
    base_by_vendor = {e["transaction"]["vendor"]: e for e in base["entries"]}
    assert base_by_vendor["AWS"]["confirmed_account"] is None
    assert base_by_vendor["AWS"]["diverges"] is False
    assert base["divergence_count"] == 0

    # Confirm AWS to its proposed account → confirmed_account set, NOT diverging.
    await harness.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(aws), account="5100-software-subscriptions",
                     source=SOURCE_HUMAN, decided_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    )
    confirmed = (await _get_package(harness.app)).json()
    conf_aws = {e["transaction"]["vendor"]: e for e in confirmed["entries"]}["AWS"]
    assert conf_aws["confirmed_account"] == "5100-software-subscriptions"
    assert conf_aws["confirmed_at"] is not None
    assert conf_aws["diverges"] is False
    assert confirmed["divergence_count"] == 0
    # Framework fields byte-identical to the no-confirmation baseline (additive only).
    for field in ("proposed_account", "confidence", "source"):
        assert conf_aws[field] == base_by_vendor["AWS"][field]

    # Correct AWS to a DIFFERENT account → diverges=true, divergence_count 1.
    await harness.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(aws), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
    )
    corrected = (await _get_package(harness.app)).json()
    corr_aws = {e["transaction"]["vendor"]: e for e in corrected["entries"]}["AWS"]
    assert corr_aws["confirmed_account"] == "5000-office-supplies"
    assert corr_aws["diverges"] is True
    assert corrected["divergence_count"] == 1
    # Still byte-identical framework fields — the correction never rewrote the package.
    for field in ("proposed_account", "confidence", "source"):
        assert corr_aws[field] == base_by_vendor["AWS"][field]


async def test_closed_period_returns_200_blocked_deferred_snapshot(harness: Harness):
    """An already-closed period returns 200 status='blocked' with the deferred-snapshot
    message — no crash (the close short-circuit precedes the skill call), no snapshot
    re-derivation."""
    await harness.ledger_store.store(_aws())  # would otherwise be a live PROPOSED entry
    await harness.close_store.record(
        CloseRecord(
            period=PERIOD, signed_at=datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc), signed_by="human",
            checklist=[{"name": "period_closeable", "met": True, "reason": "ok"}],
            transactions=[], tax={"period_total": "0"}, reconciliation={"waived": True},
            anomalies=[], summary={}, effective_prior_period_state="2026-Q1", config_prior_period_state=None,
        )
    )
    resp = await _get_package(harness.app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["summary"] is None
    assert body["entries"] == []
    assert body["tax_breakout"] is None
    assert body["reconciliation"] is None
    assert body["divergence_count"] == 0
    assert "already closed" in body["unmet_close"]
    assert "snapshot" in body["unmet_close"]


# ============================================================================
# Blocked-when-open + regime guard (honest BLOCKED 200 · framework 400)
# ============================================================================


async def test_open_but_not_ready_close_is_blocked_200(harness: Harness):
    """An open period whose effective close is BLOCKED (an unresolved flag) → 200
    status='blocked' naming the failing check; entries/breakout/reconciliation null."""
    await harness.ledger_store.store(_flagged())  # blocks categorization_complete
    await _waive(harness)  # isolate the flag as the sole blocker
    resp = await _get_package(harness.app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["entries"] == []
    assert body["summary"] is None
    assert body["tax_breakout"] is None
    assert body["reconciliation"] is None
    assert "categorization_complete" in body["unmet_close"]


async def test_unknown_tax_regime_surfaces_as_400(examples_dir, tmp_path):
    """An unregistered tax_regime makes GET /package surface the framework error (400),
    never a 200 with a swallowed tax (mirrors GET /close)."""
    h = _harness(examples_dir, tmp_path, tax_regime="VAT")
    await h.ledger_store.store(_aws())
    resp = await _get_package(h.app)
    assert resp.status_code == 400
    assert "Unknown tax_regime" in resp.json()["detail"]


async def test_hst_regime_returns_200(harness: Harness):
    """The example config (HST) registers → GET /package is a clean 200."""
    await harness.ledger_store.store(_aws())
    await _waive(harness)
    resp = await _get_package(harness.app)
    assert resp.status_code == 200
    assert resp.json()["tax_breakout"]["regime"] == "HST"
