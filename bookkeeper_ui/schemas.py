"""The API boundary representation — pydantic models mapping the framework
dataclasses → JSON.

The framework types (`Transaction`, `CategorizationReport`, `CategoryProposal`,
`CategoryFlag`) are the **source of truth**; these models are only the wire shape
the thin UI reads. Keeping them here — never in `../agent-classes` — is what
keeps the framework pure: no web/pydantic dependency leaks into it, serialization
happens once, at this boundary.

Two deliberate serialization rules, matching the store's own discipline:

- **Money is a JSON string**, not a float: ``amount`` / ``tax`` carry the exact
  `Decimal` as text (``"82.50"``) so no precision is lost crossing the wire, the
  same way the file store persists it.
- **A transaction carries its stable `id`** — the ledger's `transaction_key` —
  so the UI can post it straight back to `POST /resolve` (a resolution points at
  the transaction it resolves; the two link on this one key).

`artifact_bytes` (the raw source blob) is intentionally **omitted**: it is the
traceable source record, not part of the trust trail the UI renders, and the
read-path projection may drop it anyway (see `LedgerSource`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from bookkeeper.model import StatementLine, Transaction
from bookkeeper.skills.categorize import (
    CategorizationReport,
    CategoryFlag,
    CategoryProposal,
)
from bookkeeper.skills.reconcile import (
    MatchedPair,
    PairToConfirm,
    ReconciliationGap,
    ReconciliationReport,
)

from bookkeeper_ui.confirmations import Confirmation
from bookkeeper_ui.ledger_store import transaction_key
from bookkeeper_ui.reconciliations import Reconciliation
from bookkeeper_ui.statement_store import statement_line_key

# Where a ledger entry stands: a human-`confirmed` account, an agent `proposed`
# one awaiting confirm/correct, or `flagged` for a human to categorize from
# scratch. Drives which of the trust-trail fields below are populated.
LedgerStatus = Literal["confirmed", "proposed", "flagged"]

# The reconcile standing of a ledger entry, overlaid on the categorized ledger by
# Slice 2 (additive to `LedgerStatus`, which stays the categorize dimension): the
# per-transaction fold of `views.build_reconciliation`. `None` = no statement was
# imported for the period (never "everything is a discrepancy" — see the
# no-statement guard). A statement line with no ledger transaction
# (`unmatched_in_ledger`) has no ledger row to annotate, so it never appears here.
ReconciliationStatus = Literal[
    "matched", "confirmed", "to_confirm", "rejected", "gap_open", "gap_acknowledged"
]

# The reconcile gap buckets, exactly as the framework's `GapKind` str-enum values
# (never re-derived): a statement charge the books missed, a captured txn absent
# from the feed, or a date+vendor pair whose amounts differ.
GapKindLiteral = Literal[
    "amount_mismatch", "unmatched_in_ledger", "unmatched_on_statement"
]


class TransactionOut(BaseModel):
    """A `Transaction` on the wire — money as exact strings, plus its stable id."""

    id: str = Field(description="The ledger transaction_key — the id to POST to /resolve.")
    attribution_target_id: str
    vendor: str
    amount: str = Field(description="Exact Decimal as a string (never a lossy float).")
    tax: str
    date: str = Field(description="ISO 8601.")
    description: str

    @classmethod
    def from_model(cls, transaction: Transaction) -> "TransactionOut":
        return cls(
            id=transaction_key(transaction),
            attribution_target_id=transaction.attribution_target_id,
            vendor=transaction.vendor,
            amount=str(transaction.amount),
            tax=str(transaction.tax),
            date=transaction.date.isoformat(),
            description=transaction.description,
        )


class ProposalOut(BaseModel):
    """A `CategoryProposal` — the trust trail: what account, how sure, which rule."""

    transaction: TransactionOut
    proposed_account: str
    confidence: float
    source: str = Field(description="Which rule fired: 'owner-rule' or 'chart-match'.")

    @classmethod
    def from_model(cls, proposal: CategoryProposal) -> "ProposalOut":
        return cls(
            transaction=TransactionOut.from_model(proposal.transaction),
            proposed_account=proposal.proposed_account,
            confidence=proposal.confidence,
            source=proposal.source,
        )


class FlagOut(BaseModel):
    """A `CategoryFlag` — a transaction the agent could not confidently categorize."""

    transaction: TransactionOut
    reason: str = Field(description="Why it needs a human (below threshold, inert, no chart match).")

    @classmethod
    def from_model(cls, flag: CategoryFlag) -> "FlagOut":
        return cls(
            transaction=TransactionOut.from_model(flag.transaction),
            reason=flag.reason,
        )


class CategorizationReportOut(BaseModel):
    """A `CategorizationReport` — proposals (the trust trail) + flagged, for a period."""

    period: str
    proposals: list[ProposalOut]
    flagged: list[FlagOut]

    @classmethod
    def from_model(cls, report: CategorizationReport) -> "CategorizationReportOut":
        return cls(
            period=report.period,
            proposals=[ProposalOut.from_model(p) for p in report.proposals],
            flagged=[FlagOut.from_model(f) for f in report.flagged],
        )


class ResolveRequest(BaseModel):
    """A confirm/correct decision from the UI: which transaction, which account.

    `account` must be one already in `config.chart_of_accounts` — the API rejects
    anything else (§5.2: never invent a category, even by human hand through the
    API). `transaction_id` is the `TransactionOut.id` the decision resolves; it
    must be one the ledger holds, or the API returns a strict 404 (N1: a
    confirmation must never dangle against nothing) rather than persist an orphan.
    """

    transaction_id: str
    account: str


class ConfirmationOut(BaseModel):
    """A recorded `Confirmation` — echoed back so the UI can confirm the write."""

    transaction_id: str
    account: str
    source: str
    decided_at: str = Field(description="ISO 8601 audit timestamp.")

    @classmethod
    def from_model(cls, confirmation: Confirmation) -> "ConfirmationOut":
        return cls(
            transaction_id=confirmation.transaction_id,
            account=confirmation.account,
            source=confirmation.source,
            decided_at=confirmation.decided_at.isoformat(),
        )


class LedgerEntryOut(BaseModel):
    """One transaction in the categorized-ledger view, with its current standing.

    `status` picks which trust-trail fields carry a value:

    - ``confirmed`` — a human resolution exists: `account` is the resolved chart
      account, `source` is ``human``. `confidence`/`reason` are null.
    - ``proposed`` — no resolution yet, the agent proposed confidently: `account`
      is the proposed account, `confidence` and `source` (``owner-rule`` /
      ``chart-match``) carry the trail. `reason` is null.
    - ``flagged`` — needs a human: `reason` carries why. `account`/`confidence`/
      `source` are null.

    `reconciliation` is the **additive, nullable** Slice 2 fold — the entry's
    reconcile standing from `views.build_reconciliation`, or `null` when no
    statement was imported for the period. Slice 1 clients are unaffected: the
    field defaults to `null` and the categorize dimension (`status`) is untouched.
    """

    transaction: TransactionOut
    status: LedgerStatus
    account: str | None = None
    confidence: float | None = None
    source: str | None = None
    reason: str | None = None
    reconciliation: ReconciliationStatus | None = None


class LedgerOut(BaseModel):
    """The categorized ledger for a period — every transaction, in read order."""

    period: str
    entries: list[LedgerEntryOut]


class ImportResultOut(BaseModel):
    """The outcome of `POST /import` — how many transactions were persisted."""

    imported: int
    transactions: list[TransactionOut]


# --- Slice 2: reconcile wire shapes -----------------------------------------
#
# The framework dataclasses (`StatementLine`, `ReconciliationReport` and its
# members) stay the source of truth; these are only the wire shape the reconcile
# surfaces read. Two disciplines carry over: money is an exact **string** (here
# that includes the gap `delta`, a signed exact Decimal from the framework), and
# an item carries its stable `id` so the UI can post it back to `/reconcile/resolve`.


class StatementLineOut(BaseModel):
    """A `StatementLine` on the wire — money as an exact string, plus its stable id.

    `id` is the `statement_line_key` — the id a resolution targets on the
    statement side (the counterpart to `TransactionOut.id`). The card posts *this*
    back as `statement_line_id`, never `statement_ref` (a different value that
    would break both the 404 membership check and the resolution overlay).
    """

    id: str = Field(description="The statement_line_key — the id to POST as statement_line_id.")
    statement_ref: str
    date: str = Field(description="ISO 8601.")
    amount: str = Field(description="Exact Decimal as a string (never a lossy float).")
    description: str

    @classmethod
    def from_model(cls, line: StatementLine) -> "StatementLineOut":
        return cls(
            id=statement_line_key(line),
            statement_ref=line.statement_ref,
            date=line.date.isoformat(),
            amount=str(line.amount),
            description=line.description,
        )


class StatementLinesOut(BaseModel):
    """The stored statement lines for a period — a truth surface for inspection."""

    period: str
    lines: list[StatementLineOut]


class StatementImportResultOut(BaseModel):
    """The outcome of `POST /statements/import` — mirrors `ImportResultOut`.

    `imported` is the count of lines the file parsed to (idempotent re-import adds
    no rows to the store, but still reports the file's line count, exactly as the
    ledger `/import` does); `lines` are those lines, money as exact strings.
    """

    imported: int
    lines: list[StatementLineOut]


class MatchedPairOut(BaseModel):
    """A `MatchedPair` — the trail of what reconciled: a transaction + its line.

    **No confidence field, deliberately** — the framework `MatchedPair` carries no
    similarity score and no reason (it is only the trail of what matched), so none
    is fabricated here. A matched-tier score would be a framework vNext change.
    """

    transaction: TransactionOut
    statement_line: StatementLineOut

    @classmethod
    def from_model(cls, pair: MatchedPair) -> "MatchedPairOut":
        return cls(
            transaction=TransactionOut.from_model(pair.transaction),
            statement_line=StatementLineOut.from_model(pair.statement_line),
        )


class PairToConfirmOut(BaseModel):
    """A `PairToConfirm` — an amount+date pair whose vendors diverge too much.

    Linked (very likely the same charge) but surfaced for a human to confirm or
    reject, never silently matched. `vendor_similarity` is a 0–1 score (a JSON
    number is correct — it is not money, same as a proposal's `confidence`);
    `reason` passes through verbatim.
    """

    transaction: TransactionOut
    statement_line: StatementLineOut
    vendor_similarity: float
    reason: str

    @classmethod
    def from_model(cls, ptc: PairToConfirm) -> "PairToConfirmOut":
        return cls(
            transaction=TransactionOut.from_model(ptc.pair.transaction),
            statement_line=StatementLineOut.from_model(ptc.pair.statement_line),
            vendor_similarity=ptc.vendor_similarity,
            reason=ptc.reason,
        )


class GapOut(BaseModel):
    """A `ReconciliationGap` — one surfaced discrepancy, in one of the three kinds.

    The side(s) present depend on `kind`: an `amount_mismatch` carries both sides
    plus the signed `delta` (an exact-Decimal **string**, never a JSON number); a
    one-sided gap carries only the side that exists and a null `delta`. `reason`
    passes through verbatim.
    """

    kind: GapKindLiteral
    reason: str
    transaction: TransactionOut | None = None
    statement_line: StatementLineOut | None = None
    delta: str | None = Field(
        default=None,
        description="Signed exact Decimal as a string (amount_mismatch only); else null.",
    )

    @classmethod
    def from_model(cls, gap: ReconciliationGap) -> "GapOut":
        return cls(
            kind=gap.kind.value,
            reason=gap.reason,
            transaction=(
                TransactionOut.from_model(gap.transaction)
                if gap.transaction is not None
                else None
            ),
            statement_line=(
                StatementLineOut.from_model(gap.statement_line)
                if gap.statement_line is not None
                else None
            ),
            delta=str(gap.delta) if gap.delta is not None else None,
        )


class ReconciliationReportOut(BaseModel):
    """A `ReconciliationReport` — the raw skill output for a period, order preserved.

    `matched` and `to_confirm` are each independently in statement read order;
    `gaps` are grouped `amount_mismatch`, then `unmatched_in_ledger`, then
    `unmatched_on_statement` — the framework's deterministic order, serialized as-is.
    """

    period: str
    matched: list[MatchedPairOut]
    to_confirm: list[PairToConfirmOut]
    gaps: list[GapOut]

    @classmethod
    def from_model(cls, report: ReconciliationReport) -> "ReconciliationReportOut":
        return cls(
            period=report.period,
            matched=[MatchedPairOut.from_model(m) for m in report.matched],
            to_confirm=[PairToConfirmOut.from_model(p) for p in report.to_confirm],
            gaps=[GapOut.from_model(g) for g in report.gaps],
        )


class ResolveReconcileRequest(BaseModel):
    """One reconcile resolution from a surface: which item, which disposition.

    `transaction_id` (the `TransactionOut.id`) and `statement_line_id` (the
    `StatementLineOut.id`) identify the item — **at least one non-null**. `decision`
    is `confirm`/`reject` (a `to_confirm` pair, both ids) or `acknowledge` (any
    gap). `note` is the human's why (required non-blank for `reject`/`acknowledge`).
    The API validates all of this server-side (422), plus a strict 404 if a
    supplied id references no stored row (N1: never dangle against nothing).
    """

    transaction_id: str | None = None
    statement_line_id: str | None = None
    decision: str
    note: str = ""


class ReconcileResolutionOut(BaseModel):
    """A recorded `Reconciliation` — echoed back so the surface can confirm the write."""

    transaction_id: str | None
    statement_line_id: str | None
    decision: str
    note: str
    source: str
    decided_at: str = Field(description="ISO 8601 audit timestamp.")

    @classmethod
    def from_model(cls, reconciliation: Reconciliation) -> "ReconcileResolutionOut":
        return cls(
            transaction_id=reconciliation.transaction_id,
            statement_line_id=reconciliation.statement_line_id,
            decision=reconciliation.decision,
            note=reconciliation.note,
            source=reconciliation.source,
            decided_at=reconciliation.decided_at.isoformat(),
        )


# --- The overlaid reconcile view (the one projection all surfaces read) ------
#
# `views.build_reconciliation` returns `ReconciliationViewOut`; `GET /reconcile/view`
# serializes it; issue C's template branches on it. Status is embedded per item;
# each of the three lists preserves the framework's report order. This is the
# status-annotated view only — never an "effective" report (that constructor is
# Slice 3 work; see the PR's Slice 3 consumption contract).


class MatchedItemOut(BaseModel):
    """A confident matched pair in the view — agent-confident, no resolution applies."""

    transaction: TransactionOut
    statement_line: StatementLineOut
    status: Literal["matched"] = "matched"


class ToConfirmItemOut(BaseModel):
    """A `to_confirm` pair in the view, carrying its resolution status.

    Stays in the `to_confirm` list even once resolved: `status` is `to_confirm`
    (open), `confirmed`, or `rejected`, with the resolution's `note`/`decided_at`
    when resolved (else null). If the pair lands in framework `matched` on a later
    run it renders in the `matched` list instead and any stale resolution here is
    ignored for status (audit trail only).
    """

    transaction: TransactionOut
    statement_line: StatementLineOut
    vendor_similarity: float
    reason: str
    status: Literal["to_confirm", "confirmed", "rejected"]
    note: str | None = None
    decided_at: str | None = None


class GapItemOut(BaseModel):
    """A gap in the view, carrying its acknowledge status.

    `status` is `gap_open` or `gap_acknowledged` (with the resolution's
    `note`/`decided_at` when acknowledged, else null). Sides and `delta` mirror
    `GapOut` (delta a signed string, null for one-sided kinds).
    """

    kind: GapKindLiteral
    reason: str
    transaction: TransactionOut | None = None
    statement_line: StatementLineOut | None = None
    delta: str | None = None
    status: Literal["gap_open", "gap_acknowledged"]
    note: str | None = None
    decided_at: str | None = None


class ReconciliationViewOut(BaseModel):
    """The overlaid reconcile projection for a period — the single shared truth.

    `statement_lines` is the count of stored lines for the period; **0 is the
    explicit no-statement case** (all three lists empty, and the ledger
    `reconciliation` annotation is null on every entry) — the guard that keeps the
    app from claiming "the feed disagrees" when no feed was imported. Otherwise the
    three lists carry every report item with its overlaid `status`, in report order.
    """

    period: str
    statement_lines: int
    matched: list[MatchedItemOut]
    to_confirm: list[ToConfirmItemOut]
    gaps: list[GapItemOut]
