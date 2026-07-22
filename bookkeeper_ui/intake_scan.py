"""Slice 5 · A3 — the offline drop-directory intake (a second front door onto A1).

The primary intake mode (A1) is `POST /intake/candidates`: an extractor POSTs one
candidate document (extracted fields + the source artifact, base64). A3 is the
**offline / push-can't-reach** mode: an extractor that cannot POST drops candidate
`*.json` files into a directory, and the app ingests them **on demand** via a scan
(`POST /intake/scan` + its `POST /ui/intake/scan` htmx twin). No watcher, no poller —
the app stays request-driven.

A3 is a *second front door* onto the identical A1 store path: it validates each
dropped document by the **same field rules** and writes through the **same**
`FileCandidateStore` / `FileArtifactStore`. It never touches the ledger; only a human
confirm (issue B) does.

**Idempotency comes entirely from the store's `candidate_id` dedupe.** A re-scan
re-reads every file, re-computes each `candidate_id`, and the idempotent store no-ops
the ones already present — so re-scanning ingests nothing new. Files are left in place;
A3 keeps no processed-manifest and moves no files.

This module lives apart from `api.py` (the JSON surface) and `web.py` (the HTML
surface) so *both* call one implementation of the drop path. It restates A1's field
rules rather than importing them because the `api.py → web.py` import direction
forbids `web.py` reaching `api.py`'s validators — the same reason `web.py` restates
them in its `_revalidate_*` helpers. The rules mirror `api.py`'s A1 gate verbatim
(`_require_nonblank` / `_parse_money` / `_parse_iso_datetime`, the media allowlist, the
size cap); keep the two in sync. **Money is a string → exact `Decimal` on every path —
never a lossy float** (guardrail 4).

The drop document is A1's schema with **one** addition: instead of A1's inline base64
`artifact`, a drop document must carry **exactly one of** `artifact` (inline base64) or
`artifact_file` (a filename relative to the drop dir, read as the raw bytes). Zero or
both → an error for that file. Every other field and rule is A1's, unchanged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bookkeeper_ui.candidates import (
    CandidateSubmission,
    FileArtifactStore,
    FileCandidateStore,
    candidate_id as compute_candidate_id,
)

# Mirror of `api.py`'s A1 constants (this module cannot import them — that would be an
# `intake_scan → api → web → intake_scan` cycle). Keep these in lockstep with
# `ALLOWED_ARTIFACT_MEDIA_TYPES` / `DEFAULT_MAX_ARTIFACT_BYTES` in `api.py`.
ALLOWED_ARTIFACT_MEDIA_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "application/pdf",
        "text/plain",
    }
)
DEFAULT_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024  # 10 MiB


class DropDocumentError(Exception):
    """A per-file validation/ingest failure — its message *is* the human error string.

    One bad file never aborts the scan: the orchestrator catches this, records
    `{file, error}`, and moves to the next file. Nothing is written for a failed file.
    """


@dataclass(frozen=True)
class ScanFileError:
    """One per-file failure in a scan — the file name and the reason it was skipped."""

    file: str
    error: str


@dataclass
class ScanSummary:
    """The outcome of one drop-dir scan.

    `scanned` = total `*.json` files seen; `ingested` = new candidate rows written this
    scan; `duplicates` = files whose `(source, submission_id)` was already stored (the
    store no-op — the re-scan-ingests-nothing guarantee made visible); `errors` = the
    per-file failures (a malformed file is reported here without blocking the valid ones).
    """

    scanned: int = 0
    ingested: int = 0
    duplicates: int = 0
    errors: list[ScanFileError] = field(default_factory=list)


def iter_drop_documents(drop_dir: str | Path) -> list[Path]:
    """Every `*.json` file in the drop dir, sorted for determinism.

    Non-recursive; a stray non-`.json` file is ignored (not an error). A missing or
    empty directory yields `[]` (an empty scan, never an error).
    """
    root = Path(drop_dir)
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.json") if p.is_file())


def _require_str(value: object, field_name: str) -> str:
    """Return `value` if it is a string, else a `DropDocumentError` naming the field.

    A1 gets JSON-type enforcement for free from pydantic (a number for a `str` field is
    a 422); the file-read path parses raw JSON, so a non-string reaches here as an
    `int` / `float` / `list` and must be refused explicitly — never coerced.
    """
    if not isinstance(value, str):
        raise DropDocumentError(
            f"{field_name}: expected a JSON string, got {type(value).__name__}."
        )
    return value


def _require_nonblank(value: object, field_name: str) -> str:
    """A1's non-blank rule: `value` must be a non-blank string (else an error)."""
    text = _require_str(value, field_name)
    if not text.strip():
        raise DropDocumentError(f"{field_name} is required and must be a non-blank string.")
    return text


