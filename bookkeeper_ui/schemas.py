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

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from bookkeeper.model import StatementLine, Transaction
from bookkeeper.skills.categorize import (
    CategorizationReport,
    CategoryFlag,
    CategoryProposal,
)
from bookkeeper.skills.close_period import (
    CloseBlocker,
    CloseReport,
    CloseStatus,
)
from bookkeeper.skills.generate_package import PackageSummary
from bookkeeper.skills.reconcile import (
    MatchedPair,
    PairToConfirm,
    ReconciliationGap,
    ReconciliationReport,
)
from bookkeeper.skills.track_tax import TaxFlag, TaxSummary

from bookkeeper_ui.anomaly_reviews import AnomalyReview
from bookkeeper_ui.candidates import ACTION_CONFIRM, CandidateDecision, CandidateSubmission
from bookkeeper_ui.confirmations import Confirmation
from bookkeeper_ui.ledger_store import transaction_key
from bookkeeper_ui.reconciliations import Reconciliation
from bookkeeper_ui.statement_store import statement_line_key
from bookkeeper_ui.waivers import Waiver

if TYPE_CHECKING:  # avoid a runtime import cycle (views/exporter import this module)
    from bookkeeper_ui.closes import CloseRecord
    from bookkeeper_ui.exporter import ExportRecord
    from bookkeeper_ui.views import CloseReview

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
    """The categorized ledger for a period — every transaction, in read order.

    `closed` is the **additive** Slice 3 period-level close standing — `True` iff a
    signed close record exists for the period (`FileCloseStore.by_period()`), with
    `signed_at` / `signed_by` carrying the sign-off audit when closed. It is a
    property of the *period*, not each row, so it lives here on the envelope and not
    on `LedgerEntryOut`. Sourced from the same close-store truth every surface reads
    (banners are issue E). Additive + defaulted: the Slice 1/2 callers that pass no
    `close_store` to `build_ledger` see `closed=False` and are unaffected.
    """

    period: str
    entries: list[LedgerEntryOut]
    closed: bool = False
    signed_at: str | None = Field(default=None, description="ISO 8601 sign-off time; null unless closed.")
    signed_by: str | None = Field(default=None, description="Who signed the close; null unless closed.")


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


# --- Slice 3: the close-review projection (the one shared close truth) --------
#
# `views.build_close_review` composes the framework's real close checklist
# (`close_period`) over the *effective* reports (raw skill output + persisted human
# resolutions), plus the period's tax (`track_tax`), anomalies (`flag_anomaly`), and
# the app gates. It returns the `CloseReview` view-model (which holds the framework
# `CloseReport` **by name** — the Slice 4 seam); `CloseReviewOut.from_review`
# serializes it here for `GET /close`. The framework checklist + blockers are
# rendered **verbatim** (AC2): the same five checks, the framework's own `reason`
# strings, every blocker with its underlying item. Money everywhere is an exact
# string (tax totals + gap deltas), reusing the shipped `*.from_model` serializers
# so there is one money code path.


class CloseCheckOut(BaseModel):
    """One `close_period` precondition — the framework's verdict, verbatim (AC2)."""

    name: str
    met: bool
    reason: str


class BlockerOut(BaseModel):
    """One `CloseBlocker` — the failed check, the framework reason, the open item.

    `item` is a **tagged union** carrying the underlying framework item verbatim
    (reusing the shipped `GapOut` / `PairToConfirmOut` / `FlagOut` / `TaxFlagOut`
    serializers, so money stays an exact string and a statement line keeps its
    load-bearing `id`), tagged by `type`, or `null` for the two period guards
    (`period_closeable` / `period_coherent`), whose blocker is about the period
    itself, not a report item.
    """

    check: str
    reason: str
    item: dict[str, object] | None = None


class FrameworkCloseOut(BaseModel):
    """The `close_period` result rendered verbatim: the five checks + every blocker.

    `status` is the framework `CloseStatus` value (`ready` / `blocked`), the
    `checklist` always all five checks, and `blockers` every `CloseBlocker` with its
    `check` / `reason` / underlying `item` — nothing added, dropped, or re-worded.
    """

    status: Literal["ready", "blocked"]
    checklist: list[CloseCheckOut]
    blockers: list[BlockerOut]


class CloseSummaryOut(BaseModel):
    """The `PeriodSummary` disposition counts — present only on a READY close.

    Note `auto_filed` is the framework's own bucketing (it counts the *effective*
    proposals, so human-confirmed items are included); the app-truth per-transaction
    disposition renders from `build_ledger` and is snapshotted separately (issue D).
    """

    processed: int
    auto_filed: int
    reviewed: int
    open: int


