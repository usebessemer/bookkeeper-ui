"""The anomaly-review store — the human acknowledgment layer over `flag_anomaly`.

`flag_anomaly` (the framework skill) is advisory: it surfaces mechanical anomalies
(duplicates, over-materiality, malformed records) and **writes nothing, gates
nothing**. This store holds the human *acknowledgments* layered on top — a note
that a flag has been seen and dispositioned — kept deliberately separate from the
ledger it flags (a review never edits a transaction).

**The app derives the flag id.** Framework flags carry no id (`AnomalyFlag` is
`kind` + `reason` + `transactions`), so the app derives a deterministic one from
the flag's own content — the load-bearing detail:

    sha256("{kind.value}|{sorted transaction_keys joined by |}|{reason}")

Deterministic across runs because the framework's kinds, reasons, and ledger
ordering are deterministic. Including `reason` is deliberate and fail-safe: a
*changed* flag derives a **new, unacknowledged** id, so config drift never
inherits a stale acknowledgment. Concretely — only the `over_materiality` reason
embeds the floor value (`flag_anomaly.py`), so only that kind's id changes when
`materiality_floor` changes; `duplicate` / `malformed` ids are floor-invariant. A
framework reason-wording change in a future version would likewise invalidate acks
(the accepted, documented cost of the fail-safe direction).

A review is **append-only** — a second acknowledgment of the same flag is a new
row, not an overwrite; `by_flag_id()` collapses the trail to the current
disposition per flag (last write wins), while `all()` keeps every row for audit.

Storage format: JSONL, one acknowledgment per line; `acknowledged_at` as ISO 8601.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bookkeeper.skills.flag_anomaly import AnomalyFlag

# The `source` recorded for an acknowledgment made through the app's review path —
# the same named discipline as the other stores: a disposition came from a
# **human**, not the skill (which only surfaces).
from bookkeeper_ui.confirmations import SOURCE_HUMAN
from bookkeeper_ui.ledger_store import transaction_key


def derive_flag_id(flag: AnomalyFlag) -> str:
    """The deterministic app-derived id for a framework anomaly flag.

    ``sha256("{kind.value}|{sorted transaction_keys joined by |}|{reason}")``,
    hexdigest. `kind.value` is the `AnomalyKind` string (``"duplicate"`` /
    ``"over_materiality"`` / ``"malformed"``); the transaction keys are each
    member's `ledger_store.transaction_key`, sorted so member order never changes
    the id. `reason` is included so a reworded flag (e.g. an over-materiality
    reason re-worded by a floor change) derives a **new** id — a stale
    acknowledgment is never silently inherited.
    """
    keys = sorted(transaction_key(t) for t in flag.transactions)
    canonical = f"{flag.kind.value}|{'|'.join(keys)}|{flag.reason}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AnomalyReview:
    """One human acknowledgment of an anomaly flag.

    `flag_id` is the app-derived id (`derive_flag_id`) the acknowledgment resolves;
    `kind` / `reason` / `transaction_ids` snapshot the flag it was made against (so
    the row is self-describing in the trail even if the underlying flag later
    changes); `note` is the human's optional why; `acknowledged_at` is when;
    `source` records who (`human`).
    """

    flag_id: str
    kind: str
    reason: str
    transaction_ids: tuple[str, ...]
    note: str | None
    acknowledged_at: datetime
    source: str

    def __post_init__(self) -> None:
        # Freeze the member ids to a tuple, so a read-back (JSON list) compares
        # equal to the constructed review.
        object.__setattr__(self, "transaction_ids", tuple(self.transaction_ids))


def _to_record(review: AnomalyReview) -> dict[str, object]:
    return {
        "flag_id": review.flag_id,
        "kind": review.kind,
        "reason": review.reason,
        "transaction_ids": list(review.transaction_ids),
        "note": review.note,
        "acknowledged_at": review.acknowledged_at.isoformat(),
        "source": review.source,
    }


def _from_record(record: dict[str, object]) -> AnomalyReview:
    raw_note = record.get("note")
    return AnomalyReview(
        flag_id=str(record["flag_id"]),
        kind=str(record["kind"]),
        reason=str(record["reason"]),
        transaction_ids=tuple(str(t) for t in (record.get("transaction_ids") or [])),
        note=None if raw_note is None else str(raw_note),
        acknowledged_at=datetime.fromisoformat(str(record["acknowledged_at"])),
        source=str(record["source"]),
    )


class FileAnomalyReviewStore:
    """A JSONL-backed, append-only store of anomaly acknowledgments.

    Construct with the path to the reviews file (created on first write, parents
    included). A distinct file from the ledger — the review layer is kept separate
    from the transactions it flags, and it is advisory only (it gates nothing).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    async def record(self, review: AnomalyReview) -> None:
        """Append one acknowledgment to the trail (append-only; a re-ack is a new row)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_record(review)) + "\n")

    async def all(self) -> list[AnomalyReview]:
        """Every recorded acknowledgment, in insertion order — the full trail."""
        results: list[AnomalyReview] = []
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                results.append(_from_record(json.loads(line)))
        return results

    async def by_flag_id(self) -> dict[str, AnomalyReview]:
        """The current disposition per flag id (last write wins).

        Collapses the append-only trail by `flag_id`: a re-acknowledgment recorded
        after an earlier one replaces it here, while `all()` still holds both for
        audit.
        """
        latest: dict[str, AnomalyReview] = {}
        for review in await self.all():
            latest[review.flag_id] = review
        return latest


__all__ = [
    "SOURCE_HUMAN",
    "AnomalyReview",
    "FileAnomalyReviewStore",
    "derive_flag_id",
]