def _parse_money(value: object, field_name: str) -> Decimal:
    """Parse a JSON-**string** money value to a finite `Decimal` (A1's rule, verbatim).

    Money crosses the drop wire as a string; a JSON *number* is refused naming the field
    (never re-introduce the float bug — a bare `Decimal(82.5)` would silently accept a
    lossy float). `NaN` / `Infinity` parse but are not finite, so they are rejected too.
    """
    raw = _require_str(value, field_name)
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DropDocumentError(
            f"{field_name} {raw!r} is not a valid decimal amount (money is a string)."
        ) from exc
    if not parsed.is_finite():
        raise DropDocumentError(
            f"{field_name} must be a finite amount — {raw!r} (NaN/Infinity) is rejected."
        )
    # Exponent notation (`1E+2`) parses finite but round-trips as E-notation, yielding a
    # different `transaction_key` than the equal `100` — refuse it (A1's rule, verbatim).
    if "e" in raw.lower():
        raise DropDocumentError(
            f"{field_name} {raw!r} uses exponent notation — money is a plain decimal "
            f'string (e.g. "100", not "1E+2").'
        )
    return parsed


def _parse_iso_datetime(value: object, field_name: str) -> datetime:
    """Parse an ISO 8601 **string** to a `datetime`, else an error naming the field."""
    raw = _require_str(value, field_name)
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        raise DropDocumentError(
            f"{field_name} {raw!r} is not an ISO 8601 date/datetime."
        ) from exc


def _optional_str(value: object, field_name: str, default: str = "") -> str:
    """An optional string field: absent/null → `default`; a non-string → an error."""
    if value is None:
        return default
    return _require_str(value, field_name)


def read_artifact_bytes(document: dict[str, object], drop_dir: str | Path) -> bytes:
    """Resolve a drop document's artifact bytes — the A3-specific `artifact`/`artifact_file` rule.

    Exactly one of:
      - `artifact` — inline base64 (identical to A1), or
      - `artifact_file` — a filename **relative to the drop dir**, read as raw bytes.

    Zero present → an error; both present → an error (ambiguous). An `artifact_file` that
    escapes the drop dir (a `..` traversal or an absolute path) is refused (a drop file
    must never read arbitrary disk — the safe-resolve precedent is `web.py`'s guarded
    export download). A missing/unreadable file → an error. Size / non-empty are checked
    later, against the resolved bytes, exactly as A1 checks them after its base64 decode.
    """
    has_inline = document.get("artifact") not in (None, "")
    has_file = document.get("artifact_file") not in (None, "")
    if has_inline and has_file:
        raise DropDocumentError(
            "exactly one of 'artifact' or 'artifact_file' may be present — both is ambiguous."
        )
    if not has_inline and not has_file:
        raise DropDocumentError(
            "exactly one of 'artifact' or 'artifact_file' is required."
        )

    if has_inline:
        raw = _require_str(document.get("artifact"), "artifact")
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DropDocumentError("artifact is not valid base64.") from exc

    # `artifact_file`: resolve relative to the drop dir; never let it escape.
    rel = _require_str(document.get("artifact_file"), "artifact_file")
    root = Path(drop_dir).resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        raise DropDocumentError(
            f"artifact_file {rel!r} escapes the drop directory — refused (a drop file "
            f"may not read arbitrary disk)."
        )
    try:
        return target.read_bytes()
    except OSError as exc:
        raise DropDocumentError(
            f"artifact_file {rel!r} could not be read — no such file in the drop directory."
        ) from exc


