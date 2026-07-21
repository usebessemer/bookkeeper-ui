"""The intake port's stores — the receipt-capture feature's machine-facing half.

A **candidate** is a proposal that can never touch the ledger: an extractor POSTs
one (its extracted fields *plus its source artifact*), and only a human confirm
constructs a ledger `Transaction`. This module owns the three append-only stores
that hold that proposal side of the review boundary, kept deliberately apart from
the ledger the confirm step writes into:

- `FileCandidateStore` (`candidates.jsonl`) — one row per submission. **Idempotent
  on `candidate_id`** (the `IntakeSource`/`LedgerSink` retry discipline mirrored):
  re-submitting the same `(source, submission_id)` is a no-op, first write wins.
  Money is serialized as exact-`Decimal` strings, dates ISO 8601 — never a float.
- `FileArtifactStore` (`artifacts/<candidate_id>`) — the raw decoded artifact
  bytes as a file, kept **out** of the JSONL (a receipt JPEG is MBs; base64-in-line
  makes every whole-file read heavy). The `artifact_sha256` on the candidate row is
  the integrity / source-trace link (charter §1).
- `FileCandidateDecisionStore` (`candidate_decisions.jsonl`) — the human
  confirm/reject decisions, append-only. On a confirm the row carries the **final
  confirmed field values** plus the resulting ledger `transaction_key` and
  `ledger_outcome` (`stored` / `already-present`) — the durable candidate↔ledger
  link, and the honest-dedupe signal (a dedupe no-op is `already-present`, never
  silently counted as a fresh filing).

The candidate document maps 1:1 to the framework's `IntakeItem` at the data level
(artifact bytes, source hint, received-at, a stable id); this app is **push**
(extractors POST) where the framework's `IntakeSource` is pull, so the port aligns
at the data level, it does not implement the ABC.

Storage format for both JSONL stores: one JSON object per line; datetimes ISO
8601; **money everywhere as exact-`Decimal` strings, never a JSON number** (mirror
`ledger_store._money` / `_from_record`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# The decision actions and their `source` — a candidate is resolved by a **human**
# (the whole point of the review boundary: a machine submits, a human decides).
ACTION_CONFIRM = "confirm"
ACTION_REJECT = "reject"
VALID_ACTIONS = (ACTION_CONFIRM, ACTION_REJECT)
SOURCE_HUMAN = "human"

# The two honest-dedupe outcomes a confirm records against the ledger. `store()`
# is idempotent and silent on a duplicate key, so the confirm handler probes
# `ledger_store.contains(...)` *before* storing and records which happened —
# `already-present` makes a dedupe no-op **visible**, never a silent lost filing.
LEDGER_OUTCOME_STORED = "stored"
LEDGER_OUTCOME_ALREADY_PRESENT = "already-present"


def candidate_id(source: str, submission_id: str) -> str:
    """The stable, URL-safe candidate id — SHA-256 over `(source, submission_id)`.

    Candidate identity is `(source, submission_id)`: `source` namespaces the
    extractor's own stable `submission_id` for the artifact, so two extractors that
    happen to pick the same submission id never collide. Deterministic (a re-POST of
    the same pair maps to the same id → the idempotent no-op) and hex, so it doubles
    as the on-disk artifact filename safely.
    """
    canonical = source + "\n" + submission_id
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _money(value: Decimal) -> str:
    """Serialize exact currency as a string (a float would be lossy)."""
    return str(value)


@dataclass(frozen=True)
class CandidateSubmission:
    """One submitted candidate — an extracted transaction plus its source artifact.

    The extracted fields (`vendor` … `description`) carry no category by design
    (category is a downstream skill, not an extraction output). `attribution_target_id`
    is optional: an extractor that resolved attribution sends it, one that didn't
    sends `None` and the human assigns it at confirm. `artifact_sha256` links the row
    to the raw bytes held by `FileArtifactStore` (charter §1: traceable). Money is
    exact `Decimal`; a NULL / absent tax coalesces to `Decimal("0")` at the boundary.
    """

    candidate_id: str
    source: str
    submission_id: str
    vendor: str
    amount: Decimal
    tax: Decimal
    date: datetime
    description: str
    attribution_target_id: str | None
    source_hint: str
    received_at: datetime | None
    artifact_media_type: str
    artifact_sha256: str
    submitted_at: datetime


def _submission_to_record(submission: CandidateSubmission) -> dict[str, object]:
    """Flatten a `CandidateSubmission` to its JSONL row (exact money, ISO dates)."""
    return {
        "candidate_id": submission.candidate_id,
        "source": submission.source,
        "submission_id": submission.submission_id,
        "vendor": submission.vendor,
        "amount": _money(submission.amount),
        "tax": _money(submission.tax),
        "date": submission.date.isoformat(),
        "description": submission.description,
        "attribution_target_id": submission.attribution_target_id,
        "source_hint": submission.source_hint,
        "received_at": (
            submission.received_at.isoformat()
            if submission.received_at is not None
            else None
        ),
        "artifact_media_type": submission.artifact_media_type,
        "artifact_sha256": submission.artifact_sha256,
        "submitted_at": submission.submitted_at.isoformat(),
    }


def _submission_from_record(record: dict[str, object]) -> CandidateSubmission:
    """Reconstruct a `CandidateSubmission` from a JSONL row (exact Decimal money)."""
    raw_tax = record.get("tax")
    tax = Decimal("0") if raw_tax in (None, "") else Decimal(str(raw_tax))
    raw_received = record.get("received_at")
    raw_target = record.get("attribution_target_id")
    return CandidateSubmission(
        candidate_id=str(record["candidate_id"]),
        source=str(record["source"]),
        submission_id=str(record["submission_id"]),
        vendor=str(record["vendor"]),
        amount=Decimal(str(record["amount"])),
        tax=tax,
        date=datetime.fromisoformat(str(record["date"])),
        description=str(record.get("description", "")),
        attribution_target_id=None if raw_target is None else str(raw_target),
        source_hint=str(record.get("source_hint", "")),
        received_at=None if raw_received is None else datetime.fromisoformat(str(raw_received)),
        artifact_media_type=str(record["artifact_media_type"]),
        artifact_sha256=str(record["artifact_sha256"]),
        submitted_at=datetime.fromisoformat(str(record["submitted_at"])),
    )


class FileCandidateStore:
    """A JSONL-backed, append-only store of candidate submissions.

    Idempotent on `candidate_id` (the `FileLedgerStore` lazy-`_keys` pattern): a
    re-submission of the same `(source, submission_id)` is a no-op — first write
    wins, a differing re-POST never mutates the stored row. Construct with the path
    to `candidates.jsonl` (created on first write, parents included).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._keys: set[str] | None = None

    def _load_keys(self) -> set[str]:
        keys: set[str] = set()
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    keys.add(str(json.loads(line)["candidate_id"]))
        return keys

    async def add(self, submission: CandidateSubmission) -> None:
        """Append a candidate; idempotent on its `candidate_id` (a re-submit no-ops)."""
        if self._keys is None:
            self._keys = self._load_keys()
        if submission.candidate_id in self._keys:
            return  # already submitted — idempotent no-op (first write wins)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_submission_to_record(submission)) + "\n")
        self._keys.add(submission.candidate_id)

    async def get(self, candidate_id: str) -> CandidateSubmission | None:
        """The stored candidate for `candidate_id` (first write), or `None` if unknown."""
        if not self._path.exists():
            return None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if str(record["candidate_id"]) == candidate_id:
                return _submission_from_record(record)
        return None

    async def all(self) -> list[CandidateSubmission]:
        """Every candidate in submission (insertion) order, deduped first-write-wins."""
        results: list[CandidateSubmission] = []
        seen: set[str] = set()
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            cid = str(record["candidate_id"])
            if cid in seen:  # defensive: a hand-edited file can't yield dupes
                continue
            seen.add(cid)
            results.append(_submission_from_record(record))
        return results