class TaxFlagOut(BaseModel):
    """A `TaxFlag` — a transaction the regime held out of the totals (§5.3)."""

    transaction: TransactionOut
    reason: str

    @classmethod
    def from_model(cls, flag: TaxFlag) -> "TaxFlagOut":
        return cls(
            transaction=TransactionOut.from_model(flag.transaction),
            reason=flag.reason,
        )


class TargetTaxOut(BaseModel):
    """A `TargetTax` — reclaimable tax totalled for one attribution target.

    `reclaimable` is the exact-Decimal sum **as a string** (never a JSON number);
    `transaction_count` is the number of transactions it was built from.
    """

    attribution_target_id: str
    reclaimable: str = Field(description="Exact Decimal as a string (never a lossy float).")
    transaction_count: int


class TaxSummaryOut(BaseModel):
    """A `TaxSummary` on the wire — per-target + period totals as exact strings."""

    period: str
    regime: str
    period_total: str = Field(description="Exact Decimal as a string (never a lossy float).")
    per_target: list[TargetTaxOut]
    flagged: list[TaxFlagOut]

    @classmethod
    def from_model(cls, tax: TaxSummary) -> "TaxSummaryOut":
        return cls(
            period=tax.period,
            regime=tax.regime,
            period_total=str(tax.period_total),
            per_target=[
                TargetTaxOut(
                    attribution_target_id=t.attribution_target_id,
                    reclaimable=str(t.reclaimable),
                    transaction_count=t.transaction_count,
                )
                for t in tax.per_target
            ],
            flagged=[TaxFlagOut.from_model(f) for f in tax.flagged],
        )


class AnomalyOut(BaseModel):
    """One `flag_anomaly` flag with its app-derived id and review disposition.

    `id` is the deterministic app-derived id (`anomaly_reviews.derive_flag_id`) — the
    framework flag carries none. `acknowledged` (+ `acknowledged_at` / `note`) is the
    disposition overlaid from the anomaly-review store. Flags are advisory; they gate
    nothing in the framework (the "all anomalies reviewed" rule is the app's gate B).
    """

    id: str
    kind: str
    reason: str
    transactions: list[TransactionOut]
    acknowledged: bool
    acknowledged_at: str | None = None
    note: str | None = None


class GateAllConfirmedOut(BaseModel):
    """App gate A — every ledger entry for the period is `confirmed`."""

    met: bool
    pending_count: int


class GateAnomaliesReviewedOut(BaseModel):
    """App gate B — every current anomaly flag has a recorded acknowledgment."""

    met: bool
    unacknowledged_count: int


class GateStatementOrWaiverOut(BaseModel):
    """App gate C — the period has a statement to reconcile against, or a waiver."""

    met: bool
    source: Literal["statement", "waived", "missing"]


class AppGatesOut(BaseModel):
    """The three app gates — the app's own close policy, distinct from the checklist.

    These are labeled app policy layered over the framework's `close_period` (which
    knows nothing of anomalies or the statement-present rule). `signable` requires
    the framework `CloseReport` be READY **and** all three of these met.
    """

    all_confirmed: GateAllConfirmedOut
    anomalies_reviewed: GateAnomaliesReviewedOut
    statement_or_waiver: GateStatementOrWaiverOut


def _blocker_item_out(item: object) -> dict[str, object] | None:
    """Serialize one `CloseBlocker.item` to its tagged-union wire shape (or null).

    Reuses the shipped `*.from_model` serializers so money stays an exact string and
    a statement line keeps its load-bearing `id`; the two period guards carry no
    item (`None`).
    """
    if item is None:
        return None
    if isinstance(item, ReconciliationGap):
        return {"type": "reconciliation_gap", **GapOut.from_model(item).model_dump()}
    if isinstance(item, PairToConfirm):
        return {"type": "pair_to_confirm", **PairToConfirmOut.from_model(item).model_dump()}
    if isinstance(item, CategoryFlag):
        return {"type": "category_flag", **FlagOut.from_model(item).model_dump()}
    if isinstance(item, TaxFlag):
        return {"type": "tax_flag", **TaxFlagOut.from_model(item).model_dump()}
    return None  # defensive: an unknown item kind carries no wire shape


