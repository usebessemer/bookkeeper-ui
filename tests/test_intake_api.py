"""The intake port JSON API, end to end (httpx over the ASGI app).

Drives `create_app` with injected tmp-path intake stores + the committed sample
config, exercising Slice 5 · A's acceptance criteria: submit a candidate (idempotent,
validated) → fetch its artifact → the human confirm/reject that gates it into the
ledger (honest dedupe, the C1 closed-period guard) → the shared queue projection.
The ledger round-trip (AC #6) is asserted on the owned JSON `GET /ledger` surface.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper_ui.api import create_app
from bookkeeper_ui.candidates import (
    FileArtifactStore,
    FileCandidateDecisionStore,
    FileCandidateStore,
)
from bookkeeper_ui.closes import CloseRecord, FileCloseStore
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore

_ARTIFACT_BYTES = b"\xff\xd8\xff\x00 a small sample receipt jpeg \x01\x02\x03"


@dataclass
class IntakeHarness:
    app: FastAPI
    ledger_path: Path
    candidates_path: Path
    decisions_path: Path
    artifacts_dir: Path


def _harness(
    tmp_path: Path,
    examples_dir: Path,
    *,
    close_store: FileCloseStore | None = None,
    max_artifact_bytes: int | None = None,
    wire_intake: bool = True,
) -> IntakeHarness:
    ledger_path = tmp_path / "ledger.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    decisions_path = tmp_path / "candidate_decisions.jsonl"
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        config=load_config(examples_dir / "config.json"),
        ledger_store=FileLedgerStore(ledger_path),
        confirmation_store=FileConfirmationStore(tmp_path / "confirmations.jsonl"),
        statement_store=FileStatementStore(tmp_path / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(tmp_path / "reconciliations.jsonl"),
        close_store=close_store,
        candidate_store=FileCandidateStore(candidates_path) if wire_intake else None,
        candidate_decision_store=(
            FileCandidateDecisionStore(decisions_path) if wire_intake else None
        ),
        artifact_store=FileArtifactStore(artifacts_dir) if wire_intake else None,
        max_artifact_bytes=max_artifact_bytes,
    )
    return IntakeHarness(app, ledger_path, candidates_path, decisions_path, artifacts_dir)


@pytest.fixture
def intake(tmp_path, examples_dir) -> IntakeHarness:
    return _harness(tmp_path, examples_dir)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _payload(**overrides) -> dict:
    payload = {
        "source": "acme-extractor",
        "submission_id": "acme-18c9f2ab44e01",
        "vendor": "Home Depot",
        "amount": "82.50",
        "tax": "10.73",
        "date": "2026-06-14",
        "description": "Lumber and fasteners",
        "attribution_target_id": "target-001",
        "source_hint": "Receipt - site materials",
        "received_at": "2026-06-14T15:02:11+00:00",
        "artifact": base64.b64encode(_ARTIFACT_BYTES).decode("ascii"),
        "artifact_media_type": "image/jpeg",
    }
    payload.update(overrides)
    return payload


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- AC #1: submit ------------------------------------------------------------


async def test_submit_candidate_persists_row_and_artifact(intake: IntakeHarness):
    async with _client(intake.app) as client:
        resp = await client.post("/intake/candidates", json=_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["duplicate"] is False
    candidate = body["candidate"]
    # Money round-trips as the exact strings sent (never a float).
    assert candidate["amount"] == "82.50"
    assert candidate["tax"] == "10.73"
    cid = candidate["candidate_id"]

    rows = _rows(intake.candidates_path)
    assert len(rows) == 1 and rows[0]["candidate_id"] == cid
    assert rows[0]["amount"] == "82.50" and rows[0]["tax"] == "10.73"
    assert (intake.artifacts_dir / cid).read_bytes() == _ARTIFACT_BYTES


async def test_absent_tax_defaults_to_zero(intake: IntakeHarness):
    payload = _payload()
    del payload["tax"]
    async with _client(intake.app) as client:
        resp = await client.post("/intake/candidates", json=payload)
    assert resp.status_code == 201
    assert resp.json()["candidate"]["tax"] == "0"


# --- AC #2: idempotency -------------------------------------------------------


async def test_resubmit_is_idempotent_first_write_wins(intake: IntakeHarness):
    async with _client(intake.app) as client:
        first = await client.post("/intake/candidates", json=_payload())
        assert first.status_code == 201
        # Same (source, submission_id), different payload → 200 duplicate, unchanged.
        second = await client.post(
            "/intake/candidates",
            json=_payload(vendor="Different Vendor", amount="999.99"),
        )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["candidate"]["vendor"] == "Home Depot"  # first write wins
    assert second.json()["candidate"]["amount"] == "82.50"
    assert len(_rows(intake.candidates_path)) == 1  # no new row


# --- AC #3: validation --------------------------------------------------------


async def test_amount_as_json_number_is_422_and_writes_nothing(intake: IntakeHarness):
    async with _client(intake.app) as client:
        resp = await client.post("/intake/candidates", json=_payload(amount=82.50))
    assert resp.status_code == 422
    assert "amount" in json.dumps(resp.json())
    assert _rows(intake.candidates_path) == []
    assert not intake.artifacts_dir.exists() or not any(intake.artifacts_dir.iterdir())


async def test_amount_scientific_notation_is_422_and_writes_nothing(intake: IntakeHarness):
    """`1E+2` parses to a finite Decimal (no float leak) but round-trips as canonical
    E-notation — a DIFFERENT transaction_key than the economically-equal `100`, which
    would weaken honest dedupe. Money on the wire is a plain decimal string, so the
    exponent form is a 422 with nothing written (pin 1)."""
    async with _client(intake.app) as client:
        amt = await client.post("/intake/candidates", json=_payload(amount="1E+2"))
        tax = await client.post("/intake/candidates", json=_payload(tax="1e2"))
    assert amt.status_code == 422 and "amount" in json.dumps(amt.json())
    assert tax.status_code == 422 and "tax" in json.dumps(tax.json())
    assert _rows(intake.candidates_path) == []
    assert not intake.artifacts_dir.exists() or not any(intake.artifacts_dir.iterdir())


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"date": "not-a-date"},
        {"vendor": "   "},
        {"source": ""},
        {"submission_id": ""},
        {"artifact_media_type": "application/zip"},
        {"artifact": base64.b64encode(b"").decode("ascii")},  # empty artifact
        {"artifact": "not valid base64!!!"},
    ],
)
async def test_invalid_payloads_are_422_and_write_nothing(
    intake: IntakeHarness, overrides
):
    async with _client(intake.app) as client:
        resp = await client.post("/intake/candidates", json=_payload(**overrides))
    assert resp.status_code == 422, overrides
    assert _rows(intake.candidates_path) == []


async def test_over_cap_artifact_is_422(tmp_path, examples_dir):
    harness = _harness(tmp_path, examples_dir, max_artifact_bytes=8)
    async with _client(harness.app) as client:
        resp = await client.post(
            "/intake/candidates",
            json=_payload(artifact=base64.b64encode(b"x" * 32).decode("ascii")),
        )
    assert resp.status_code == 422
    assert _rows(harness.candidates_path) == []


# --- AC #4: artifact fetch ----------------------------------------------------


async def test_get_artifact_returns_exact_bytes_and_media_type(intake: IntakeHarness):
    async with _client(intake.app) as client:
        cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        resp = await client.get(f"/intake/artifact/{cid}")
    assert resp.status_code == 200
    assert resp.content == _ARTIFACT_BYTES
    assert resp.headers["content-type"] == "image/jpeg"


async def test_get_artifact_unknown_is_404(intake: IntakeHarness):
    async with _client(intake.app) as client:
        resp = await client.get("/intake/artifact/deadbeef")
    assert resp.status_code == 404


# --- AC #6: confirm files a corrected transaction into the ledger -------------


async def test_confirm_with_corrections_files_to_ledger(intake: IntakeHarness):
    async with _client(intake.app) as client:
        cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        resp = await client.post(
            "/intake/resolve",
            json={
                "candidate_id": cid,
                "action": "confirm",
                "vendor": "Home Depot (corrected)",
                "amount": "100.00",
                "tax": "13.00",
                "attribution_target_id": "target-002",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["standing"] == "confirmed"
        assert body["ledger_outcome"] == "stored"
        txn_key = body["transaction_key"]

        # The ledger gains exactly one row carrying the corrected values + artifact.
        ledger_rows = _rows(intake.ledger_path)
        assert len(ledger_rows) == 1
        row = ledger_rows[0]
        assert row["vendor"] == "Home Depot (corrected)"
        assert row["amount"] == "100.00" and row["tax"] == "13.00"
        assert row["attribution_target_id"] == "target-002"
        assert base64.b64decode(row["artifact_bytes"]) == _ARTIFACT_BYTES
        assert row["key"] == txn_key

        # A decision row records the final values + the ledger link.
        (decision,) = _rows(intake.decisions_path)
        assert decision["action"] == "confirm"
        assert decision["amount"] == "100.00"
        assert decision["transaction_key"] == txn_key
        assert decision["ledger_outcome"] == "stored"

        # GET /ledger (the owned JSON surface, unchanged build_ledger) returns it.
        ledger = (await client.get("/ledger", params={"period": "2026-Q2"})).json()
    ids = {e["transaction"]["id"]: e["status"] for e in ledger["entries"]}
    assert txn_key in ids
    assert ids[txn_key] in ("proposed", "flagged")


# --- AC #7: attribution target must be configured -----------------------------


async def test_confirm_requires_configured_attribution_target(intake: IntakeHarness):
    payload = _payload(attribution_target_id=None)  # extractor didn't resolve one
    async with _client(intake.app) as client:
        cid = (await client.post("/intake/candidates", json=payload)).json()[
            "candidate"
        ]["candidate_id"]
        # Human confirm without assigning a target → 422, no writes.
        absent = await client.post(
            "/intake/resolve", json={"candidate_id": cid, "action": "confirm"}
        )
        # ...or one not in config.attribution_targets → 422.
        invalid = await client.post(
            "/intake/resolve",
            json={"candidate_id": cid, "action": "confirm", "attribution_target_id": "nope"},
        )
    assert absent.status_code == 422
    assert invalid.status_code == 422
    assert _rows(intake.ledger_path) == []
    assert _rows(intake.decisions_path) == []


# --- AC #8: honest dedupe is visible -----------------------------------------


async def test_duplicate_confirm_is_visible_not_silent(intake: IntakeHarness):
    async with _client(intake.app) as client:
        first_cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        await client.post(
            "/intake/resolve", json={"candidate_id": first_cid, "action": "confirm"}
        )
        # A second, distinct candidate whose confirmed business fields are identical.
        second_cid = (
            await client.post(
                "/intake/candidates",
                json=_payload(submission_id="acme-DIFFERENT", source_hint="another scan"),
            )
        ).json()["candidate"]["candidate_id"]
        resp = await client.post(
            "/intake/resolve", json={"candidate_id": second_cid, "action": "confirm"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ledger_outcome"] == "already-present"  # visible, not silent
    assert "already in the ledger" in body["message"].lower()
    assert len(_rows(intake.ledger_path)) == 1  # no second ledger row

    # The decision row records the honest outcome for the "M filed" count (H1).
    decisions = _rows(intake.decisions_path)
    assert [d["ledger_outcome"] for d in decisions] == ["stored", "already-present"]


# --- AC #9: reject leaves the ledger untouched --------------------------------


async def test_reject_records_decision_and_leaves_ledger(intake: IntakeHarness):
    async with _client(intake.app) as client:
        cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        resp = await client.post(
            "/intake/resolve",
            json={"candidate_id": cid, "action": "reject", "reject_reason": "personal"},
        )
    assert resp.status_code == 200
    assert resp.json()["standing"] == "rejected"
    assert _rows(intake.ledger_path) == []  # ledger untouched
    (decision,) = _rows(intake.decisions_path)
    assert decision["action"] == "reject" and decision["reject_reason"] == "personal"
    # The submission row + artifact remain on disk (append-only audit trail).
    assert len(_rows(intake.candidates_path)) == 1
    assert (intake.artifacts_dir / cid).exists()


# --- AC #10: resolve error cases ----------------------------------------------


async def test_resolve_unknown_candidate_is_404(intake: IntakeHarness):
    async with _client(intake.app) as client:
        resp = await client.post(
            "/intake/resolve", json={"candidate_id": "nope", "action": "confirm"}
        )
    assert resp.status_code == 404


async def test_resolve_already_decided_is_409(intake: IntakeHarness):
    async with _client(intake.app) as client:
        cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        await client.post(
            "/intake/resolve", json={"candidate_id": cid, "action": "reject"}
        )
        again = await client.post(
            "/intake/resolve", json={"candidate_id": cid, "action": "confirm"}
        )
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["action"] == "reject"  # its recorded outcome is returned


# --- AC #18: the C1 closed-period guard on the confirm path -------------------


async def test_confirm_into_closed_period_is_409(tmp_path, examples_dir):
    close_store = FileCloseStore(tmp_path / "closes.jsonl")
    # Seed a signed close for 2026-Q2 — the period the example date lands in.
    await close_store.record(
        CloseRecord(
            period="2026-Q2",
            signed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            signed_by="owner",
            checklist=(),
            transactions=(),
            tax={},
            reconciliation={},
            anomalies=(),
            effective_prior_period_state=None,
            config_prior_period_state=None,
        )
    )
    harness = _harness(tmp_path, examples_dir, close_store=close_store)
    async with _client(harness.app) as client:
        cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        resp = await client.post(
            "/intake/resolve", json={"candidate_id": cid, "action": "confirm"}
        )
    assert resp.status_code == 409
    assert _rows(harness.ledger_path) == []  # no ledger write
    assert _rows(harness.decisions_path) == []  # no decision row


async def test_confirm_edited_date_out_of_closed_period_succeeds(tmp_path, examples_dir):
    """The guard reads the EDITED date: editing out of the closed period is allowed."""
    close_store = FileCloseStore(tmp_path / "closes.jsonl")
    await close_store.record(
        CloseRecord(
            period="2026-Q2",
            signed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            signed_by="owner",
            checklist=(),
            transactions=(),
            tax={},
            reconciliation={},
            anomalies=(),
            effective_prior_period_state=None,
            config_prior_period_state=None,
        )
    )
    harness = _harness(tmp_path, examples_dir, close_store=close_store)
    async with _client(harness.app) as client:
        cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        resp = await client.post(
            "/intake/resolve",
            json={"candidate_id": cid, "action": "confirm", "date": "2026-08-01"},
        )
    assert resp.status_code == 200  # 2026-Q3 is open
    assert len(_rows(harness.ledger_path)) == 1


async def test_confirm_edited_INTO_closed_period_is_409(tmp_path, examples_dir):
    """The guard reads the EDITED date in the CLOSED direction too (pin 2): a candidate
    whose STORED date sits in an OPEN period, edited so the confirmed date lands in a
    CLOSED period, is refused. Distinguishes 'reads the edited date' from 'reads the
    stored date' — a guard that only checked the stored date would slip this through."""
    close_store = FileCloseStore(tmp_path / "closes.jsonl")
    # Close 2026-Q3 — NOT the candidate's own period (its date 2026-06-14 is 2026-Q2, open).
    await close_store.record(
        CloseRecord(
            period="2026-Q3",
            signed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            signed_by="owner",
            checklist=(),
            transactions=(),
            tax={},
            reconciliation={},
            anomalies=(),
            effective_prior_period_state=None,
            config_prior_period_state=None,
        )
    )
    harness = _harness(tmp_path, examples_dir, close_store=close_store)
    async with _client(harness.app) as client:
        cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        resp = await client.post(
            "/intake/resolve",
            json={"candidate_id": cid, "action": "confirm", "date": "2026-08-01"},
        )
    assert resp.status_code == 409  # 2026-08-01 → 2026-Q3 (closed)
    assert "2026-Q3" in json.dumps(resp.json())
    assert _rows(harness.ledger_path) == []  # no ledger write
    assert _rows(harness.decisions_path) == []  # no decision row