class FileArtifactStore:
    """The raw source-artifact bytes on disk, one file per candidate.

    Kept out of the JSONL: an artifact is MBs, so base64-in-line would make every
    whole-file candidate read heavy. Written under `<dir>/<candidate_id>` (the id is
    a hex SHA-256, so a safe filename). Idempotent — a re-put for an existing
    candidate is a no-op (first write wins, mirroring the candidate store). Construct
    with the artifacts directory (created on first write).
    """

    def __init__(self, directory: str | Path):
        self._dir = Path(directory)

    def _path_for(self, candidate_id: str) -> Path:
        return self._dir / candidate_id

    async def put(self, candidate_id: str, data: bytes) -> None:
        """Persist a candidate's raw artifact bytes; idempotent (a re-put no-ops)."""
        path = self._path_for(candidate_id)
        if path.exists():
            return  # already stored — first write wins
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, candidate_id: str) -> bytes | None:
        """The stored artifact bytes for `candidate_id`, or `None` if none on disk."""
        path = self._path_for(candidate_id)
        if not path.exists():
            return None
        return path.read_bytes()


@dataclass(frozen=True)
class CandidateDecision:
    """One human confirm/reject decision on a candidate.

    Append-only audit: a decision is a new row, never an overwrite. On a **confirm**
    the row carries the final confirmed field values (the human's edits, or the
    candidate's own values where unedited) plus the resulting ledger `transaction_key`
    and `ledger_outcome` — the durable candidate↔ledger link and the honest-dedupe
    signal. On a **reject** it carries only the optional `reject_reason`; the ledger
    is untouched. The confirm-only fields default to `None` so one row shape holds
    both decisions.
    """

    candidate_id: str
    action: str
    source: str
    decided_at: datetime
    # confirm-only — the final confirmed field values + the ledger link
    vendor: str | None = None
    amount: Decimal | None = None
    tax: Decimal | None = None
    date: datetime | None = None
    description: str | None = None
    attribution_target_id: str | None = None
    transaction_key: str | None = None
    ledger_outcome: str | None = None
    # reject-only
    reject_reason: str | None = None