def _framework_out(report: CloseReport) -> FrameworkCloseOut:
    """Render a `CloseReport` verbatim — the five checks + every blocker (AC2)."""
    return FrameworkCloseOut(
        status=report.status.value,  # type: ignore[arg-type]
        checklist=[
            CloseCheckOut(name=c.name, met=c.met, reason=c.reason) for c in report.checklist
        ],
        blockers=[
            BlockerOut(check=b.check, reason=b.reason, item=_blocker_item_out(b.item))
            for b in report.blockers
        ],
    )


class CloseReviewOut(BaseModel):
    """The composed close-review projection for a period — `GET /close`'s wire shape.

    Two shapes in one envelope, distinguished by `closed`:

    - **Open period** — the live composition: the framework `framework` checklist,
      the `summary` (READY only), `tax`, `anomalies`, the `reconciliation_source`,
      the three `app_gates`, and `signable`. `close_record` is null.
    - **Closed period** — the stored signed snapshot rendered as the truth (a
      **read of the record, not a recomputation**): `closed=True`, `close_record`
      carries the durable close record, `signable=False`. The open-only composition
      fields are null (the signed snapshot is the record; issue E renders it).

    `effective_prior_period_state` / `config_prior_period_state` expose the D4
    effective-prior substitution (the prior label the close was struck against) and
    the untouched config-file value. `materiality_check_active` marks whether the
    `over_materiality` anomaly check ran (it is inert when `materiality_floor` is
    unset) — so a consumer never mistakes "no size flags" for "the check ran clean".
    Money everywhere is an exact string.
    """

    period: str
    closed: bool
    close_record: dict[str, object] | None = None
    framework: FrameworkCloseOut | None = None
    summary: CloseSummaryOut | None = None
    tax: TaxSummaryOut | None = None
    anomalies: list[AnomalyOut] | None = None
    materiality_check_active: bool | None = None
    reconciliation_source: Literal["statement", "waived", "missing"] | None = None
    app_gates: AppGatesOut | None = None
    signable: bool = False
    effective_prior_period_state: str | None = None
    config_prior_period_state: str | None = None

    @classmethod
    def from_review(cls, review: "CloseReview") -> "CloseReviewOut":
        """Serialize a `views.CloseReview` view-model to the wire shape.

        A **closed** review echoes its stored `close_record` (the signed snapshot is
        the rendered truth, never a recomputation); the open-only fields stay null.
        An **open** review renders the live composition.
        """
        if review.closed:
            record = review.close_record
            return cls(
                period=review.period,
                closed=True,
                close_record=_close_record_out(record) if record is not None else None,
                signable=False,
            )

        assert review.close_report is not None  # open review always composes one
        report = review.close_report
        summary = (
            CloseSummaryOut(
                processed=report.proposed_close.summary.processed,
                auto_filed=report.proposed_close.summary.auto_filed,
                reviewed=report.proposed_close.summary.reviewed,
                open=report.proposed_close.summary.open,
            )
            if report.proposed_close is not None
            else None
        )
        assert review.tax_summary is not None
        return cls(
            period=review.period,
            closed=False,
            close_record=None,
            framework=_framework_out(report),
            summary=summary,
            tax=TaxSummaryOut.from_model(review.tax_summary),
            anomalies=[
                AnomalyOut(
                    id=a.id,
                    kind=a.kind,
                    reason=a.reason,
                    transactions=[TransactionOut.from_model(t) for t in a.transactions],
                    acknowledged=a.acknowledged,
                    acknowledged_at=(
                        a.acknowledged_at.isoformat() if a.acknowledged_at is not None else None
                    ),
                    note=a.note,
                )
                for a in review.anomalies
            ],
            materiality_check_active=review.materiality_check_active,
            reconciliation_source=review.reconciliation_source,  # type: ignore[arg-type]
            app_gates=AppGatesOut(
                all_confirmed=GateAllConfirmedOut(
                    met=review.gate_all_confirmed.met,
                    pending_count=review.gate_all_confirmed.count,
                ),
                anomalies_reviewed=GateAnomaliesReviewedOut(
                    met=review.gate_anomalies_reviewed.met,
                    unacknowledged_count=review.gate_anomalies_reviewed.count,
                ),
                statement_or_waiver=GateStatementOrWaiverOut(
                    met=review.gate_statement_or_waiver.met,
                    source=review.reconciliation_source,  # type: ignore[arg-type]
                ),
            ),
            signable=review.signable,
            effective_prior_period_state=review.effective_prior_period_state,
            config_prior_period_state=review.config_prior_period_state,
        )


