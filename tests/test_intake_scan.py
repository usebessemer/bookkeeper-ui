"""Slice 5 · A3 — the offline drop-directory intake, end to end.

Drives `POST /intake/scan` (the JSON scan) and `POST /ui/intake/scan` (its htmx twin)
plus the `GET /` win-state gate, over `create_app` with an injected tmp-path drop dir.
Covers the acceptance criteria A3 owns:

- **AC 11** — a valid drop document + artifact → scan ingests it; re-scanning ingests
  nothing new (store-`candidate_id` idempotency); a malformed file is reported in the
  scan result **without** blocking the valid ones.
- **AC 19 (the A3 half)** — the win state offers "Scan drop folder to check for new"
  only when the drop dir is enabled; unwired, the headline stands alone and
  `POST /intake/scan` is a 503 (the MUST flow never depends on this SHOULD feature).
- The A3-specific artifact rule (exactly one of `artifact` / `artifact_file`), the
  path-escape refusal, and money as exact-`Decimal` strings on the scan path.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import bookkeeper_ui.api as api
import bookkeeper_ui.intake_scan as intake_scan
from bookkeeper_ui.api import create_app
from bookkeeper_ui.candidates import (
    FileArtifactStore,
    FileCandidateDecisionStore,
    FileCandidateStore,
    candidate_id as compute_candidate_id,
)
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore

_ARTIFACT_BYTES = b"\xff\xd8\xff\x00 a small sample receipt jpeg \x01\x02\x03"


@dataclass
class ScanHarness:
    app: FastAPI
    candidates_path: Path
    decisions_path: Path
    artifacts_dir: Path
    drop_dir: Path


def _make(
    tmp_path: Path,
    examples_dir: Path,
    *,
    wire_drop: bool = True,
    wire_intake: bool = True,
    max_artifact_bytes: int | None = None,
) -> ScanHarness:
    drop_dir = tmp_path / "intake_drop"
    if wire_drop:
        drop_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = tmp_path / "candidates.jsonl"
    decisions_path = tmp_path / "candidate_decisions.jsonl"
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        config=load_config(examples_dir / "config.json"),
        ledger_store=FileLedgerStore(tmp_path / "ledger.jsonl"),
        confirmation_store=FileConfirmationStore(tmp_path / "confirmations.jsonl"),
        statement_store=FileStatementStore(tmp_path / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(tmp_path / "reconciliations.jsonl"),
        candidate_store=FileCandidateStore(candidates_path) if wire_intake else None,
        candidate_decision_store=(
            FileCandidateDecisionStore(decisions_path) if wire_intake else None
        ),
        artifact_store=FileArtifactStore(artifacts_dir) if wire_intake else None,
        intake_drop_dir=(drop_dir if wire_drop else None),
        max_artifact_bytes=max_artifact_bytes,
    )
    return ScanHarness(app, candidates_path, decisions_path, artifacts_dir, drop_dir)


@pytest.fixture
def scan(tmp_path, examples_dir) -> ScanHarness:
    return _make(tmp_path, examples_dir)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _doc(**overrides) -> dict:
    """A candidate drop document with `artifact_file` (the offline shape). Override freely."""
    doc = {
        "source": "folder-scan",
        "submission_id": "scan-0001",
        "vendor": "Home Depot",
        "amount": "82.50",
        "tax": "10.73",
        "date": "2026-06-14",
        "description": "Lumber and fasteners",
        "attribution_target_id": "target-001",
        "source_hint": "Receipt - site materials",
        "received_at": "2026-06-14T15:02:11+00:00",
        "artifact_file": "scan-0001.jpg",
        "artifact_media_type": "image/jpeg",
    }
    doc.update(overrides)
    return doc


def _write_doc(drop_dir: Path, name: str, doc: dict) -> None:
    (drop_dir / name).write_text(json.dumps(doc), encoding="utf-8")


def _write_artifact(drop_dir: Path, name: str, data: bytes = _ARTIFACT_BYTES) -> None:
    (drop_dir / name).write_bytes(data)


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- AC 11: a valid drop doc + artifact → ingested; re-scan → nothing new ------


async def test_scan_ingests_valid_document_and_artifact(scan: ScanHarness):
    _write_artifact(scan.drop_dir, "scan-0001.jpg")
    _write_doc(scan.drop_dir, "scan-0001.json", _doc())

    async with _client(scan.app) as client:
        resp = await client.post("/intake/scan")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"scanned": 1, "ingested": 1, "duplicates": 0, "errors": []}

    # The candidate row + its artifact blob were written via the A store path.
    cid = compute_candidate_id("folder-scan", "scan-0001")
    rows = _rows(scan.candidates_path)
    assert len(rows) == 1 and rows[0]["candidate_id"] == cid
    # Money round-trips as the exact strings sent (never a float).
    assert rows[0]["amount"] == "82.50" and rows[0]["tax"] == "10.73"
    assert (scan.artifacts_dir / cid).read_bytes() == _ARTIFACT_BYTES


async def test_rescan_is_idempotent(scan: ScanHarness):
    _write_artifact(scan.drop_dir, "scan-0001.jpg")
    _write_doc(scan.drop_dir, "scan-0001.json", _doc())

    async with _client(scan.app) as client:
        first = (await client.post("/intake/scan")).json()
        second = (await client.post("/intake/scan")).json()

    assert first["ingested"] == 1
    # Files left in place → the second scan re-reads them and the store no-ops the
    # already-present candidate: nothing new written, every file a duplicate (AC 11).
    assert second == {"scanned": 1, "ingested": 0, "duplicates": 1, "errors": []}
    assert len(_rows(scan.candidates_path)) == 1


async def test_inline_base64_artifact_is_accepted(scan: ScanHarness):
    """The A1 inline-`artifact` shape works in a drop doc too (exactly-one-of, satisfied)."""
    doc = _doc(artifact=base64.b64encode(_ARTIFACT_BYTES).decode("ascii"))
    del doc["artifact_file"]
    _write_doc(scan.drop_dir, "inline.json", doc)

    async with _client(scan.app) as client:
        body = (await client.post("/intake/scan")).json()
    assert body["ingested"] == 1 and body["errors"] == []
    cid = compute_candidate_id("folder-scan", "scan-0001")
    assert (scan.artifacts_dir / cid).read_bytes() == _ARTIFACT_BYTES


async def test_malformed_file_reported_without_blocking_valid(scan: ScanHarness):
    """A malformed file is reported in `errors`; the valid one alongside it still ingests."""
    # A valid pair.
    _write_artifact(scan.drop_dir, "good.jpg")
    _write_doc(scan.drop_dir, "good.json", _doc(submission_id="good", artifact_file="good.jpg"))
    # Malformed: amount as a JSON NUMBER (never a string) — the float-bug guard.
    _write_artifact(scan.drop_dir, "bad.jpg")
    _write_doc(
        scan.drop_dir,
        "bad.json",
        _doc(submission_id="bad", artifact_file="bad.jpg", amount=82.5),
    )
    # Malformed: neither artifact nor artifact_file.
    noart = _doc(submission_id="noart")
    del noart["artifact_file"]
    _write_doc(scan.drop_dir, "noart.json", noart)

    async with _client(scan.app) as client:
        body = (await client.post("/intake/scan")).json()

    assert body["scanned"] == 3
    assert body["ingested"] == 1  # the good one ingested despite the bad neighbours
    error_files = {e["file"] for e in body["errors"]}
    assert error_files == {"bad.json", "noart.json"}
    # The amount-as-number error names the field; the missing-artifact error is explicit.
    by_file = {e["file"]: e["error"] for e in body["errors"]}
    assert "amount" in by_file["bad.json"]
    assert "artifact" in by_file["noart.json"]
    # Exactly one candidate landed (the good one) — no partial write for a failed file.
    assert len(_rows(scan.candidates_path)) == 1


async def test_both_artifact_forms_is_ambiguous_error(scan: ScanHarness):
    _write_artifact(scan.drop_dir, "both.jpg")
    doc = _doc(
        submission_id="both",
        artifact_file="both.jpg",
        artifact=base64.b64encode(_ARTIFACT_BYTES).decode("ascii"),
    )
    _write_doc(scan.drop_dir, "both.json", doc)

    async with _client(scan.app) as client:
        body = (await client.post("/intake/scan")).json()
    assert body["ingested"] == 0
    assert len(body["errors"]) == 1
    assert "both" in body["errors"][0]["error"].lower()
    assert _rows(scan.candidates_path) == []


# --- Path-escape: a drop file may never read arbitrary disk --------------------


@pytest.mark.parametrize("escape", ["../secret.bin", "/etc/hostname"])
async def test_artifact_file_path_escape_is_refused(scan: ScanHarness, tmp_path, escape):
    # Plant a secret OUTSIDE the drop dir: if the escape check failed, it would be read.
    (tmp_path / "secret.bin").write_bytes(b"TOP SECRET")
    _write_doc(scan.drop_dir, "escape.json", _doc(submission_id="escape", artifact_file=escape))

    async with _client(scan.app) as client:
        body = (await client.post("/intake/scan")).json()

    assert body["ingested"] == 0
    assert len(body["errors"]) == 1
    assert body["errors"][0]["file"] == "escape.json"
    # Nothing outside the drop dir was ingested.
    assert _rows(scan.candidates_path) == []
    cid = compute_candidate_id("folder-scan", "escape")
    assert not (scan.artifacts_dir / cid).exists()


async def test_missing_artifact_file_is_reported(scan: ScanHarness):
    # A doc pointing at a file that isn't in the drop dir.
    _write_doc(scan.drop_dir, "missing.json", _doc(submission_id="m", artifact_file="nope.jpg"))
    async with _client(scan.app) as client:
        body = (await client.post("/intake/scan")).json()
    assert body["ingested"] == 0 and len(body["errors"]) == 1
    assert "artifact_file" in body["errors"][0]["error"]


# --- Money discipline: strings-only, no float on the scan path -----------------


async def test_money_round_trips_as_exact_strings(scan: ScanHarness):
    _write_artifact(scan.drop_dir, "money.jpg")
    _write_doc(
        scan.drop_dir,
        "money.json",
        _doc(submission_id="money", artifact_file="money.jpg", amount="1234.05", tax="0.00"),
    )
    async with _client(scan.app) as client:
        await client.post("/intake/scan")
        listing = (await client.get("/intake/candidates")).json()
    row = listing["candidates"][0]["candidate"]
    # The exact strings sent survive the scan+store round-trip — never re-parsed via float.
    assert row["amount"] == "1234.05"
    assert row["tax"] == "0.00"


def test_scan_module_has_no_float_on_money_path():
    """Guardrail 4: no `float(` anywhere in the scan module (money is a Decimal string)."""
    source = Path(intake_scan.__file__).read_text(encoding="utf-8")
    assert "float(" not in source


async def test_absent_tax_coalesces_to_zero(scan: ScanHarness):
    _write_artifact(scan.drop_dir, "notax.jpg")
    doc = _doc(submission_id="notax", artifact_file="notax.jpg")
    del doc["tax"]
    _write_doc(scan.drop_dir, "notax.json", doc)
    async with _client(scan.app) as client:
        await client.post("/intake/scan")
        listing = (await client.get("/intake/candidates")).json()
    assert listing["candidates"][0]["candidate"]["tax"] == "0"


# --- Empty / missing dir: an empty scan, never an error -----------------------


async def test_empty_drop_dir_is_an_empty_scan(scan: ScanHarness):
    async with _client(scan.app) as client:
        body = (await client.post("/intake/scan")).json()
    assert body == {"scanned": 0, "ingested": 0, "duplicates": 0, "errors": []}


async def test_non_json_files_are_ignored(scan: ScanHarness):
    _write_artifact(scan.drop_dir, "loose.jpg")  # a stray non-.json file
    (scan.drop_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
    async with _client(scan.app) as client:
        body = (await client.post("/intake/scan")).json()
    # Only *.json files are scanned; the stray files are neither scanned nor errors.
    assert body == {"scanned": 0, "ingested": 0, "duplicates": 0, "errors": []}


# --- AC 19 (A3 half): the drop-dir feature gate -------------------------------


async def test_scan_503_when_drop_dir_unwired(tmp_path, examples_dir):
    """Unwired (`intake_drop_dir is None`) → the scan refuses a 503, never a silent no-op."""
    h = _make(tmp_path, examples_dir, wire_drop=False)
    async with _client(h.app) as client:
        resp = await client.post("/intake/scan")
    assert resp.status_code == 503


async def test_win_state_omits_scan_prompt_when_unwired(tmp_path, examples_dir):
    """Disabled → the win-state headline stands alone; no 'Scan drop folder' prompt."""
    h = _make(tmp_path, examples_dir, wire_drop=False)
    async with _client(h.app) as client:
        home = (await client.get("/")).text
    assert "All receipts reviewed" in home  # the MUST headline still renders
    assert "Scan drop folder" not in home  # the SHOULD prompt is absent (no hard dep)


async def test_win_state_offers_scan_prompt_when_wired(scan: ScanHarness):
    """Enabled → the win state offers 'Scan drop folder to check for new'."""
    async with _client(scan.app) as client:
        home = (await client.get("/")).text  # nothing seeded → win state
    assert "All receipts reviewed" in home
    assert "Scan drop folder to check for new" in home
    assert 'hx-post="/ui/intake/scan"' in home


async def test_queue_shows_scan_button_when_wired(scan: ScanHarness):
    """Enabled + a pending candidate → the queue view also offers the scan button."""
    _write_artifact(scan.drop_dir, "scan-0001.jpg")
    _write_doc(scan.drop_dir, "scan-0001.json", _doc())
    async with _client(scan.app) as client:
        await client.post("/intake/scan")  # ingest one → a pending card
        home = (await client.get("/")).text
    assert 'id="intake-queue"' in home  # the queue is showing (not the win state)
    assert "Scan drop folder" in home


# --- The htmx twin: POST /ui/intake/scan renders the result partial -----------


async def test_ui_scan_twin_renders_result_partial(scan: ScanHarness):
    _write_artifact(scan.drop_dir, "scan-0001.jpg")
    _write_doc(scan.drop_dir, "scan-0001.json", _doc())
    async with _client(scan.app) as client:
        resp = await client.post("/ui/intake/scan")
    assert resp.status_code == 200
    html = resp.text
    assert "1 new" in html  # the tally line
    # A candidate actually landed via the same store path.
    assert len(_rows(scan.candidates_path)) == 1
    # New candidates → the refresh-the-queue affordance is offered.
    assert "Refresh the queue" in html


async def test_ui_scan_twin_lists_malformed_files(scan: ScanHarness):
    bad = _doc(submission_id="bad")
    del bad["artifact_file"]  # neither artifact form → an error line
    _write_doc(scan.drop_dir, "bad.json", bad)
    async with _client(scan.app) as client:
        html = (await client.post("/ui/intake/scan")).text
    assert "bad.json" in html  # the malformed file is a visible line (AC 11)
    assert "skipped" in html.lower()


async def test_ui_scan_twin_errors_when_unwired(tmp_path, examples_dir):
    h = _make(tmp_path, examples_dir, wire_drop=False)
    async with _client(h.app) as client:
        resp = await client.post("/ui/intake/scan")
    # The UI convention: a config error is a 200 error partial, not a 500.
    assert resp.status_code == 200
    assert "not configured" in resp.text.lower()


# --- A NaN/Infinity STRING on the scan money path (pin 24) --------------------


async def test_scan_nan_infinity_string_amount_is_rejected(scan: ScanHarness):
    """`_parse_money` has two guards: `_require_str` refuses a JSON *number* before
    `Decimal()`, and `is_finite()` refuses a NaN/Infinity *string* after it. The only
    other scan-money test uses a JSON number (branch 1); this exercises branch 2 — a
    "NaN"/"Infinity" string is rejected, its file skipped, nothing ingested."""
    _write_artifact(scan.drop_dir, "nan.jpg")
    _write_artifact(scan.drop_dir, "inf.jpg")
    _write_doc(
        scan.drop_dir, "nan.json", _doc(submission_id="nan", artifact_file="nan.jpg", amount="NaN")
    )
    _write_doc(
        scan.drop_dir,
        "inf.json",
        _doc(submission_id="inf", artifact_file="inf.jpg", amount="Infinity"),
    )
    async with _client(scan.app) as client:
        summary = (await client.post("/intake/scan")).json()
    assert summary["ingested"] == 0
    by_file = {e["file"]: e["error"] for e in summary["errors"]}
    assert "finite" in by_file["nan.json"] and "finite" in by_file["inf.json"]
    assert _rows(scan.candidates_path) == []


# --- A1 == A3: the two front doors enforce the IDENTICAL gate (pin 23) --------


def test_a1_a3_share_identical_media_allowlist_and_cap():
    """The highest-value missing pin. A3 (`intake_scan`) hand-restates A1's (`api`) gate
    because an `intake_scan → api → web → intake_scan` import cycle forbids importing A1's
    rules — a STRUCTURALLY FORCED duplication that cannot be deduped, so it must be pinned.
    Mutation (f) added `image/tiff` to the scan allowlist (a type A1 rejects) and all 425
    tests stayed green; asserting the two rule SETS are identical kills it."""
    assert intake_scan.ALLOWED_ARTIFACT_MEDIA_TYPES == api.ALLOWED_ARTIFACT_MEDIA_TYPES
    assert intake_scan.DEFAULT_MAX_ARTIFACT_BYTES == api.DEFAULT_MAX_ARTIFACT_BYTES


def _a1_payload(**overrides) -> dict:
    """The A1 (`POST /intake/candidates`) shape of the shared baseline — inline base64
    `artifact` instead of A3's `artifact_file`, every other field identical to `_doc`."""
    payload = _doc()
    del payload["artifact_file"]
    payload["artifact"] = base64.b64encode(_ARTIFACT_BYTES).decode("ascii")
    payload.update(overrides)
    return payload


