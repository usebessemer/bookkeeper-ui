"""The shared read projections — one place the API (#2) and the UI (#3) share.

Two projections live here, one per slice's read logic, so each is computed *once*
and every surface renders the same truth:

- `build_ledger` — the categorized ledger (Slice 1): run `categorize` (read-only)
  for the pending proposals/flags, overlay the confirmation store's latest human
  decision, and (Slice 2, additively) fold in each transaction's reconcile standing.
- `build_reconciliation` — the overlaid reconcile view (Slice 2): run
  `reconcile_account` (read-only) and overlay the reconciliation store's latest
  resolution per item, emitting each report item with a `status`. The JSON
  `/reconcile/view` route returns it as-is; the queue UI (issue C) and the ledger
  fold both read *this* result, so there is one reconcile truth, not three.

Both return the wire schemas (`LedgerOut` / `ReconciliationViewOut`) directly, not
a new domain type: the API serializes them and a Jinja template reads their fields
just as happily as JSON.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime

from bookkeeper.config import BookkeeperConfig
from bookkeeper.model import Transaction
from bookkeeper.skills.categorize import (
    CategorizationReport,
    CategoryProposal,
    categorize,
)
from bookkeeper.skills.close_period import CloseReport, CloseStatus, close_period
from bookkeeper.skills.flag_anomaly import flag_anomaly
from bookkeeper.skills.reconcile import (
    GapKind,
    MatchedPair,
    ReconciliationGap,
    ReconciliationReport,
    reconcile_account,
)
from bookkeeper.skills.track_tax import TaxSummary, track_tax

from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore, derive_flag_id
from bookkeeper_ui.closes import CloseRecord, FileCloseStore
from bookkeeper_ui.confirmations import SOURCE_HUMAN, FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.reconciliations import (
    DECISION_ACKNOWLEDGE,
    DECISION_CONFIRM,
    DECISION_REJECT,
    FileReconciliationStore,
)
from bookkeeper_ui.schemas import (
    GapItemOut,
    GapOut,
    LedgerEntryOut,
    LedgerOut,
    MatchedItemOut,
    ReconciliationStatus,
    ReconciliationViewOut,
    StatementLineOut,
    ToConfirmItemOut,
    TransactionOut,
)
from bookkeeper_ui.statement_store import FileStatementStore, statement_line_key
from bookkeeper_ui.waivers import FileWaiverStore


async def build_ledger(
    *,
    config: BookkeeperConfig,
    ledger_store: FileLedgerStore,
    confirmation_store: FileConfirmationStore,
    period: str,
    statement_store: FileStatementStore | None = None,
    reconciliation_store: FileReconciliationStore | None = None,
    close_store: FileCloseStore | None = None,
) -> LedgerOut:
    """The categorized ledger for `period`: every stored transaction (in the
    store's deterministic read order) annotated with its current standing —
    `confirmed` (resolved account), `proposed` (agent trust trail), or `flagged`
    (needs a human).

    Re-runs `categorize` for the pending proposals/flags (it writes nothing) and
    overlays the confirmation store's latest decision per transaction, so a
    resolved transaction shows `confirmed` even if it was first flagged.

    When both `statement_store` and `reconciliation_store` are supplied, each entry
    also carries its reconcile standing (`reconciliation`) from the *same*
    `build_reconciliation` projection — the Slice 2 fold. Omitting them (the Slice 1
    callers) leaves `reconciliation` null: additive, so those callers are unchanged.

    `close_store` is the **additive** Slice 3 param (keyword-only, default `None`,
    mirroring the reconcile stores): supplied, the period-level `closed` /
    `signed_at` / `signed_by` fields carry the period's signed-close standing from
    `by_period()` (the one closed-period truth every surface reads). Omitted (the
    five Slice 1/2 call sites), `closed` is `False` — so those callers are unchanged.
    """
    report = await categorize(ledger_store, config, period)
    transactions = await ledger_store.fetch_for_period(period)
    proposals = {transaction_key(p.transaction): p for p in report.proposals}
    flags = {transaction_key(f.transaction): f for f in report.flagged}
    confirmed = await confirmation_store.latest_by_transaction()

    # The reconcile fold, from the one shared projection (never recomputed here).
    # Absent the reconcile stores (Slice 1 callers), or with no statement imported,
    # every entry's `reconciliation` stays null.
    reconciliation_by_txn: dict[str, ReconciliationStatus] = {}
    if statement_store is not None and reconciliation_store is not None:
        view = await build_reconciliation(
            config=config,
            ledger_store=ledger_store,
            statement_store=statement_store,
            reconciliation_store=reconciliation_store,
            period=period,
        )
        reconciliation_by_txn = _reconciliation_by_transaction(view)

    entries: list[LedgerEntryOut] = []
    for transaction in transactions:
        txn_id = transaction_key(transaction)
        out = TransactionOut.from_model(transaction)
        reconciliation = reconciliation_by_txn.get(txn_id)

        confirmation = confirmed.get(txn_id)
        if confirmation is not None:
            entries.append(
                LedgerEntryOut(
                    transaction=out,
                    status="confirmed",
                    account=confirmation.account,
                    source=confirmation.source,
                    reconciliation=reconciliation,
                )
            )
            continue

        proposal = proposals.get(txn_id)
        if proposal is not None:
            entries.append(
                LedgerEntryOut(
                    transaction=out,
                    status="proposed",
                    account=proposal.proposed_account,
                    confidence=proposal.confidence,
                    source=proposal.source,
                    reconciliation=reconciliation,
                )
            )
            continue

        # Every fetched transaction is partitioned into proposals ∪ flagged by
        # categorize, so this is the flagged branch; the fallback reason is a
        # defensive belt in case a hand-edited ledger drifts from the report.
        flag = flags.get(txn_id)
        entries.append(
            LedgerEntryOut(
                transaction=out,
                status="flagged",
                reason=flag.reason if flag is not None else "Uncategorized.",
                reconciliation=reconciliation,
            )
        )

    # The period-level close standing, from the one closed-period truth
    # (`by_period()`). Absent the close store (the Slice 1/2 callers) → not closed.
    record = None
    if close_store is not None:
        record = (await close_store.by_period()).get(period)
    return LedgerOut(
        period=period,
        entries=entries,
        closed=record is not None,
        signed_at=record.signed_at.isoformat() if record is not None else None,
        signed_by=record.signed_by if record is not None else None,
    )


def _reconciliation_by_transaction(
    view: ReconciliationViewOut,
) -> dict[str, ReconciliationStatus]:
    """Fold the reconcile view down to a per-transaction status for the ledger.

    One-to-one matching means each ledger transaction lands in exactly one bucket,
    so a transaction maps to one status. A statement-only gap
    (`unmatched_in_ledger`) has no transaction and so annotates no ledger row.
    """
    by_txn: dict[str, ReconciliationStatus] = {}
    for matched in view.matched:
        by_txn[matched.transaction.id] = matched.status
    for pair in view.to_confirm:
        by_txn[pair.transaction.id] = pair.status
    for gap in view.gaps:
        if gap.transaction is not None:
            by_txn[gap.transaction.id] = gap.status
    return by_txn


async def build_reconciliation(
    *,
    config: BookkeeperConfig,
    ledger_store: FileLedgerStore,
    statement_store: FileStatementStore,
    reconciliation_store: FileReconciliationStore,
    period: str,
) -> ReconciliationViewOut:
    """The overlaid reconcile view for `period` — the one projection every surface reads.

    1. **No-statement guard (a product-surface rule, not a framework override).**
       If the statement store holds *zero* lines for the period, return the
       explicit no-statement view (`statement_lines: 0`, all lists empty) **without
       running the skill** — never render every transaction as a discrepancy when
       no feed was imported. (The raw `POST /reconcile` endpoint does not
       short-circuit; it returns the skill's truthful report.)
    2. Otherwise run `reconcile_account` (read-only, writes nothing) and overlay the
       reconciliation store's `latest_by_item()` per `(transaction_id,
       statement_line_id)`, emitting each item with its `status`. Report order is
       preserved within each of the three lists.

    A recorded resolution whose item lands in framework `matched` on a later run is
    **ignored for status** (the pair renders `matched`; the resolution stays in the
    audit trail) — matched pairs never consult the overlay. A resolution whose
    decision does not fit the item it lands on (e.g. an `acknowledge` keyed to a
    pair that is now `to_confirm`) is likewise ignored for status.
    """
    statement_lines = await statement_store.fetch_statement(period)
    if not statement_lines:
        # The feed was never imported for this period — do not run the skill and
        # manufacture an all-gaps report. Explicit empty view; ledger annotations
        # fold to null off this (see `_reconciliation_by_transaction`).
        return ReconciliationViewOut(
            period=period, statement_lines=0, matched=[], to_confirm=[], gaps=[]
        )

    report = await reconcile_account(ledger_store, statement_store, config, period)
    latest = await reconciliation_store.latest_by_item()

    # `matched` — agent-confident; no resolution applies (a stale one is ignored).
    matched = [
        MatchedItemOut(
            transaction=TransactionOut.from_model(pair.transaction),
            statement_line=StatementLineOut.from_model(pair.statement_line),
        )
        for pair in report.matched
    ]

    # `to_confirm` — overlay confirm/reject; anything else leaves it open.
    to_confirm: list[ToConfirmItemOut] = []
    for ptc in report.to_confirm:
        txn_id = transaction_key(ptc.pair.transaction)
        stmt_id = statement_line_key(ptc.pair.statement_line)
        resolution = latest.get((txn_id, stmt_id))
        status: str = "to_confirm"
        note: str | None = None
        decided_at: str | None = None
        if resolution is not None and resolution.decision in (
            DECISION_CONFIRM,
            DECISION_REJECT,
        ):
            status = "confirmed" if resolution.decision == DECISION_CONFIRM else "rejected"
            note = resolution.note
            decided_at = resolution.decided_at.isoformat()
        to_confirm.append(
            ToConfirmItemOut(
                transaction=TransactionOut.from_model(ptc.pair.transaction),
                statement_line=StatementLineOut.from_model(ptc.pair.statement_line),
                vendor_similarity=ptc.vendor_similarity,
                reason=ptc.reason,
                status=status,  # type: ignore[arg-type]
                note=note,
                decided_at=decided_at,
            )
        )

    # `gaps` — overlay acknowledge; anything else leaves it open. Either side of a
    # gap's key may be null (a one-sided gap carries only its one id), unlike a
    # to_confirm pair which always carries both — hence the distinct key names.
    # The report→wire mapping (sides + the signed `delta` string) reuses
    # `GapOut.from_model`, so the view delta shares the *one* delta code path the
    # raw `POST /reconcile` uses — no second `str(gap.delta)` to drift (issue #24
    # AC8 / #31).
    gaps: list[GapItemOut] = []
    for gap in report.gaps:
        gap_txn_id = transaction_key(gap.transaction) if gap.transaction is not None else None
        gap_stmt_id = (
            statement_line_key(gap.statement_line)
            if gap.statement_line is not None
            else None
        )
        resolution = latest.get((gap_txn_id, gap_stmt_id))
        gap_status = "gap_open"
        gap_note: str | None = None
        gap_decided_at: str | None = None
        if resolution is not None and resolution.decision == DECISION_ACKNOWLEDGE:
            gap_status = "gap_acknowledged"
            gap_note = resolution.note
            gap_decided_at = resolution.decided_at.isoformat()
        base = GapOut.from_model(gap)
        gaps.append(
            GapItemOut(
                kind=base.kind,
                reason=base.reason,
                transaction=base.transaction,
                statement_line=base.statement_line,
                delta=base.delta,
                status=gap_status,  # type: ignore[arg-type]
                note=gap_note,
                decided_at=gap_decided_at,
            )
        )

    return ReconciliationViewOut(
        period=period,
        statement_lines=len(statement_lines),
        matched=matched,
        to_confirm=to_confirm,
        gaps=gaps,
    )


# --- Slice 3: the effective reports + the shared close-review projection ------
#
# The close checklist (`close_period`) is a pure function of three framework
# reports. The app never hands it the *raw* skill output: it hands **effective**
# reports — the raw report with each persisted human resolution applied — so a
# flagged-then-confirmed transaction, a confirmed/rejected reconcile pair, an
# acknowledged gap, and a no-statement waiver all read correctly at close. These
# two constructors build the effective reports as **real framework dataclasses**
# (never by mutating a Slice-2 view), and `build_close_review` composes the one
# shared close projection over them — the single truth `GET /close` (this issue)
# and `GET /ui/close` (issue E) both render, and the seam Slice 4 binds to.


async def build_effective_categorization(
    *,
    config: BookkeeperConfig,
    ledger_store: FileLedgerStore,
    confirmation_store: FileConfirmationStore,
    period: str,
) -> CategorizationReport:
    """The effective `CategorizationReport` for `period` — raw `categorize` + confirmations.

    Start from raw `categorize` (writes nothing). Every `flagged` entry whose
    transaction has a confirmation-store resolution is **moved out of `flagged` and
    into `proposals`** as a human proposal (`proposed_account` = the confirmed
    account, `confidence=1.0`, `source=SOURCE_HUMAN` — the app's ``"human"``
    convention, a free-text source string, *not* a framework constant). Raw agent
    proposals pass through unchanged (they never block); a flag with no confirmation
    stays flagged (it still blocks). The report **must** carry `period=<the closing
    period>`, or `close_period`'s `period_coherent` precondition fail-safe-BLOCKs.

    The synthetic `confidence=1.0` is never rendered as an agent claim — close-review
    status renders from `build_ledger` (whose confirmed rows carry `source="human"`
    and no confidence). It exists only so the framework counts the item as filed.
    """
    raw = await categorize(ledger_store, config, period)
    confirmed = await confirmation_store.latest_by_transaction()

    proposals = list(raw.proposals)
    flagged = []
    for flag in raw.flagged:
        confirmation = confirmed.get(transaction_key(flag.transaction))
        if confirmation is not None:
            proposals.append(
                CategoryProposal(
                    transaction=flag.transaction,
                    proposed_account=confirmation.account,
                    confidence=1.0,
                    source=SOURCE_HUMAN,
                )
            )
        else:
            flagged.append(flag)

    return CategorizationReport(
        period=period,
        proposals=tuple(proposals),
        flagged=tuple(flagged),
    )


async def build_effective_reconciliation(
    *,
    config: BookkeeperConfig,
    ledger_store: FileLedgerStore,
    statement_store: FileStatementStore,
    reconciliation_store: FileReconciliationStore,
    waiver_store: FileWaiverStore | None,
    period: str,
) -> tuple[ReconciliationReport, str]:
    """The effective `ReconciliationReport` for `period` + its `reconciliation_source`.

    The close-input sibling of `build_reconciliation` (status-view → real framework
    report). Returns a `(ReconciliationReport, source)` pair where `source` is one of
    ``"statement"`` / ``"waived"`` / ``"missing"`` (gate C reads it).

    - **No statement lines + a waiver row** → an empty report (backed by the recorded
      waiver), `source="waived"` — rendered/snapshotted as *waived*, never
      "reconciled".
    - **No statement lines + no waiver** → run `reconcile_account` against the empty
      statement and let it block honestly (`source="missing"`); never fabricate a
      clean report. (Zero transactions too → a vacuously-empty report that is
      framework-clean, but `source` still `"missing"` so gate C fails — AC10.)
    - **Statement lines present** → run `reconcile_account` and overlay the
      reconciliation store's `latest_by_item()` (any prior waiver row is ignored),
      `source="statement"`:
        - a **confirmed** `to_confirm` pair → a `MatchedPair` in `matched`;
        - a **rejected** `to_confirm` pair → decomposed into its two constituent
          one-sided gaps (a real, close-blocking discrepancy);
        - an **acknowledged** gap → dropped; an unresolved pair/gap stays (blocks);
        - `matched` pairs never consult the overlay (a stale resolution is ignored).

    Both paths stamp `period=<the closing period>` — `close_period`'s
    `period_coherent` fail-safe-BLOCKs on any input report whose `.period` differs.
    """
    statement_lines = await statement_store.fetch_statement(period)

    if not statement_lines:
        waived = waiver_store is not None and period in (await waiver_store.by_period())
        if waived:
            # Empty, backed by the recorded waiver — rendered/snapshotted as waived.
            return (
                ReconciliationReport(period=period, matched=(), to_confirm=(), gaps=()),
                "waived",
            )
        # No statement and no waiver: run the skill against the empty statement and
        # let it block honestly (every ledger txn → an unmatched_on_statement gap).
        # Never short-circuit to a clean report here (unlike the Slice-2 view guard).
        report = await reconcile_account(ledger_store, statement_store, config, period)
        return (
            ReconciliationReport(
                period=period,
                matched=report.matched,
                to_confirm=report.to_confirm,
                gaps=report.gaps,
            ),
            "missing",
        )

    report = await reconcile_account(ledger_store, statement_store, config, period)
    latest = await reconciliation_store.latest_by_item()

    # `matched` — never consult the overlay (mirrors build_reconciliation).
    matched = list(report.matched)
    to_confirm = []

    # `to_confirm` — confirm → matched; reject → two one-sided gaps; else stays open.
    rejected_gaps: list[ReconciliationGap] = []
    for ptc in report.to_confirm:
        txn_id = transaction_key(ptc.pair.transaction)
        stmt_id = statement_line_key(ptc.pair.statement_line)
        resolution = latest.get((txn_id, stmt_id))
        decision = resolution.decision if resolution is not None else None
        if decision == DECISION_CONFIRM:
            matched.append(
                MatchedPair(ptc.pair.transaction, ptc.pair.statement_line)
            )
        elif decision == DECISION_REJECT:
            # A rejected match is an unexplained discrepancy: decompose into the two
            # constituent one-sided gaps, both of which block `reconciliation_clean`.
            rejected_gaps.append(
                ReconciliationGap(
                    kind=GapKind.UNMATCHED_ON_STATEMENT,
                    reason=(
                        f"Human rejected the amount+date link to statement line "
                        f"{ptc.pair.statement_line.statement_ref!r} — the captured "
                        f"transaction has no confirmed statement counterpart."
                    ),
                    transaction=ptc.pair.transaction,
                )
            )
            rejected_gaps.append(
                ReconciliationGap(
                    kind=GapKind.UNMATCHED_IN_LEDGER,
                    reason=(
                        f"Human rejected the amount+date link for statement line "
                        f"{ptc.pair.statement_line.statement_ref!r} — the statement "
                        f"charge has no confirmed ledger counterpart."
                    ),
                    statement_line=ptc.pair.statement_line,
                )
            )
        else:
            to_confirm.append(ptc)

    # `gaps` — drop an acknowledged gap; everything else stays open. Append the
    # rejected-pair gaps after the raw gaps (deterministic: report order, then
    # to_confirm order).
    kept_gaps: list[ReconciliationGap] = []
    for gap in report.gaps:
        gap_txn_id = transaction_key(gap.transaction) if gap.transaction is not None else None
        gap_stmt_id = (
            statement_line_key(gap.statement_line)
            if gap.statement_line is not None
            else None
        )
        resolution = latest.get((gap_txn_id, gap_stmt_id))
        if (
            resolution is not None
            and resolution.decision == DECISION_ACKNOWLEDGE
            and gap.kind in (GapKind.UNMATCHED_IN_LEDGER, GapKind.UNMATCHED_ON_STATEMENT)
        ):
            # An acknowledged *one-sided* gap (a missing/absent entry the human has
            # explained) clears. An AMOUNT_MISMATCH is a live money disagreement
            # (books vs the authoritative statement) — the framework blocks on it and
            # an acknowledge changes no amounts, so it must be corrected-and-re-run or
            # rejected before it clears. Never close over an unresolved dollar delta.
            continue
        kept_gaps.append(gap)

    return (
        ReconciliationReport(
            period=period,
            matched=tuple(matched),
            to_confirm=tuple(to_confirm),
            gaps=tuple(kept_gaps + rejected_gaps),
        ),
        "statement",
    )


@dataclass(frozen=True)
class AnomalyItem:
    """One `flag_anomaly` flag with its app-derived id and review disposition.

    `id` is the deterministic app-derived id (`derive_flag_id`); the framework flag
    carries none. `transactions` are the flag's member framework `Transaction`s.
    `acknowledged` (+ `acknowledged_at` / `note`) is the disposition overlaid from
    the anomaly-review store (gate B reads it).
    """

    id: str
    kind: str
    reason: str
    transactions: tuple[Transaction, ...]
    acknowledged: bool
    acknowledged_at: datetime | None
    note: str | None


@dataclass(frozen=True)
class GateResult:
    """One app gate's verdict + its count (pending / unacknowledged; 0 for gate C)."""

    met: bool
    count: int


@dataclass(frozen=True)
class CloseReview:
    """The composed close-review projection for a period — the one shared close truth.

    Holds the framework `CloseReport` **by name** (`close_report`, the effective
    close over the effective reports — the seam Slice 4 binds to, never a
    recomputation), plus the effective reports, the tax summary, the anomaly overlay,
    the app gates, and `signable`. `build_close_review` returns it; the API/UI
    serialize it (`CloseReviewOut.from_review`). `effective_prior_period_state` /
    `config_prior_period_state` carry the D4 effective-prior substitution and the
    untouched config-file value.

    For an **already-closed** period, `closed=True` and `close_record` is the stored
    signed snapshot — the rendered truth (a *read of the record*, not a recompute);
    the composition fields are `None`.
    """

    period: str
    closed: bool
    close_record: CloseRecord | None
    close_report: CloseReport | None
    effective_categorization: CategorizationReport | None
    effective_reconciliation: ReconciliationReport | None
    tax_summary: TaxSummary | None
    anomalies: tuple[AnomalyItem, ...]
    materiality_check_active: bool
    reconciliation_source: str
    ledger: LedgerOut | None
    gate_all_confirmed: GateResult
    gate_anomalies_reviewed: GateResult
    gate_statement_or_waiver: GateResult
    signable: bool
    effective_prior_period_state: str | None
    config_prior_period_state: str | None


async def build_close_review(
    *,
    config: BookkeeperConfig,
    ledger_store: FileLedgerStore,
    confirmation_store: FileConfirmationStore,
    statement_store: FileStatementStore,
    reconciliation_store: FileReconciliationStore,
    close_store: FileCloseStore | None,
    anomaly_review_store: FileAnomalyReviewStore | None,
    waiver_store: FileWaiverStore | None,
    period: str,
) -> CloseReview:
    """Compose the one shared close-review projection for `period`.

    An **already-closed** period (in `close_store.by_period()`) returns its stored
    signed snapshot as the rendered truth — never a recomputation.

    Otherwise:

    1. Build the effective `CategorizationReport` + `ReconciliationReport` (raw skill
       output + persisted human resolutions, incl. the no-statement waiver path).
    2. Run `track_tax` (surfacing `UnknownTaxRegime`, never swallowing) and
       `flag_anomaly` **as-is**.
    3. Apply the **effective prior-period state (D4)**: the latest close record's
       period if the close store is non-empty, else `config.prior_period_state` —
       substituted **only** into the `close_period` call via
       `dataclasses.replace` (the config file is never written; no other skill sees
       the replaced value).
    4. Overlay anomaly acknowledgments (matched on the derived flag id) and evaluate
       the three **app gates** (all-confirmed / anomalies-reviewed /
       statement-or-waiver).
    5. `signable` = the effective `CloseReport` is READY **and** all three gates met.
    """
    # Closed-period truth first — a signed period renders its stored snapshot.
    closed_map = await close_store.by_period() if close_store is not None else {}
    if period in closed_map:
        return CloseReview(
            period=period,
            closed=True,
            close_record=closed_map[period],
            close_report=None,
            effective_categorization=None,
            effective_reconciliation=None,
            tax_summary=None,
            anomalies=(),
            materiality_check_active=config.materiality_floor is not None,
            reconciliation_source="missing",
            ledger=None,
            gate_all_confirmed=GateResult(met=False, count=0),
            gate_anomalies_reviewed=GateResult(met=False, count=0),
            gate_statement_or_waiver=GateResult(met=False, count=0),
            signable=False,
            effective_prior_period_state=None,
            config_prior_period_state=config.prior_period_state,
        )

    # The effective reports (raw skill output + persisted human resolutions).
    effective_categorization = await build_effective_categorization(
        config=config,
        ledger_store=ledger_store,
        confirmation_store=confirmation_store,
        period=period,
    )
    effective_reconciliation, reconciliation_source = await build_effective_reconciliation(
        config=config,
        ledger_store=ledger_store,
        statement_store=statement_store,
        reconciliation_store=reconciliation_store,
        waiver_store=waiver_store,
        period=period,
    )

    # The framework computation skills, called as-is. `UnknownTaxRegime` propagates
    # (the route surfaces it) — never swallowed into a 200 with an empty tax.
    tax_summary = await track_tax(ledger_store, config, period)
    anomaly_report = await flag_anomaly(ledger_store, config, period)

    # The effective prior-period state (D4): the latest signed close's period, else
    # the config value. Substituted ONLY for the close_period call (config unwritten).
    latest_close = await close_store.latest() if close_store is not None else None
    effective_prior = (
        latest_close.period if latest_close is not None else config.prior_period_state
    )
    close_config = dataclasses.replace(config, prior_period_state=effective_prior)
    close_report = close_period(
        effective_reconciliation,
        tax_summary,
        effective_categorization,
        close_config,
        period,
    )

    # The ledger projection — per-transaction status is the one truth (gate A source).
    ledger = await build_ledger(
        config=config,
        ledger_store=ledger_store,
        confirmation_store=confirmation_store,
        period=period,
        statement_store=statement_store,
        reconciliation_store=reconciliation_store,
        close_store=close_store,
    )

    # Anomaly acknowledgment overlay (matched on the derived flag id) + gate B count.
    reviews = (
        await anomaly_review_store.by_flag_id() if anomaly_review_store is not None else {}
    )
    anomalies: list[AnomalyItem] = []
    unacknowledged = 0
    for flag in anomaly_report.flags:
        flag_id = derive_flag_id(flag)
        review = reviews.get(flag_id)
        if review is None:
            unacknowledged += 1
        anomalies.append(
            AnomalyItem(
                id=flag_id,
                kind=flag.kind.value,
                reason=flag.reason,
                transactions=flag.transactions,
                acknowledged=review is not None,
                acknowledged_at=review.acknowledged_at if review is not None else None,
                note=review.note if review is not None else None,
            )
        )

    # The three app gates (labeled app policy, distinct from the framework checklist).
    pending = sum(1 for e in ledger.entries if e.status != "confirmed")
    gate_all_confirmed = GateResult(met=pending == 0, count=pending)
    gate_anomalies_reviewed = GateResult(met=unacknowledged == 0, count=unacknowledged)
    gate_statement_or_waiver = GateResult(
        met=reconciliation_source != "missing", count=0
    )

    signable = (
        close_report.status == CloseStatus.READY
        and gate_all_confirmed.met
        and gate_anomalies_reviewed.met
        and gate_statement_or_waiver.met
    )

    return CloseReview(
        period=period,
        closed=False,
        close_record=None,
        close_report=close_report,
        effective_categorization=effective_categorization,
        effective_reconciliation=effective_reconciliation,
        tax_summary=tax_summary,
        anomalies=tuple(anomalies),
        materiality_check_active=config.materiality_floor is not None,
        reconciliation_source=reconciliation_source,
        ledger=ledger,
        gate_all_confirmed=gate_all_confirmed,
        gate_anomalies_reviewed=gate_anomalies_reviewed,
        gate_statement_or_waiver=gate_statement_or_waiver,
        signable=signable,
        effective_prior_period_state=effective_prior,
        config_prior_period_state=config.prior_period_state,
    )
