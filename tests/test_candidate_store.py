"""The intake stores: candidate id determinism, idempotency, round-trip, decisions.

Store-level coverage for `candidates.py` (Slice 5 · A) — the JSONL round-trip keeps
money exact (Decimal-as-string, never a float), the candidate store is idempotent on
`candidate_id` (first write wins), the artifact store round-trips raw bytes, and the
decision store collapses last-write-wins. Mirrors `test_ledger_store.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from bookkeeper_ui.candidates import (
    ACTION_CONFIRM,
    ACTION_REJECT,
    SOURCE_HUMAN,
    CandidateDecision,
    CandidateSubmission,
    FileArtifactStore,
    FileCandidateDecisionStore,
    FileCandidateStore,
    candidate_id,
)


def _submission(
    *,
    source: str = "acme-extractor",
    submission_id: str = "acme-0001",
    vendor: str = "Home Depot",
    amount: str = "82.50",
    tax: str = "10.73",
    date: datetime | None = None,
    description: str = "Lumber",
    attribution_target_id: str | None = "target-001",
    source_hint: str = "Receipt",
    received_at: datetime | None = None,
    media_type: str = "image/jpeg",
    sha256: str = "deadbeef",
) -> CandidateSubmission:
    cid = candidate_id(source, submission_id)
    return CandidateSubmission(
        candidate_id=cid,
        source=source,
        submission_id=submission_id,
        vendor=vendor,
        amount=Decimal(amount),
        tax=Decimal(tax),
        date=date or datetime(2026, 6, 14),
        description=description,
        attribution_target_id=attribution_target_id,
        source_hint=source_hint,
        received_at=received_at,
        artifact_media_type=media_type,
        artifact_sha256=sha256,
        submitted_at=datetime(2026, 6, 20, 18, 3, 44, tzinfo=timezone.utc),
    )


# --- candidate_id ------------------------------------------------------------


def test_candidate_id_is_deterministic_and_identity_scoped():
    """Same (source, submission_id) → same id; either differing → a different id."""
    base = candidate_id("acme-extractor", "s-1")
    assert base == candidate_id("acme-extractor", "s-1")  # deterministic
    assert base != candidate_id("acme-extractor", "s-2")  # submission_id scopes
    assert base != candidate_id("other-extractor", "s-1")  # source namespaces
    assert len(base) == 64 and all(c in "0123456789abcdef" for c in base)  # hex, URL-safe


# --- FileCandidateStore ------------------------------------------------------


async def test_candidate_round_trip_keeps_money_exact(tmp_path):
    store = FileCandidateStore(tmp_path / "candidates.jsonl")
    submission = _submission(amount="82.50", tax="10.73")
    await store.add(submission)

    got = await store.get(submission.candidate_id)
    assert got is not None
    assert got == submission  # full round-trip
    assert got.amount == Decimal("82.50") and isinstance(got.amount, Decimal)
    assert got.tax == Decimal("10.73")


async def test_candidate_add_is_idempotent_first_write_wins(tmp_path):
    """A re-add of the same candidate_id is a no-op; the first stored row wins."""
    store = FileCandidateStore(tmp_path / "candidates.jsonl")
    first = _submission(vendor="Home Depot")
    await store.add(first)
    # A differing re-submission under the same identity must not overwrite.
    await store.add(_submission(vendor="Different Vendor"))

    everything = await store.all()
    assert len(everything) == 1
    assert everything[0].vendor == "Home Depot"


async def test_candidate_add_appends_one_raw_row_on_reinsert(tmp_path):
    """Store-level idempotency pinned at the FILE, not transitively via `all()`.

    `all()` re-dedupes first-write-wins, so a broken `add()` that appended a second row
    for the same id would still read back as one candidate through `all()` — the store's
    own `candidate_id` no-op stays untested. This reads `candidates.jsonl` directly: a
    re-`add()` of the same identity must append NOTHING, leaving exactly one raw line.
    """
    import json

    path = tmp_path / "candidates.jsonl"
    store = FileCandidateStore(path)
    await store.add(_submission(vendor="Home Depot"))
    await store.add(_submission(vendor="Different Vendor"))  # same identity → no-op

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1  # exactly one row on disk — the no-op appended nothing
    assert json.loads(lines[0])["vendor"] == "Home Depot"  # the first write persisted


async def test_candidate_absent_tax_coalesces_to_zero(tmp_path):
    """A row with a null tax reads back as Decimal('0') (never None-money)."""
    store = FileCandidateStore(tmp_path / "candidates.jsonl")
    # Write a hand-shaped row with tax=null (the boundary the ledger store guards too).
    path = tmp_path / "candidates.jsonl"
    submission = _submission()
    import json

    record = {
        "candidate_id": submission.candidate_id,
        "source": submission.source,
        "submission_id": submission.submission_id,
        "vendor": submission.vendor,
        "amount": "82.50",
        "tax": None,
        "date": submission.date.isoformat(),
        "description": submission.description,
        "attribution_target_id": submission.attribution_target_id,
        "source_hint": submission.source_hint,
        "received_at": None,
        "artifact_media_type": submission.artifact_media_type,
        "artifact_sha256": submission.artifact_sha256,
        "submitted_at": submission.submitted_at.isoformat(),
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    got = await store.get(submission.candidate_id)
    assert got is not None and got.tax == Decimal("0")


async def test_candidate_get_unknown_is_none(tmp_path):
    store = FileCandidateStore(tmp_path / "candidates.jsonl")
    assert await store.get("nope") is None
    assert await store.all() == []


async def test_candidate_all_preserves_submission_order(tmp_path):
    store = FileCandidateStore(tmp_path / "candidates.jsonl")
    for i in range(3):
        await store.add(_submission(submission_id=f"s-{i}", vendor=f"V{i}"))
    assert [s.vendor for s in await store.all()] == ["V0", "V1", "V2"]


# --- FileArtifactStore -------------------------------------------------------


async def test_artifact_round_trip_and_idempotent(tmp_path):
    store = FileArtifactStore(tmp_path / "artifacts")
    raw = b"\xff\xd8\xff raw jpeg bytes"
    await store.put("cid-1", raw)
    assert await store.get("cid-1") == raw
    # Idempotent — a re-put keeps the first bytes (first write wins).
    await store.put("cid-1", b"different")
    assert await store.get("cid-1") == raw


async def test_artifact_unknown_is_none(tmp_path):
    store = FileArtifactStore(tmp_path / "artifacts")
    assert await store.get("missing") is None


# --- FileCandidateDecisionStore ----------------------------------------------


async def test_decision_confirm_round_trip_keeps_money_exact(tmp_path):
    store = FileCandidateDecisionStore(tmp_path / "candidate_decisions.jsonl")
    decision = CandidateDecision(
        candidate_id="cid-1",
        action=ACTION_CONFIRM,
        source=SOURCE_HUMAN,
        decided_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        vendor="Home Depot",
        amount=Decimal("100.00"),
        tax=Decimal("13.00"),
        date=datetime(2026, 6, 14),
        description="Corrected",
        attribution_target_id="target-001",
        transaction_key="abc123",
        ledger_outcome="stored",
    )
    await store.record(decision)
    (got,) = await store.all()
    assert got == decision
    assert got.amount == Decimal("100.00") and isinstance(got.amount, Decimal)


async def test_decision_reject_round_trip(tmp_path):
    store = FileCandidateDecisionStore(tmp_path / "candidate_decisions.jsonl")
    decision = CandidateDecision(
        candidate_id="cid-2",
        action=ACTION_REJECT,
        source=SOURCE_HUMAN,
        decided_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        reject_reason="not a business expense",
    )
    await store.record(decision)
    (got,) = await store.all()
    assert got == decision
    assert got.amount is None and got.transaction_key is None


async def test_decision_latest_by_candidate_last_write_wins(tmp_path):
    store = FileCandidateDecisionStore(tmp_path / "candidate_decisions.jsonl")
    early = CandidateDecision(
        candidate_id="cid-1",
        action=ACTION_REJECT,
        source=SOURCE_HUMAN,
        decided_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        reject_reason="first",
    )
    later = CandidateDecision(
        candidate_id="cid-1",
        action=ACTION_CONFIRM,
        source=SOURCE_HUMAN,
        decided_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
        ledger_outcome="stored",
        transaction_key="k",
    )
    await store.record(early)
    await store.record(later)
    latest = await store.latest_by_candidate()
    assert latest["cid-1"].action == ACTION_CONFIRM  # last appended wins
    assert len(await store.all()) == 2  # both kept in the trail