async def test_confirm_missing_artifact_is_404_and_writes_nothing(intake: IntakeHarness):
    """A confirm files the ledger row that carries the receipt bytes (the §1 source-trace
    link). If the artifact blob is lost between submit and confirm, coalescing to `b""`
    would file a row with NO artifact — undetectable downstream (transaction_key excludes
    artifact_bytes). Confirm 404s instead, writing nothing (pin 3)."""
    async with _client(intake.app) as client:
        cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        # Simulate the blob going missing between submit and confirm.
        (intake.artifacts_dir / cid).unlink()
        resp = await client.post(
            "/intake/resolve", json={"candidate_id": cid, "action": "confirm"}
        )
    assert resp.status_code == 404
    assert _rows(intake.ledger_path) == []  # no ledger row filed with empty bytes
    assert _rows(intake.decisions_path) == []  # no decision row


async def test_confirm_attribution_422_runs_before_closed_guard(tmp_path, examples_dir):
    """AC-18 sequencing (pin 15): the attribution check runs BEFORE the closed guard.
    A confirm into a closed period carrying an INVALID attribution target is a 422
    (attribution), not a 409 (closed) — proving the fixed order."""
    close_store = FileCloseStore(tmp_path / "closes.jsonl")
    await close_store.record(
        CloseRecord(
            period="2026-Q2",  # the candidate's own period is closed
            signed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            signed_by="owner",
            checklist=(),
            transactions=(),
            tax={},
            reconciliation={},
            anomalies=(),
            effective_prior_period_state=None,
            config_prior_period_state=None,
        )
    )
    harness = _harness(tmp_path, examples_dir, close_store=close_store)
    async with _client(harness.app) as client:
        cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        resp = await client.post(
            "/intake/resolve",
            json={
                "candidate_id": cid,
                "action": "confirm",
                "attribution_target_id": "nope",  # invalid → 422 before the 409 guard
            },
        )
    assert resp.status_code == 422  # attribution wins over the closed-period 409
    assert _rows(harness.ledger_path) == []
    assert _rows(harness.decisions_path) == []


