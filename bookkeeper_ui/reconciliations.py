"""The reconciliation-resolution store — the human confirm/reject/acknowledge layer.

The reconcile analog of `confirmations.py`. `reconcile_account` is detection-only
(it writes nothing, §5.5); this store holds the human *resolutions* layered on top
of each fresh report: a divergent-vendor pair confirmed or rejected, a gap
acknowledged. Kept deliberately separate from `ledger.jsonl` and `statements.jsonl`
— a resolution **never** adjusts a ledger entry or edits a statement line (§5.5:
a discrepancy is surfaced, never auto-fixed). If the human actually fixes the
books, the *next* reconcile run reflects it and the stale resolution simply stops
mapping to a report item (a harmless orphan in the trail).

A resolution is **append-only** — every decision is recorded in order as an audit
trail (charter §1: traceable). A *correction* of an earlier decision is a new row,
not an overwrite; `latest_by_item()` collapses the trail to the current decision
per item (last write wins), while `all()` keeps every row for audit.

**Overlay identity is the `(transaction_id, statement_line_id)` tuple** — either
side may be null (a one-sided gap carries only its one side), but never both. No
opaque derived id is stored: the two component ids *are* the audit record, and are
what `views.build_reconciliation` overlays each fresh report by. `transaction_id`
is the ledger `transaction_key`; `statement_line_id` is the `statement_line_key`.

Storage format: JSONL, one decision per line; `decided_at` as ISO 8601.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The `source` recorded for a decision made through the app's reconcile queue.
# The same named discipline as the confirmation store: these dispositions came
# from a **human**, not the skill (which only surfaces — it never resolves).
from bookkeeper_ui.confirmations import SOURCE_HUMAN

# The three human dispositions the reconcile queue records, one per report-item
# kind (see the issue's decision table):
#   - a `to_confirm` pair is confirmed (accept the link) or rejected (not the
#     same charge);
#   - any gap kind is acknowledged (seen it, recorded a disposition).
DECISION_CONFIRM = "confirm"
DECISION_REJECT = "reject"
DECISION_ACKNOWLEDGE = "acknowledge"

#: Every decision the resolve path accepts — anything else is a 422.
VALID_DECISIONS = frozenset({DECISION_CONFIRM, DECISION_REJECT, DECISION_ACKNOWLEDGE})

#: Pair decisions — they resolve a `to_confirm` *pair*, so both ids are required.
PAIR_DECISIONS = frozenset({DECISION_CONFIRM, DECISION_REJECT})

#: Decisions whose `note` (the human's why) must be present and non-blank. A bare
#: `confirm` (accept the surfaced link) needs no explanation; a `reject` or an
#: `acknowledge` records a disposition the trail must be able to justify.
NOTE_REQUIRED_DECISIONS = frozenset({DECISION_REJECT, DECISION_ACKNOWLEDGE})

#: The overlay key: `(transaction_id, statement_line_id)`, either side nullable.
ItemKey = tuple[str | None, str | None]


@dataclass(frozen=True)
class Reconciliation:
    """One human resolution of a reconcile report item.

    `transaction_id` (the ledger `transaction_key`) and `statement_line_id` (the
    `statement_line_key`) are the item this resolves — **at least one non-null**: a
    pair resolution carries both, a one-sided gap carries its one side, an
    `amount_mismatch` carries both. `decision` is one of `VALID_DECISIONS`; `note`
    is the human's why (required for `reject` / `acknowledge`, optional for
    `confirm`). `source` records who decided (`human`); `decided_at` is when — the
    audit timestamp.
    """

    transaction_id: str | None
    statement_line_id: str | None
    decision: str
    note: str
    source: str
    decided_at: datetime

    @property
    def item_key(self) -> ItemKey:
        """The `(transaction_id, statement_line_id)` overlay identity."""
        return (self.transaction_id, self.statement_line_id)


def _to_record(reconciliation: Reconciliation) -> dict[str, object]:
    return {
        "transaction_id": reconciliation.transaction_id,
        "statement_line_id": reconciliation.statement_line_id,
        "decision": reconciliation.decision,
        "note": reconciliation.note,
        "source": reconciliation.source,
        "decided_at": reconciliation.decided_at.isoformat(),
    }


def _opt_str(value: object) -> str | None:
    """A stored id as a `str`, or `None` when the row carried a null id.

    Distinct from `str(value)` on purpose: a one-sided resolution stores `null`
    for its absent side, and `str(None)` would silently turn that into the string
    ``"None"`` — a key that matches nothing and corrupts the overlay.
    """
    return None if value is None else str(value)


def _from_record(record: dict[str, object]) -> Reconciliation:
    return Reconciliation(
        transaction_id=_opt_str(record.get("transaction_id")),
        statement_line_id=_opt_str(record.get("statement_line_id")),
        decision=str(record["decision"]),
        note=str(record.get("note", "")),
        source=str(record["source"]),
        decided_at=datetime.fromisoformat(str(record["decided_at"])),
    )


class FileReconciliationStore:
    """A JSONL-backed, append-only store of reconcile resolutions.

    Construct with the path to the reconciliations file (created on first write,
    parents included). A distinct file from the ledger, the statements, and the
    confirmations — the reconcile-resolution layer is kept separate from every
    source it resolves, and it is the *only* write path any reconcile surface has.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    async def record(self, reconciliation: Reconciliation) -> None:
        """Append one resolution to the audit trail."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_record(reconciliation)) + "\n")

    async def all(self) -> list[Reconciliation]:
        """Every recorded resolution, in decision (insertion) order — the full trail."""
        results: list[Reconciliation] = []
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                results.append(_from_record(json.loads(line)))
        return results

    async def latest_by_item(self) -> dict[ItemKey, Reconciliation]:
        """The current resolution per item (last write wins).

        Collapses the append-only trail by the `(transaction_id, statement_line_id)`
        overlay identity: a correction recorded after an earlier decision on the
        same item replaces it here, while `all()` still holds both for audit.
        """
        latest: dict[ItemKey, Reconciliation] = {}
        for reconciliation in await self.all():
            latest[reconciliation.item_key] = reconciliation
        return latest


__all__ = [
    "SOURCE_HUMAN",
    "DECISION_CONFIRM",
    "DECISION_REJECT",
    "DECISION_ACKNOWLEDGE",
    "VALID_DECISIONS",
    "PAIR_DECISIONS",
    "NOTE_REQUIRED_DECISIONS",
    "ItemKey",
    "Reconciliation",
    "FileReconciliationStore",
]