def _close_record_out(record: "CloseRecord") -> dict[str, object]:
    """Serialize a stored `CloseRecord` to its wire dict (the signed snapshot).

    The record's snapshot payloads are already JSON-native with money pre-stringified
    (the close store's discipline — and issue D's in-memory sign path mirrors it), so
    they pass through verbatim; only the `signed_at` datetime is rendered ISO 8601.
    """
    return {
        "period": record.period,
        "signed_at": record.signed_at.isoformat(),
        "signed_by": record.signed_by,
        "checklist": [dict(c) for c in record.checklist],
        "transactions": [dict(t) for t in record.transactions],
        "tax": dict(record.tax),
        "reconciliation": dict(record.reconciliation),
        "anomalies": [dict(a) for a in record.anomalies],
        "summary": dict(record.summary),
        "effective_prior_period_state": record.effective_prior_period_state,
        "config_prior_period_state": record.config_prior_period_state,
    }


class CloseRecordOut(BaseModel):
    """A signed `CloseRecord` on the wire — `POST /sign`'s response (the D3 snapshot).

    The durable, self-contained record echoed back after a period is signed. Its
    nested payloads (`checklist` / `transactions` / `tax` / `reconciliation` /
    `anomalies` / `summary`) are typed `object` maps carrying the snapshot verbatim;
    the sign path builds them **pre-stringified** (money as exact-`Decimal` strings,
    datetimes ISO 8601, sequences as lists) so the returned record is byte-equal to
    the one the store reads back.

    **Money discipline (why a pydantic model, not a raw dict).** A raw-dict route
    response goes through FastAPI's `jsonable_encoder`, which coerces a `Decimal` to
    a lossy JSON **float** — the exact money bug the slice forbids. Serializing
    through this model instead renders any `Decimal` value inside the `object` maps
    as its exact **string**, so even a payload that slipped a raw `Decimal` past the
    sign path's stringification still lands on the wire as an exact string, never a
    number (the refinement-#3 belt-and-suspenders).
    """

    period: str
    signed_at: str = Field(description="ISO 8601 sign-off time (UTC).")
    signed_by: str
    checklist: list[dict[str, object]]
    transactions: list[dict[str, object]]
    tax: dict[str, object]
    reconciliation: dict[str, object]
    anomalies: list[dict[str, object]]
    summary: dict[str, object]
    effective_prior_period_state: str | None = None
    config_prior_period_state: str | None = None

    @classmethod
    def from_record(cls, record: "CloseRecord") -> "CloseRecordOut":
        return cls(**_close_record_out(record))


class SignRequest(BaseModel):
    """A §5.7 human sign-off request: which period, and (optionally) who signed.

    `period` is client-supplied free text — the sign handler validates it up front
    (a well-formed quarterly ``YYYY-Qn`` label with ≥1 ledger transaction) before any
    composition, and re-verifies the whole close server-side (never trusting client
    state). `signed_by` defaults to ``"owner"`` (single-user local — a label, not an
    identity).
    """

    period: str
    signed_by: str | None = None


# --- Slice 3: the thin write shapes (issue C) --------------------------------
#
# The two Slice-3 write endpoints — acknowledge an anomaly flag (`POST
# /anomalies/review`) and waive a no-statement period (`POST /reconciliation/waive`)
# — that feed close review's gate B (anomalies-reviewed) and gate C
# (statement-or-waiver). Each request is a thin shape validated in `api.py` (machine
# 4xx); each response echoes the recorded store row verbatim so the caller can
# confirm the append. These write only their own store's row — no ledger /
# confirmation / statement / reconciliation touch (the app's separate-layer rule).


class AnomalyReviewRequest(BaseModel):
    """A human acknowledgment of one anomaly flag: which flag, which period, why.

    `flag_id` is the app-derived id (`anomaly_reviews.derive_flag_id`) of a **current**
    flag for `period` — the API re-runs `flag_anomaly` and rejects (422) an id that
    matches no current flag, so an ack never dangles against a flag that does not
    exist. `note` is the human's optional why.
    """

    flag_id: str
    period: str
    note: str | None = None


class AnomalyReviewOut(BaseModel):
    """A recorded `AnomalyReview` — echoed back so the surface can confirm the write.

    Carries the acknowledged flag's id plus the snapshot it was made against
    (`kind` / `reason` / `transaction_ids`, so the row is self-describing in the
    trail even if the underlying flag later changes), the human's `note`, the
    `acknowledged_at` audit timestamp, and `source` (`human`).
    """

    flag_id: str
    kind: str
    reason: str
    transaction_ids: list[str]
    note: str | None = None
    acknowledged_at: str = Field(description="ISO 8601 audit timestamp.")
    source: str

    @classmethod
    def from_model(cls, review: AnomalyReview) -> "AnomalyReviewOut":
        return cls(
            flag_id=review.flag_id,
            kind=review.kind,
            reason=review.reason,
            transaction_ids=list(review.transaction_ids),
            note=review.note,
            acknowledged_at=review.acknowledged_at.isoformat(),
            source=review.source,
        )


