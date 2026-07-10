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

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.categorize import categorize
from bookkeeper.skills.reconcile import reconcile_account

from bookkeeper_ui.confirmations import FileConfirmationStore
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


async def build_ledger(
    *,
    config: BookkeeperConfig,
    ledger_store: FileLedgerStore,
    confirmation_store: FileConfirmationStore,
    period: str,
    statement_store: FileStatementStore | None = None,
    reconciliation_store: FileReconciliationStore | None = None,
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

    return LedgerOut(period=period, entries=entries)


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