async def test_confirm_blank_tax_coalesces_to_zero_like_submit(intake: IntakeHarness):
    """Pin 6: an explicit empty-string `tax` on confirm coalesces to Decimal('0') —
    matching the submit boundary (`tax not in (None, "")`), so the two paths never
    disagree on a cleared tax field. `amount` stays required (an empty string is a 422 on
    both paths), so the symmetry holds where it should and the required field is unchanged."""
    async with _client(intake.app) as client:
        blank_tax_cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        blank_tax = await client.post(
            "/intake/resolve",
            json={"candidate_id": blank_tax_cid, "action": "confirm", "tax": ""},
        )
        blank_amt_cid = (
            await client.post("/intake/candidates", json=_payload(submission_id="acme-2"))
        ).json()["candidate"]["candidate_id"]
        blank_amount = await client.post(
            "/intake/resolve",
            json={"candidate_id": blank_amt_cid, "action": "confirm", "amount": ""},
        )
    assert blank_tax.status_code == 200  # "" tax → 0, filed
    (row,) = _rows(intake.ledger_path)
    assert row["tax"] == "0"
    assert blank_amount.status_code == 422  # amount stays required — "" is a 422


# --- the shared queue projection ---------------------------------------------


async def test_list_candidates_reflects_standing_and_filters(intake: IntakeHarness):
    async with _client(intake.app) as client:
        pending_cid = (await client.post("/intake/candidates", json=_payload())).json()[
            "candidate"
        ]["candidate_id"]
        confirmed_cid = (
            await client.post(
                "/intake/candidates", json=_payload(submission_id="acme-2")
            )
        ).json()["candidate"]["candidate_id"]
        await client.post(
            "/intake/resolve", json={"candidate_id": confirmed_cid, "action": "confirm"}
        )

        all_q = (await client.get("/intake/candidates")).json()
        standings = {
            c["candidate"]["candidate_id"]: c["standing"] for c in all_q["candidates"]
        }
        assert standings[pending_cid] == "pending"
        assert standings[confirmed_cid] == "confirmed"

        confirmed_only = (
            await client.get("/intake/candidates", params={"status": "confirmed"})
        ).json()
        assert [c["candidate"]["candidate_id"] for c in confirmed_only["candidates"]] == [
            confirmed_cid
        ]

        bad = await client.get("/intake/candidates", params={"status": "bogus"})
    assert bad.status_code == 422


# --- 503 when the port is not wired (never a silent no-op) --------------------


async def test_intake_routes_503_when_unwired(tmp_path, examples_dir):
    harness = _harness(tmp_path, examples_dir, wire_intake=False)
    async with _client(harness.app) as client:
        resp = await client.post("/intake/candidates", json=_payload())
        listed = await client.get("/intake/candidates")
    assert resp.status_code == 503
    assert listed.status_code == 503