class WaiveRequest(BaseModel):
    """A human waiver of one period's reconciliation precondition: which period, why.

    `period` is the period waived — the API rejects (409) a period that already has a
    statement on file (a present statement is never waivable) or that is already
    closed. `waived_by` is the attribution (defaults to ``"owner"`` — single-user
    local, a label not an identity); `note` is the human's optional why.
    """

    period: str
    waived_by: str | None = None
    note: str | None = None


class WaiverOut(BaseModel):
    """A recorded `Waiver` — echoed back so the surface can confirm the write."""

    period: str
    waived_at: str = Field(description="ISO 8601 audit timestamp.")
    waived_by: str
    note: str | None = None

    @classmethod
    def from_model(cls, waiver: Waiver) -> "WaiverOut":
        return cls(
            period=waiver.period,
            waived_at=waiver.waived_at.isoformat(),
            waived_by=waiver.waived_by,
            note=waiver.note,
        )


# --- Slice 4 · A: the accountant-package preview surface (proposed | blocked) ---
#
# The wire shape of `generate_accountant_package`'s `AccountantPackage` — the
# read side of the accountant-package deliverable (`GET /package`). Money stays an
# exact `Decimal` string (never a JSON number), dates ISO 8601, and each entry
# carries `transaction_id` (= `transaction_key`) so the trust trail travels by
# reference back to the local ledger row — `artifact_bytes` never crosses the wire.
# The framework fields (`proposed_account` / `confidence` / `source`) come verbatim
# from the categorization; the confirmation overlay (`confirmed_account` /
# `confirmed_at` / `diverges`) is **additive** — `views.build_package` joins each
# entry to its latest confirmation and never rewrites a framework field.


class PackageEntryOut(BaseModel):
    """One costed / categorized / taxed package line + the app's confirmation overlay.

    `proposed_account` / `confidence` / `source` are the framework `CategoryProposal`
    verbatim (`owner-rule` / `chart-match`, or the app's `human` convention for a
    Slice-3 human-confirmed flag). The overlay — `confirmed_account` / `confirmed_at`
    / `diverges` — is the app's latest confirm/correct decision joined on
    `transaction.id`, **additive only** (the framework fields are byte-identical with
    or without a confirmation present). The raw `human`-source values (`source`,
    `confidence=1.0`) stay honest on the wire; the "human-confirmed" label +
    confidence suppression is a UI-render concern (a later issue), not this JSON.
    """

    transaction: TransactionOut
    proposed_account: str
    confidence: float
    source: str = Field(description="Which rule fired: 'owner-rule' / 'chart-match' / 'human'.")
    attribution_target_id: str
    tax: str = Field(description="Exact Decimal as a string (never a lossy float).")
    confirmed_account: str | None = Field(
        default=None, description="The human's latest confirm/correct account, or null."
    )
    confirmed_at: str | None = Field(default=None, description="ISO 8601, or null.")
    diverges: bool = Field(
        description="True iff a confirmation exists and its account differs from proposed_account.",
    )


class PackageMatchedPairOut(BaseModel):
    """A narrowed package-surface matched pair — the txn id + its statement side.

    Deliberately narrower than `MatchedPairOut` (which carries both full sides): the
    package trail links by reference, so it carries `transaction_id` (= the ledger
    `transaction_key`) rather than the whole transaction, plus the statement line's
    own fields. `StatementLine` has **no vendor** — the only description available is
    the statement line's `description`.
    """

    transaction_id: str = Field(description="The ledger transaction_key of the matched txn.")
    statement_ref: str
    date: str = Field(description="ISO 8601 (the statement line's date).")
    amount: str = Field(description="Exact Decimal as a string (the statement line's amount).")
    statement_description: str

    @classmethod
    def from_model(cls, pair: MatchedPair) -> "PackageMatchedPairOut":
        return cls(
            transaction_id=transaction_key(pair.transaction),
            statement_ref=pair.statement_line.statement_ref,
            date=pair.statement_line.date.isoformat(),
            amount=str(pair.statement_line.amount),
            statement_description=pair.statement_line.description,
        )


