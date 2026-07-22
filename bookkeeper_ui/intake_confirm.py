"""Slice 5 — the one confirm-apply write core shared by the JSON and HTML resolve twins.

`POST /intake/resolve` (`api.py`) and `POST /ui/intake/resolve` (`web.py`) both apply a
human **confirm**: refuse a date edited into a signed-closed period (the C1 guard), fetch
the source artifact bytes, construct the ledger `Transaction`, file it with honest dedupe
(probe `contains()` **before** the idempotent `store()`), and append the decision row (the
durable candidate↔ledger link). Each surface *validates* the human's edited fields its own
way — the JSON twin raises a machine 422, the HTML twin re-renders the card — so field
validation stays in each handler. But from the C1 guard onward the write path is
byte-identical, and its two load-bearing invariants (the C1 refusal reading the **edited**
date, and the probe-before-store dedupe ordering) must never silently drift between the
twins. That shared core lives here; each handler catches the two typed refusals below and
surfaces them in its own idiom.

This module imports only leaf modules (`candidates` / `closes` / `ledger_store` /
`periods`) and the framework — never `api.py` or `web.py` — so both handlers can import it
with no cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bookkeeper.model import Transaction

from bookkeeper_ui.candidates import (
    ACTION_CONFIRM,
    SOURCE_HUMAN,
    LEDGER_OUTCOME_ALREADY_PRESENT,
    LEDGER_OUTCOME_STORED,
    CandidateDecision,
    FileArtifactStore,
    FileCandidateDecisionStore,
)
from bookkeeper_ui.closes import FileCloseStore, closed_periods
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.periods import period_of


class ConfirmClosedPeriodError(Exception):
    """The confirmed (edited) date falls in a signed-closed period — refuse the write.

    Raised **before** any read or write, so a refused confirm files nothing and records
    no decision row. Carries the offending `period` so each surface can name it (the JSON
    twin a 409, the HTML twin the `_closed_refusal.html` card). The guard reads the
    **edited** date (never `transaction_in_closed_period`, which probes an existing ledger
    row and would silently pass — the transaction is not filed yet).
    """

    def __init__(self, period: str) -> None:
        self.period = period
        super().__init__(period)


class ConfirmArtifactMissingError(Exception):
    """The candidate's source artifact is gone at confirm — a hard error, never empty.

    A confirm files the ledger row that carries the receipt bytes (the charter-§1
    source-trace link). Coalescing a lost blob to `b""` would file a row with **no**
    artifact, and `transaction_key` excludes `artifact_bytes` so the loss would be
    undetectable downstream. Refuse instead (the JSON twin 404s, mirroring the
    artifact-serve route; the HTML twin renders the failure into the card).
    """

    def __init__(self, candidate_id: str) -> None:
        self.candidate_id = candidate_id
        super().__init__(candidate_id)


@dataclass(frozen=True)
class ConfirmOutcome:
    """The result of a successful confirm — the ledger link + the honest-dedupe signal."""

    transaction_key: str
    ledger_outcome: str


async def apply_confirm(
    *,
    candidate_id: str,
    vendor: str,
    amount: Decimal,
    tax: Decimal,
    date: datetime,
    description: str,
    attribution_target_id: str,
    now: datetime,
    ledger_store: FileLedgerStore,
    artifact_store: FileArtifactStore,
    decision_store: FileCandidateDecisionStore,
    close_store: FileCloseStore | None,
) -> ConfirmOutcome:
    """Apply a validated confirm: C1 guard → artifact → file (honest dedupe) → record.

    The caller passes the **effective** field values (the human's edits, else the
    candidate's own values), already re-validated through its own gate, plus the
    already-checked `attribution_target_id`. This core then, in fixed order:

    1. refuses a date edited into a closed period (`ConfirmClosedPeriodError`) — before
       any read or write, so nothing is filed and no decision row is written;
    2. fetches the source artifact bytes, refusing a missing blob
       (`ConfirmArtifactMissingError`) rather than coalescing to `b""`;
    3. constructs the framework `Transaction` (the receipt bytes ride `artifact_bytes`);
    4. honest dedupe — probes `contains()` **before** the idempotent, silent `store()`,
       so a duplicate confirm is a **visible** no-op (`already-present`), never a silent
       lost filing;
    5. appends the confirm decision row (the durable candidate↔ledger link).

    Returns the ledger `transaction_key` and the `ledger_outcome`.
    """
    # 1. C1 closed-period guard — on the human-EDITED date being written.
    if period_of(date) in await closed_periods(close_store):
        raise ConfirmClosedPeriodError(period_of(date))

    # 2. The source artifact must exist — a lost blob is a hard error, never empty bytes.
    artifact_bytes = await artifact_store.get(candidate_id)
    if not artifact_bytes:
        raise ConfirmArtifactMissingError(candidate_id)

    # 3. Construct the ledger Transaction (the receipt bytes ride artifact_bytes).
    transaction = Transaction(
        attribution_target_id=attribution_target_id,
        vendor=vendor,
        amount=amount,
        tax=tax,
        date=date,
        description=description,
        artifact_bytes=artifact_bytes,
    )
    key = transaction_key(transaction)

    # 4. Honest dedupe — probe contains() BEFORE the idempotent, silent store().
    already = await ledger_store.contains(key)
    await ledger_store.store(transaction)
    outcome = LEDGER_OUTCOME_ALREADY_PRESENT if already else LEDGER_OUTCOME_STORED

    # 5. Append the decision row — the durable candidate↔ledger link + dedupe signal.
    await decision_store.record(
        CandidateDecision(
            candidate_id=candidate_id,
            action=ACTION_CONFIRM,
            source=SOURCE_HUMAN,
            decided_at=now,
            vendor=vendor,
            amount=amount,
            tax=tax,
            date=date,
            description=description,
            attribution_target_id=attribution_target_id,
            transaction_key=key,
            ledger_outcome=outcome,
        )
    )
    return ConfirmOutcome(transaction_key=key, ledger_outcome=outcome)