# One shared accept/reject table, driven through BOTH front doors. Each case's verdict
# must match on A1 and A3 — the whole point of the equivalence. Covers the field rules
# both gates restate: money (exponent / non-finite string), the non-blank rule, and the
# media allowlist (incl. the off-allowlist type mutation (f) slipped onto A3).
_EQUIVALENCE_CASES = [
    ("valid_baseline", {}, True),
    ("amount_exponent_notation", {"amount": "1E+2"}, False),
    ("amount_nan_string", {"amount": "NaN"}, False),
    ("amount_infinity_string", {"amount": "Infinity"}, False),
    ("tax_exponent_notation", {"tax": "1e2"}, False),
    ("blank_vendor", {"vendor": "   "}, False),
    ("off_allowlist_media_type", {"artifact_media_type": "image/tiff"}, False),
]


@pytest.mark.parametrize(
    "overrides,accept", [(o, a) for _n, o, a in _EQUIVALENCE_CASES],
    ids=[n for n, _o, _a in _EQUIVALENCE_CASES],
)
async def test_a1_a3_validation_equivalence(tmp_path, examples_dir, overrides, accept):
    """The SAME field rules decide identically on the A1 (`POST /intake/candidates`) and
    A3 (`POST /intake/scan`) front doors — driven through both from one table. A rule that
    drifted on one gate (e.g. an off-allowlist media type accepted only by A3) breaks the
    matching verdict here."""
    # A1: the JSON port (inline base64 artifact) → 201 accept / 422 reject.
    a1 = _make(tmp_path / "a1", examples_dir)
    async with _client(a1.app) as client:
        resp = await client.post("/intake/candidates", json=_a1_payload(**overrides))
    a1_accepted = resp.status_code == 201

    # A3: the drop scan (artifact_file) → ingested==1 accept / a per-file error reject.
    a3 = _make(tmp_path / "a3", examples_dir)
    _write_artifact(a3.drop_dir, "eq.jpg")
    _write_doc(a3.drop_dir, "eq.json", _doc(artifact_file="eq.jpg", **overrides))
    async with _client(a3.app) as client:
        summary = (await client.post("/intake/scan")).json()
    a3_accepted = summary["ingested"] == 1

    assert a1_accepted == accept  # A1 verdict matches the table
    assert a3_accepted == accept  # A3 verdict matches the table
    assert a1_accepted == a3_accepted  # ...and the two front doors agree


async def test_a1_a3_over_cap_artifact_rejected_identically(tmp_path, examples_dir):
    """The size cap decides identically on both front doors too (the minor note under pin
    23): an over-cap artifact is a 422 on A1 and a per-file error on A3 — never ingested."""
    big = b"x" * 100
    a1 = _make(tmp_path / "a1", examples_dir, max_artifact_bytes=10)
    async with _client(a1.app) as client:
        resp = await client.post(
            "/intake/candidates",
            json=_a1_payload(artifact=base64.b64encode(big).decode("ascii")),
        )
    assert resp.status_code == 422

    a3 = _make(tmp_path / "a3", examples_dir, max_artifact_bytes=10)
    _write_artifact(a3.drop_dir, "big.jpg", big)
    _write_doc(a3.drop_dir, "big.json", _doc(artifact_file="big.jpg"))
    async with _client(a3.app) as client:
        summary = (await client.post("/intake/scan")).json()
    assert summary["ingested"] == 0 and len(summary["errors"]) == 1