class ReconciliationOut(BaseModel):
    """The package's reconciliation trail — matched pairs + honest open counts.

    The package-surface projection of a `ReconciliationReport`: the `matched` pairs
    (narrowed to `PackageMatchedPairOut`) plus `to_confirm_count` / `gap_count`. On a
    PROPOSED package both counts are 0 by construction (a READY close carries no open
    reconcile item) — serialized anyway (honesty over assumption).
    """

    matched: list[PackageMatchedPairOut]
    to_confirm_count: int
    gap_count: int

    @classmethod
    def from_model(cls, report: ReconciliationReport) -> "ReconciliationOut":
        return cls(
            matched=[PackageMatchedPairOut.from_model(m) for m in report.matched],
            to_confirm_count=len(report.to_confirm),
            gap_count=len(report.gaps),
        )


class PackageSummaryOut(BaseModel):
    """The package's disposition counts — mirrors the framework `PackageSummary`.

    On a PROPOSED package (built from a READY close) `open == 0` and
    `processed == auto_filed + reviewed` by construction.
    """

    processed: int
    auto_filed: int
    reviewed: int
    open: int

    @classmethod
    def from_model(cls, summary: PackageSummary) -> "PackageSummaryOut":
        return cls(
            processed=summary.processed,
            auto_filed=summary.auto_filed,
            reviewed=summary.reviewed,
            open=summary.open,
        )


class PackageOut(BaseModel):
    """The accountant-package preview on the wire — proposed (assembled) or blocked.

    A PROPOSED package (the effective close was READY) carries the full trail:
    `summary`, the costed/categorized/taxed `entries` (in the categorization report's
    order), the `tax_breakout` (per target + period, exact-string money), and the
    `reconciliation` result. A BLOCKED package (the effective close was not READY, or
    the period is already closed) sets `summary` / `tax_breakout` / `reconciliation`
    to null, `entries` to `[]`, `divergence_count` to 0, and names why in
    `unmet_close`. `divergence_count` is the number of entries a human corrected
    away from the proposed account.
    """

    period: str
    status: Literal["proposed", "blocked"] = Field(
        description="The PackageStatus value — 'proposed' (assembled) or 'blocked' (refused)."
    )
    accounting_method: str
    jurisdiction: str
    summary: PackageSummaryOut | None = None
    entries: list[PackageEntryOut] = Field(default_factory=list)
    tax_breakout: TaxSummaryOut | None = None
    reconciliation: ReconciliationOut | None = None
    unmet_close: str | None = None
    divergence_count: int = 0


# --- Slice 4 · B: the export write-path shapes -------------------------------
#
# The wire shapes for the export write path: `POST /export`'s result and the
# `GET /exports` log-row projection. Both echo an `ExportRecord` (the append-only
# log row) verbatim — same shape, one per surface — so a caller can confirm the
# write and list the trail. Money never appears here (only ids, hashes, counts, and
# ISO timestamps), so there is no float risk on this boundary.


class ExportFileOut(BaseModel):
    """One exported Core file's fingerprint — its name, sha256, and byte count."""

    name: str
    sha256: str = Field(description="sha256 hex digest of the file's exact bytes.")
    bytes: int


class ExportResultOut(BaseModel):
    """`POST /export`'s response — the export just written, echoed for confirmation.

    Carries the `export_id`, `period`, `package_status` (`proposed`), `exported_at`
    (ISO 8601, UTC), the per-file fingerprints of the three hashed Core files, and
    the package's `divergence_count`.
    """

    export_id: str
    period: str
    package_status: str
    exported_at: str = Field(description="ISO 8601 export time (UTC).")
    files: list[ExportFileOut]
    divergence_count: int

    @classmethod
    def from_record(cls, record: "ExportRecord") -> "ExportResultOut":
        return cls(**_export_record_fields(record))


class ExportRecordOut(BaseModel):
    """A `GET /exports` log-row — the same shape as `ExportResultOut`, per trail row.

    The append-only export log projected to the wire, in export (insertion) order.
    """

    export_id: str
    period: str
    package_status: str
    exported_at: str = Field(description="ISO 8601 export time (UTC).")
    files: list[ExportFileOut]
    divergence_count: int

    @classmethod
    def from_record(cls, record: "ExportRecord") -> "ExportRecordOut":
        return cls(**_export_record_fields(record))