def _decision_to_record(decision: CandidateDecision) -> dict[str, object]:
    """Flatten a `CandidateDecision` to its JSONL row (exact money, ISO dates)."""
    return {
        "candidate_id": decision.candidate_id,
        "action": decision.action,
        "source": decision.source,
        "decided_at": decision.decided_at.isoformat(),
        "vendor": decision.vendor,
        "amount": None if decision.amount is None else _money(decision.amount),
        "tax": None if decision.tax is None else _money(decision.tax),
        "date": None if decision.date is None else decision.date.isoformat(),
        "description": decision.description,
        "attribution_target_id": decision.attribution_target_id,
        "transaction_key": decision.transaction_key,
        "ledger_outcome": decision.ledger_outcome,
        "reject_reason": decision.reject_reason,
    }


def _decision_from_record(record: dict[str, object]) -> CandidateDecision:
    """Reconstruct a `CandidateDecision` from a JSONL row (exact Decimal money)."""
    raw_amount = record.get("amount")
    raw_tax = record.get("tax")
    raw_date = record.get("date")
    return CandidateDecision(
        candidate_id=str(record["candidate_id"]),
        action=str(record["action"]),
        source=str(record.get("source", SOURCE_HUMAN)),
        decided_at=datetime.fromisoformat(str(record["decided_at"])),
        vendor=None if record.get("vendor") is None else str(record["vendor"]),
        amount=None if raw_amount is None else Decimal(str(raw_amount)),
        tax=None if raw_tax is None else Decimal(str(raw_tax)),
        date=None if raw_date is None else datetime.fromisoformat(str(raw_date)),
        description=None if record.get("description") is None else str(record["description"]),
        attribution_target_id=(
            None
            if record.get("attribution_target_id") is None
            else str(record["attribution_target_id"])
        ),
        transaction_key=(
            None if record.get("transaction_key") is None else str(record["transaction_key"])
        ),
        ledger_outcome=(
            None if record.get("ledger_outcome") is None else str(record["ledger_outcome"])
        ),
        reject_reason=(
            None if record.get("reject_reason") is None else str(record["reject_reason"])
        ),
    )


class FileCandidateDecisionStore:
    """A JSONL-backed, append-only store of candidate confirm/reject decisions.

    Distinct file from the candidates it decides — the resolution layer is kept
    apart from the submissions, exactly as `FileConfirmationStore` is kept apart from
    the ledger. Construct with the path to `candidate_decisions.jsonl` (created on
    first write, parents included).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    async def record(self, decision: CandidateDecision) -> None:
        """Append one confirm/reject decision to the audit trail."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_decision_to_record(decision)) + "\n")

    async def all(self) -> list[CandidateDecision]:
        """Every recorded decision, in decision (insertion) order — the full trail."""
        results: list[CandidateDecision] = []
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                results.append(_decision_from_record(json.loads(line)))
        return results

    async def latest_by_candidate(self) -> dict[str, CandidateDecision]:
        """The current decision per candidate id (last write wins).

        A candidate is decided terminally (the resolve handler refuses a second
        decision), so in practice there is one row per candidate; the collapse stays
        honest — the later-appended decision wins — if the trail ever holds two.
        """
        latest: dict[str, CandidateDecision] = {}
        for decision in await self.all():
            latest[decision.candidate_id] = decision
        return latest