def build_candidate(
    document: dict[str, object],
    artifact_bytes: bytes,
    *,
    max_artifact_bytes: int,
) -> CandidateSubmission:
    """Validate a drop document by A1's field rules → a `CandidateSubmission` (no write).

    Identical rules to `POST /intake/candidates`: non-blank `source`/`submission_id`/
    `vendor`; finite-`Decimal` money as strings (`tax` absent/blank → `Decimal("0")`);
    ISO dates; an allowlisted media type; decoded artifact bytes non-empty and ≤ the cap.
    Any failure raises `DropDocumentError` naming the field — **never a partial write**.
    """
    source = _require_nonblank(document.get("source"), "source")
    submission_id = _require_nonblank(document.get("submission_id"), "submission_id")
    vendor = _require_nonblank(document.get("vendor"), "vendor")
    amount = _parse_money(document.get("amount"), "amount")
    raw_tax = document.get("tax")
    tax = Decimal("0") if raw_tax in (None, "") else _parse_money(raw_tax, "tax")
    date = _parse_iso_datetime(document.get("date"), "date")
    raw_received = document.get("received_at")
    received_at = (
        None if raw_received in (None, "") else _parse_iso_datetime(raw_received, "received_at")
    )
    description = _optional_str(document.get("description"), "description")
    source_hint = _optional_str(document.get("source_hint"), "source_hint")
    raw_target = document.get("attribution_target_id")
    attribution_target_id = None if raw_target is None else _require_str(
        raw_target, "attribution_target_id"
    )

    media_type = _require_str(document.get("artifact_media_type"), "artifact_media_type")
    if media_type not in ALLOWED_ARTIFACT_MEDIA_TYPES:
        raise DropDocumentError(
            f"artifact_media_type {media_type!r} is not allowed — one of "
            f"{sorted(ALLOWED_ARTIFACT_MEDIA_TYPES)}."
        )
    if not artifact_bytes:
        raise DropDocumentError(
            "artifact resolved to empty bytes — an artifact is required."
        )
    if len(artifact_bytes) > max_artifact_bytes:
        raise DropDocumentError(
            f"artifact is {len(artifact_bytes)} bytes — over the {max_artifact_bytes}-byte cap."
        )

    cid = compute_candidate_id(source, submission_id)
    return CandidateSubmission(
        candidate_id=cid,
        source=source,
        submission_id=submission_id,
        vendor=vendor,
        amount=amount,
        tax=tax,
        date=date,
        description=description,
        attribution_target_id=attribution_target_id,
        source_hint=source_hint,
        received_at=received_at,
        artifact_media_type=media_type,
        artifact_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        submitted_at=datetime.now(timezone.utc),
    )


async def scan_drop_dir(
    *,
    drop_dir: str | Path,
    candidate_store: FileCandidateStore,
    artifact_store: FileArtifactStore,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> ScanSummary:
    """Scan the drop dir → ingest each valid candidate document through the A1 store path.

    Per-file isolated: a parse failure, an artifact-resolution failure, or a validation
    failure records `{file, error}` and continues — **one bad file never aborts the
    scan**, and a failed file writes nothing. Ingest rides the store's `candidate_id`
    idempotency: an already-present candidate is counted a `duplicate`, not an ingest,
    so a second scan over the unchanged dir writes nothing new (AC 11).

    Artifact first (its sha256 is already on the row), then the candidate row — the row
    is the index, so a crash between the two leaves an orphan blob, never a row pointing
    at missing bytes (A1's ordering).
    """
    summary = ScanSummary()
    for path in iter_drop_documents(drop_dir):
        summary.scanned += 1
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            summary.errors.append(ScanFileError(path.name, f"not readable as JSON: {exc}"))
            continue
        if not isinstance(document, dict):
            summary.errors.append(
                ScanFileError(path.name, "a drop document must be a JSON object.")
            )
            continue
        try:
            artifact_bytes = read_artifact_bytes(document, drop_dir)
            submission = build_candidate(
                document, artifact_bytes, max_artifact_bytes=max_artifact_bytes
            )
        except DropDocumentError as exc:
            summary.errors.append(ScanFileError(path.name, str(exc)))
            continue

        if await candidate_store.get(submission.candidate_id) is not None:
            # Already on record (first write wins) — a re-scan no-op, not a fresh ingest.
            summary.duplicates += 1
            continue
        await artifact_store.put(submission.candidate_id, artifact_bytes)
        await candidate_store.add(submission)
        summary.ingested += 1
    return summary