def _export_record_fields(record: "ExportRecord") -> dict[str, object]:
    """The shared field mapping both export wire shapes project from an `ExportRecord`."""
    return {
        "export_id": record.export_id,
        "period": record.period,
        "package_status": record.package_status,
        "exported_at": record.exported_at.isoformat(),
        "files": [
            ExportFileOut(
                name=str(f["name"]),
                sha256=str(f["sha256"]),
                bytes=int(f["bytes"]),  # type: ignore[call-overload]
            )
            for f in record.files
        ],
        "divergence_count": record.divergence_count,
    }


# --- Slice 5 · A: the intake port (candidate ingest + the shared queue) -------
#
# A candidate is a proposal that can never touch the ledger (only a human confirm
# constructs a `Transaction`). These are the wire shapes for the JSON intake API
# (`POST /intake/candidates`, `GET /intake/candidates`, `POST /intake/resolve`) and
# the shared `build_intake_queue` projection both JSON and (later) HTML read — so a
# candidate's standing is computed once, not re-derived per surface. Money is exact
# strings, mirroring the rest of this boundary.

# Where a candidate stands: `pending` (no human decision yet), `confirmed` (a human
# confirmed it into the ledger), or `rejected`. Overlaid by `build_intake_queue`.
IntakeStanding = Literal["pending", "confirmed", "rejected"]


def standing_for_action(action: str) -> IntakeStanding:
    """The candidate standing a decided action resolves to — the one action→standing map.

    A `confirm` stands `confirmed`, any other decided action (`reject`) stands
    `rejected`. The queue projection (`CandidateEntryOut.build`) and the `/intake/resolve`
    response both derive the echoed standing from **this**, so the two can never drift to
    disagree on what a decision means.
    """
    return "confirmed" if action == ACTION_CONFIRM else "rejected"


class CandidateOut(BaseModel):
    """A submitted candidate on the wire — money as exact strings, dates ISO 8601.

    Carries the extracted fields (no category, by design — category is a downstream
    skill), the optional `attribution_target_id` (null when the extractor didn't
    resolve one; the human assigns it at confirm), and the `artifact_sha256` / media
    type that link the row to its raw source bytes (fetched via `GET /intake/artifact`).
    The raw base64 artifact is **never** echoed here — it is served on its own route.
    """

    candidate_id: str = Field(description="sha256(source + submission_id) — the stable id.")
    source: str
    submission_id: str
    vendor: str
    amount: str = Field(description="Exact Decimal as a string (never a lossy float).")
    tax: str
    date: str = Field(description="ISO 8601.")
    description: str
    attribution_target_id: str | None = None
    source_hint: str
    received_at: str | None = Field(default=None, description="ISO 8601, or null.")
    artifact_media_type: str
    artifact_sha256: str
    submitted_at: str = Field(description="ISO 8601 server receive time (UTC).")

    @classmethod
    def from_submission(cls, submission: CandidateSubmission) -> "CandidateOut":
        return cls(
            candidate_id=submission.candidate_id,
            source=submission.source,
            submission_id=submission.submission_id,
            vendor=submission.vendor,
            amount=str(submission.amount),
            tax=str(submission.tax),
            date=submission.date.isoformat(),
            description=submission.description,
            attribution_target_id=submission.attribution_target_id,
            source_hint=submission.source_hint,
            received_at=(
                submission.received_at.isoformat()
                if submission.received_at is not None
                else None
            ),
            artifact_media_type=submission.artifact_media_type,
            artifact_sha256=submission.artifact_sha256,
            submitted_at=submission.submitted_at.isoformat(),
        )


class CandidateSubmitOut(BaseModel):
    """The outcome of `POST /intake/candidates` — the stored candidate + a dupe flag.

    `duplicate` is `True` when the `(source, submission_id)` was already on record
    (the idempotent no-op: the route returns 200 with the **existing** candidate,
    unchanged, and writes nothing); `False` on a first write (201).
    """

    duplicate: bool
    candidate: CandidateOut


class CandidateSubmitRequest(BaseModel):
    """A candidate document POSTed by an extractor (money as JSON **strings**).

    `amount` / `tax` are JSON strings parsing to a finite `Decimal` — a JSON *number*
    is a 422 (never re-introduce the float bug on this new path). `artifact` is the
    base64 of the source bytes; `artifact_media_type` is validated against the
    allowlist in the handler. Optional fields absent → their documented defaults
    (`tax` → `"0"`, `description`/`source_hint` → `""`, the rest → null).
    """

    source: str
    submission_id: str
    vendor: str
    amount: str
    tax: str | None = None
    date: str
    description: str | None = None
    attribution_target_id: str | None = None
    source_hint: str | None = None
    received_at: str | None = None
    artifact: str = Field(description="base64 of the raw source artifact bytes.")
    artifact_media_type: str


