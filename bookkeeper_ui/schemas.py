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

from bookkeeper.model import Transaction
from bookkeeper.skills.categorize import (
    CategorizationReport,
    CategoryFlag,
    CategoryProposal,
)

from bookkeeper_ui.confirmations import Confirmation
from bookkeeper_ui.ledger_store import transaction_key

# Where a ledger entry stands: a human-`confirmed` account, an agent `proposed`
# one awaiting confirm/correct, or `flagged` for a human to categorize from
# scratch. Drives which of the trust-trail fields below are populated.
LedgerStatus = Literal["confirmed", "proposed", "flagged"]


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
    """

    transaction: TransactionOut
    status: LedgerStatus
    account: str | None = None
    confidence: float | None = None
    source: str | None = None
    reason: str | None = None


class LedgerOut(BaseModel):
    """The categorized ledger for a period — every transaction, in read order."""

    period: str
    entries: list[LedgerEntryOut]


class ImportResultOut(BaseModel):
    """The outcome of `POST /import` — how many transactions were persisted."""

    imported: int
    transactions: list[TransactionOut]