class ResolveCandidateRequest(BaseModel):
    """A human confirm/reject decision on a candidate.

    `action` is `confirm` or `reject`. On a **confirm**, the optional field values
    are the human's final edits — each absent field falls back to the candidate's own
    submitted value, and every effective value is re-validated through the same gate
    the submission passed (finite-Decimal money as strings, ISO date, non-blank
    vendor; `attribution_target_id` required and in `config.attribution_targets`). On
    a **reject**, only `reject_reason` is used; the ledger is untouched.
    """

    candidate_id: str
    action: Literal["confirm", "reject"]
    # confirm-only edited final values (absent → fall back to the candidate's value)
    vendor: str | None = None
    amount: str | None = None
    tax: str | None = None
    date: str | None = None
    description: str | None = None
    attribution_target_id: str | None = None
    # reject-only
    reject_reason: str | None = None


class CandidateResolutionOut(BaseModel):
    """The recorded outcome of `POST /intake/resolve` — echoed so the caller sees it.

    On a confirm, `ledger_outcome` is the honest-dedupe signal (`stored` when a new
    ledger row was written, `already-present` when the confirmed business fields
    matched an already-filed transaction — a **visible** no-op, never silent) and
    `transaction_key` is the durable link to the ledger row. `message` states the
    outcome in words. On a reject, `reject_reason` carries the note.
    """

    candidate_id: str
    action: str
    standing: IntakeStanding
    ledger_outcome: str | None = None
    transaction_key: str | None = None
    reject_reason: str | None = None
    decided_at: str = Field(description="ISO 8601 audit timestamp (UTC).")
    message: str


class CandidateEntryOut(BaseModel):
    """One candidate in the intake queue, with its current standing.

    The shared `build_intake_queue` projection overlays the decision trail on the
    submissions: `standing` is `pending` / `confirmed` / `rejected`, and the decision
    fields (`action`, `ledger_outcome`, `transaction_key`, `reject_reason`,
    `decided_at`) carry the resolution when the candidate is decided (all null while
    pending). Both the JSON list route and the later UI queue read this one shape.
    """

    candidate: CandidateOut
    standing: IntakeStanding
    action: str | None = None
    ledger_outcome: str | None = None
    transaction_key: str | None = None
    reject_reason: str | None = None
    decided_at: str | None = None

    @classmethod
    def build(
        cls,
        submission: CandidateSubmission,
        decision: CandidateDecision | None,
    ) -> "CandidateEntryOut":
        """Project a submission + its latest decision (or none) into a queue entry."""
        if decision is None:
            return cls(candidate=CandidateOut.from_submission(submission), standing="pending")
        standing = standing_for_action(decision.action)
        return cls(
            candidate=CandidateOut.from_submission(submission),
            standing=standing,
            action=decision.action,
            ledger_outcome=decision.ledger_outcome,
            transaction_key=decision.transaction_key,
            reject_reason=decision.reject_reason,
            decided_at=decision.decided_at.isoformat(),
        )


class IntakeQueueOut(BaseModel):
    """The intake queue for the JSON list route — every candidate with its standing.

    `status` echoes the applied filter (null = all statuses). `candidates` is the
    shared `build_intake_queue` projection in submission (insertion) order, filtered
    to `status` when one is given.
    """

    status: IntakeStanding | None = None
    candidates: list[CandidateEntryOut]


class ScanFileErrorOut(BaseModel):
    """One per-file failure in a drop-dir scan — the file name and why it was skipped.

    A malformed drop file is reported here (never a partial write, never an aborted
    scan): the valid files alongside it still ingest (Slice 5 · A3, AC 11).
    """

    file: str
    error: str


class ScanResultOut(BaseModel):
    """The outcome of `POST /intake/scan` — the drop-dir ingest tally + per-file errors.

    `scanned` is every `*.json` file seen; `ingested` the new candidate rows written
    this scan; `duplicates` the files whose `(source, submission_id)` was already stored
    (the store's idempotent no-op made visible — a second scan over the unchanged dir is
    all `duplicates`, `ingested: 0`); `errors` the per-file failures. Money on the
    ingested rows stays an exact `Decimal` string end to end — this surface reports only
    counts, so no money crosses it.
    """

    scanned: int
    ingested: int
    duplicates: int
    errors: list[ScanFileErrorOut]
