"""The extraction-review UI (Slice 5 · B), end to end (httpx over the ASGI app).

Drives the HTML surface `register_ui` mounts on `create_app` — `GET /ui/intake`
(one editable review card per pending candidate) and `POST /ui/intake/resolve`
(the §5 human-confirm gate: confirm/correct/reject). Candidates are seeded through
the JSON `POST /intake/candidates` (issue A), so the queue reads the *same*
`build_intake_queue` projection both surfaces share. Covers the acceptance criteria
this issue owns: AC5 (the card, no confidence/category), AC16 (the label map),
AC17 (the reason line), AC18 (the closed-period UI refusal), AC22 (the terminal
swaps), plus the confirm/reject write roundtrips and honest dedupe.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper_ui.api import create_app
from bookkeeper_ui.candidates import (
    ACTION_CONFIRM,
    ACTION_REJECT,
    LEDGER_OUTCOME_ALREADY_PRESENT,
    LEDGER_OUTCOME_STORED,
    SOURCE_HUMAN,
    CandidateDecision,
    FileArtifactStore,
    FileCandidateDecisionStore,
    FileCandidateStore,
)
from bookkeeper_ui.views import count_filed_today
from bookkeeper_ui.closes import CloseRecord, FileCloseStore
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore

_ARTIFACT_BYTES = b"\xff\xd8\xff\x00 a small sample receipt jpeg \x01\x02\x03"
_LABELS = {"target-001": "Main Business", "target-002": "Side Project"}


@dataclass
class IntakeUiHarness:
    app: FastAPI
    ledger_path: Path
    candidates_path: Path
    decisions_path: Path
    artifacts_dir: Path


def _make(
    tmp_path: Path,
    examples_dir: Path,
    *,
    labels: dict[str, str] | None = None,
    close_store: FileCloseStore | None = None,
    wire_intake: bool = True,
) -> IntakeUiHarness:
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
        attribution_target_labels=labels,
    )
    return IntakeUiHarness(app, ledger_path, candidates_path, decisions_path, artifacts_dir)


@pytest.fixture
def intake_ui(tmp_path, examples_dir) -> IntakeUiHarness:
    return _make(tmp_path, examples_dir, labels=_LABELS)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _payload(**overrides) -> dict:
    payload = {
        "source": "acme-extractor",
        "submission_id": "acme-1",
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


async def _seed(client: httpx.AsyncClient, **overrides) -> str:
    """Submit a candidate through the JSON port; return its candidate_id."""
    resp = await client.post("/intake/candidates", json=_payload(**overrides))
    return resp.json()["candidate"]["candidate_id"]


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- AC 5: the card — editable fields + inline artifact, NO confidence/category ---


async def test_intake_card_renders_editable_fields_and_artifact(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        cid = await _seed(client)
        resp = await client.get("/ui/intake")
    assert resp.status_code == 200
    html = resp.text
    # One card, self-referencing for the outerHTML swap.
    assert f'id="card-{cid}"' in html
    assert f'hx-target="#card-{cid}"' in html
    # The inline artifact ALONGSIDE the fields, with a meaningful alt.
    assert f'src="/intake/artifact/{cid}"' in html
    assert 'alt="receipt from Home Depot"' in html
    # Every extracted field editable + pre-filled verbatim.
    assert 'name="vendor" value="Home Depot"' in html
    assert 'name="amount" value="82.50"' in html
    assert 'name="tax" value="10.73"' in html
    assert 'name="description" value="Lumber and fasteners"' in html


async def test_money_inputs_are_text_never_number(intake_ui: IntakeUiHarness):
    """AC5: money is type=text (type=number mangles "82.50"/trailing zeros)."""
    async with _client(intake_ui.app) as client:
        # A trailing-zero amount must survive verbatim in the pre-filled value.
        await _seed(client, amount="45.990", tax="0.500")
        html = (await client.get("/ui/intake")).text
    assert 'type="number"' not in html
    assert 'name="amount" value="45.990"' in html
    assert 'name="tax" value="0.500"' in html


async def test_card_has_no_confidence_or_category_element(intake_ui: IntakeUiHarness):
    """AC5: no confidence and no proposed-category/account element anywhere."""
    async with _client(intake_ui.app) as client:
        await _seed(client)
        html = (await client.get("/ui/intake")).text
    assert "confiden" not in html.lower()  # no confidence bar/score/chip
    # No chart-of-accounts category picker on the card (that is the downstream queue).
    assert "5000-office-supplies" not in html
    assert "chart_of_accounts" not in html


# --- AC 16: the label map (render + fallback + pre-select on the id) --------------


async def test_label_map_renders_human_labels(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        await _seed(client)
        html = (await client.get("/ui/intake")).text
    # The <option value> is the id; the visible text is the human label.
    assert '<option value="target-001" selected>Main Business</option>' in html
    assert '<option value="target-002">Side Project</option>' in html


async def test_without_labels_select_shows_raw_ids(tmp_path, examples_dir):
    harness = _make(tmp_path, examples_dir, labels=None)
    async with _client(harness.app) as client:
        await _seed(client)
        html = (await client.get("/ui/intake")).text
    # Fallback: the raw id is both the value and the visible text.
    assert '<option value="target-001" selected>target-001</option>' in html
    assert "Main Business" not in html


async def test_preselect_is_on_the_id_not_the_label(intake_ui: IntakeUiHarness):
    """AC16: a candidate carrying target-001 pre-selects the target-001 option."""
    async with _client(intake_ui.app) as client:
        await _seed(client, attribution_target_id="target-002")
        html = (await client.get("/ui/intake")).text
    assert '<option value="target-002" selected>Side Project</option>' in html
    assert '<option value="target-001" selected>' not in html


async def test_null_attribution_shows_choose_placeholder(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        await _seed(client, attribution_target_id=None)
        html = (await client.get("/ui/intake")).text
    assert 'value="" selected disabled' in html  # the "choose…" placeholder
    assert '<option value="target-001" selected>' not in html


# --- AC 17: the reason line (conditional, source-aware, null-suppressed) ----------


async def test_reason_line_generic_for_non_receipt_intake(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        await _seed(client, source="acme-extractor", source_hint="site materials")
        html = (await client.get("/ui/intake")).text
    assert "suggested" in html and "site materials" in html
    assert "from the email subject" not in html


async def test_reason_line_warmer_for_receipt_intake(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        await _seed(client, source="receipt-intake", source_hint="Office chair")
        html = (await client.get("/ui/intake")).text
    assert "from the email subject: 'Office chair'" in html


async def test_null_attribution_shows_no_reason_line(intake_ui: IntakeUiHarness):
    """AC17: a null-attribution candidate whose source_hint carries a failure reason
    shows NO reason line (never mis-rendered as attribution provenance)."""
    async with _client(intake_ui.app) as client:
        await _seed(
            client, attribution_target_id=None, source_hint="extract failed: blurry scan"
        )
        html = (await client.get("/ui/intake")).text
    assert "suggested" not in html
    assert "from the email subject" not in html
    # The failure reason is not surfaced as a suggestion reason.
    assert "extract failed" not in html.split('class="trail')[0]


# --- AC 18: the closed-period UI refusal (edited date; stores untouched) ----------


async def _closed_q2(tmp_path: Path) -> FileCloseStore:
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
    return close_store


async def test_confirm_into_closed_period_is_refused_into_the_card(tmp_path, examples_dir):
    close_store = await _closed_q2(tmp_path)
    harness = _make(tmp_path, examples_dir, labels=_LABELS, close_store=close_store)
    async with _client(harness.app) as client:
        cid = await _seed(client)  # date 2026-06-14 → 2026-Q2 (closed)
        resp = await client.post(
            "/ui/intake/resolve",
            data={
                "candidate_id": cid,
                "action": "confirm",
                "vendor": "Home Depot",
                "amount": "82.50",
                "tax": "10.73",
                "date": "2026-06-14",
                "attribution_target_id": "target-001",
                "period": "2026-Q2",
            },
        )
    assert resp.status_code == 200
    assert "Period 2026-Q2 is closed" in resp.text
    # No ledger write, no decision row, counter untouched (no OOB span).
    assert _rows(harness.ledger_path) == []
    assert _rows(harness.decisions_path) == []
    assert "intake-pending-count" not in resp.text


async def test_confirm_edited_out_of_closed_period_succeeds(tmp_path, examples_dir):
    """The guard reads the EDITED date: editing out of the closed period is allowed."""
    close_store = await _closed_q2(tmp_path)
    harness = _make(tmp_path, examples_dir, labels=_LABELS, close_store=close_store)
    async with _client(harness.app) as client:
        cid = await _seed(client)
        resp = await client.post(
            "/ui/intake/resolve",
            data={
                "candidate_id": cid,
                "action": "confirm",
                "vendor": "Home Depot",
                "amount": "82.50",
                "tax": "10.73",
                "date": "2026-08-01",  # 2026-Q3 is open
                "attribution_target_id": "target-001",
                "period": "2026-Q2",
            },
        )
    assert resp.status_code == 200
    assert "Confirmed and filed" in resp.text
    assert len(_rows(harness.ledger_path)) == 1


# --- AC 22: the terminal-decision swaps -------------------------------------------


async def test_resolve_already_decided_swaps_already_reviewed(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        cid = await _seed(client)
        await client.post(
            "/ui/intake/resolve",
            data={"candidate_id": cid, "action": "reject", "reject_reason": "dup", "period": "2026-Q2"},
        )
        again = await client.post(
            "/ui/intake/resolve",
            data={
                "candidate_id": cid,
                "action": "confirm",
                "vendor": "Home Depot",
                "amount": "82.50",
                "date": "2026-06-14",
                "attribution_target_id": "target-001",
                "period": "2026-Q2",
            },
        )
    assert again.status_code == 200
    assert "Already reviewed" in again.text
    # No second decision row appended past the terminal state.
    assert len(_rows(intake_ui.decisions_path)) == 1


async def test_resolve_unknown_candidate_swaps_gone(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        resp = await client.post(
            "/ui/intake/resolve",
            data={
                "candidate_id": "0" * 64,
                "action": "confirm",
                "vendor": "x",
                "amount": "1.00",
                "date": "2026-06-14",
                "attribution_target_id": "target-001",
                "period": "2026-Q2",
            },
        )
    assert resp.status_code == 200
    assert "No longer available" in resp.text


# --- confirm/reject write roundtrips ----------------------------------------------


async def test_confirm_files_transaction_with_edits_artifact_and_decision_row(
    intake_ui: IntakeUiHarness,
):
    async with _client(intake_ui.app) as client:
        cid = await _seed(client)
        resp = await client.post(
            "/ui/intake/resolve",
            data={
                "candidate_id": cid,
                "action": "confirm",
                "vendor": "Home Depot Inc",  # a correction
                "amount": "80.00",  # a correction
                "tax": "10.73",
                "date": "2026-06-14",
                "description": "Lumber",
                "attribution_target_id": "target-002",  # a correction
                "period": "2026-Q2",
            },
        )
        assert resp.status_code == 200
        assert "Confirmed and filed" in resp.text
        # Both OOB counters recompute off the projection (innerHTML strategy so the
        # counter's aria-live region + focus target survive the swap).
        assert 'id="intake-pending-count" hx-swap-oob="innerHTML">0</span>' in resp.text
        assert 'id="intake-pulse-count" hx-swap-oob="innerHTML">0</span>' in resp.text
        # The confirmed transaction lands in the ledger for its period with the edits.
        ledger = (await client.get("/ledger", params={"period": "2026-Q2"})).json()
        entry = next(
            e for e in ledger["entries"] if e["transaction"]["vendor"] == "Home Depot Inc"
        )
        assert entry["transaction"]["amount"] == "80.00"
        assert entry["transaction"]["attribution_target_id"] == "target-002"

    # The raw artifact bytes rode artifact_bytes into the ledger row.
    ledger_row = _rows(intake_ui.ledger_path)[0]
    assert base64.b64decode(ledger_row["artifact_bytes"]) == _ARTIFACT_BYTES
    # The decision row carries the ledger link + honest-dedupe outcome. The recorded
    # transaction_key equals the ledger entry's id (= transaction_key).
    decision = _rows(intake_ui.decisions_path)[0]
    assert decision["action"] == "confirm"
    assert decision["ledger_outcome"] == "stored"
    assert decision["transaction_key"] == entry["transaction"]["id"]


async def test_reject_records_decision_and_leaves_ledger_untouched(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        cid = await _seed(client)
        resp = await client.post(
            "/ui/intake/resolve",
            data={
                "candidate_id": cid,
                "action": "reject",
                "reject_reason": "not a business expense",
                "period": "2026-Q2",
            },
        )
    assert resp.status_code == 200
    assert "Rejected" in resp.text
    assert _rows(intake_ui.ledger_path) == []  # ledger untouched
    decision = _rows(intake_ui.decisions_path)[0]
    assert decision["action"] == "reject"
    assert decision["reject_reason"] == "not a business expense"


async def test_confirm_duplicate_is_visible_already_present(intake_ui: IntakeUiHarness):
    """Honest dedupe: two candidates with identical business fields — the second
    confirm no-ops the ledger but says so (already-present), never a silent filing."""
    async with _client(intake_ui.app) as client:
        cid1 = await _seed(client, submission_id="a")
        cid2 = await _seed(client, submission_id="b")  # same business fields
        common = {
            "action": "confirm",
            "vendor": "Home Depot",
            "amount": "82.50",
            "tax": "10.73",
            "date": "2026-06-14",
            "description": "Lumber and fasteners",
            "attribution_target_id": "target-001",
            "period": "2026-Q2",
        }
        await client.post("/ui/intake/resolve", data={"candidate_id": cid1, **common})
        second = await client.post(
            "/ui/intake/resolve", data={"candidate_id": cid2, **common}
        )
    assert "already in the ledger" in second.text
    assert len(_rows(intake_ui.ledger_path)) == 1  # only one ledger row
    outcomes = [d["ledger_outcome"] for d in _rows(intake_ui.decisions_path)]
    assert outcomes == ["stored", "already-present"]


# --- the defensive attribution 422 + validation error -----------------------------


async def test_confirm_invalid_attribution_is_422(intake_ui: IntakeUiHarness):
    """Defensive 422 (the <select> offers only valid ids), mirroring §5.2."""
    async with _client(intake_ui.app) as client:
        cid = await _seed(client)
        resp = await client.post(
            "/ui/intake/resolve",
            data={
                "candidate_id": cid,
                "action": "confirm",
                "vendor": "Home Depot",
                "amount": "82.50",
                "date": "2026-06-14",
                "attribution_target_id": "target-999",  # not in config
                "period": "2026-Q2",
            },
        )
    assert resp.status_code == 422
    assert _rows(intake_ui.ledger_path) == []
    assert _rows(intake_ui.decisions_path) == []


async def test_confirm_bad_money_renders_error_keeps_edits_writes_nothing(
    intake_ui: IntakeUiHarness,
):
    async with _client(intake_ui.app) as client:
        cid = await _seed(client)
        resp = await client.post(
            "/ui/intake/resolve",
            data={
                "candidate_id": cid,
                "action": "confirm",
                "vendor": "HD corrected",
                "amount": "not-a-number",
                "tax": "10.73",
                "date": "2026-06-14",
                "attribution_target_id": "target-001",
                "period": "2026-Q2",
            },
        )
    assert resp.status_code == 200
    # The card is re-rendered (not removed) with the edits kept + the failure named.
    assert f'id="card-{cid}"' in resp.text
    assert 'value="HD corrected"' in resp.text
    assert 'value="not-a-number"' in resp.text
    assert "not a valid amount" in resp.text
    # Counter untouched (no OOB span), nothing written.
    assert "intake-pending-count" not in resp.text
    assert _rows(intake_ui.ledger_path) == []
    assert _rows(intake_ui.decisions_path) == []


# --- the shared projection: only pending candidates render ------------------------


async def test_queue_shows_only_pending_candidates(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        pending = await _seed(client, submission_id="p", vendor="Pending Vendor")
        decided = await _seed(client, submission_id="d", vendor="Decided Vendor")
        await client.post(
            "/ui/intake/resolve",
            data={"candidate_id": decided, "action": "reject", "reject_reason": "x", "period": "2026-Q2"},
        )
        html = (await client.get("/ui/intake")).text
    assert f"card-{pending}" in html
    assert f"card-{decided}" not in html
    assert "Pending Vendor" in html
    assert "Decided Vendor" not in html


async def test_empty_queue_renders_empty_state(intake_ui: IntakeUiHarness):
    async with _client(intake_ui.app) as client:
        html = (await client.get("/ui/intake")).text
    assert "Nothing to review" in html


async def test_intake_queue_reads_examples_config_labels(tmp_path, examples_dir):
    """The committed example config's labels flow through build_app_from_env-style
    reads — here injected — so the demo path shows human labels, not raw ids."""
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    harness = _make(
        tmp_path, examples_dir, labels=data.get("attribution_target_labels")
    )
    async with _client(harness.app) as client:
        await _seed(client)
        html = (await client.get("/ui/intake")).text
    assert data["attribution_target_labels"]["target-001"] in html


# ===========================================================================
# Slice 5 · B+: the capture-home landing (GET / re-homed to the intake queue)
# ===========================================================================


async def test_capture_home_renders_hero_queue_with_pending_card(
    intake_ui: IntakeUiHarness,
):
    """AC 14: `GET /` renders the hero review queue — one card per pending candidate
    off build_intake_queue(status='pending') — with the honest 'N to review' pulse and
    NO confidence / completeness % / category anywhere on the landing."""
    async with _client(intake_ui.app) as client:
        cid = await _seed(client)
        home = (await client.get("/")).text
    # Zone A — the pulse's call-to-action count is the pending count.
    assert 'id="intake-pulse-count" class="pulse-count">1</span>' in home
    assert "to review" in home
    # The hero card for the pending candidate, in the focus-managed queue.
    assert f'id="card-{cid}"' in home
    assert 'id="intake-queue"' in home
    # No fabricated trust: no confidence, no completeness %, no category picker.
    assert "confiden" not in home.lower()
    assert "% complete" not in home
    assert "chart_of_accounts" not in home
    assert 'name="account"' not in home


async def test_capture_home_shows_only_pending_never_rejected_or_confirmed(
    intake_ui: IntakeUiHarness,
):
    """AC 14: the landing hero renders only PENDING candidates — a rejected/confirmed
    candidate never appears (it reads build_intake_queue(status='pending'))."""
    async with _client(intake_ui.app) as client:
        pending = await _seed(client, submission_id="p", vendor="Pending Vendor")
        rejected = await _seed(client, submission_id="r", vendor="Rejected Vendor")
        await client.post(
            "/ui/intake/resolve",
            data={"candidate_id": rejected, "action": "reject", "period": "2026-Q2"},
        )
        home = (await client.get("/")).text
    assert f"card-{pending}" in home and "Pending Vendor" in home
    assert f"card-{rejected}" not in home and "Rejected Vendor" not in home


async def test_capture_home_win_state_when_queue_clear(intake_ui: IntakeUiHarness):
    """AC 19 (H3): at zero pending the queue zone reads exactly 'All receipts
    reviewed' (NOT 'all caught up'), pairs the day's stored tally, and promotes
    categorize to the primary next action — only here, at queue-clear."""
    async with _client(intake_ui.app) as client:
        home = (await client.get("/")).text  # nothing seeded → nothing pending
    assert "All receipts reviewed" in home
    assert "all caught up" not in home.lower()  # must not overclaim
    assert "filed today" in home  # the tally is paired always
    # Categorize promoted to the primary next action (a button) at queue-clear.
    assert 'class="button" href="/ui/queue?period=' in home


async def test_capture_home_pulse_counts_stored_confirms_only(
    intake_ui: IntakeUiHarness,
):
    """AC 15 (honest pulse): 'M filed today' counts only action=='confirm' AND
    ledger_outcome=='stored' — a dedupe no-op (already-present) does NOT inflate it."""
    async with _client(intake_ui.app) as client:
        cid1 = await _seed(client, submission_id="a")
        cid2 = await _seed(client, submission_id="b")  # identical business fields
        common = {
            "action": "confirm",
            "vendor": "Home Depot",
            "amount": "82.50",
            "tax": "10.73",
            "date": "2026-06-14",
            "description": "Lumber and fasteners",
            "attribution_target_id": "target-001",
            "period": "2026-Q2",
        }
        await client.post("/ui/intake/resolve", data={"candidate_id": cid1, **common})
        await client.post("/ui/intake/resolve", data={"candidate_id": cid2, **common})
        home = (await client.get("/")).text
    # One row actually filed; the dedupe no-op is NOT counted (1, never 2).
    assert '<span class="pulse-filed">1</span>' in home
    assert '<span class="pulse-filed">2</span>' not in home


async def test_capture_home_no_period_on_receipts_link(intake_ui: IntakeUiHarness):
    """AC 14: the nav lists 'Receipts' first and its link carries NO ?period= (the
    intake queue is all-periods); every other link keeps threading the period."""
    async with _client(intake_ui.app) as client:
        home = (await client.get("/")).text
    assert '<a href="/">Receipts</a>' in home
    assert '<a href="/?period=' not in home  # the Receipts link is period-agnostic
    assert '/ui/package?period=' in home  # non-Receipts links keep the period


# --- count_filed_today: the honesty rules, unit-tested off the pure helper ---------


def _stored_confirm(decided_at: datetime) -> CandidateDecision:
    return CandidateDecision(
        candidate_id="c",
        action=ACTION_CONFIRM,
        source=SOURCE_HUMAN,
        decided_at=decided_at,
        ledger_outcome=LEDGER_OUTCOME_STORED,
    )


def test_count_filed_today_counts_stored_confirms_today():
    now = datetime.now(timezone.utc)
    today = datetime.now().date()
    assert count_filed_today([_stored_confirm(now), _stored_confirm(now)], today=today) == 2


def test_count_filed_today_excludes_dedupe_noops_and_rejects():
    now = datetime.now(timezone.utc)
    today = datetime.now().date()
    decisions = [
        _stored_confirm(now),
        CandidateDecision(  # dedupe no-op — a decision, but no fresh filing
            candidate_id="d", action=ACTION_CONFIRM, source=SOURCE_HUMAN,
            decided_at=now, ledger_outcome=LEDGER_OUTCOME_ALREADY_PRESENT,
        ),
        CandidateDecision(  # reject — the ledger is untouched
            candidate_id="e", action=ACTION_REJECT, source=SOURCE_HUMAN, decided_at=now,
        ),
    ]
    assert count_filed_today(decisions, today=today) == 1


def test_count_filed_today_excludes_other_days():
    today = datetime.now().date()
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    assert count_filed_today([_stored_confirm(yesterday)], today=today) == 0


def test_count_filed_today_buckets_by_server_local_day_not_utc():
    """The day boundary is the deployment-LOCAL day: a stored confirm at 7pm local
    counts as today even though its stored UTC instant may fall on a different date.
    Round-trips a 7pm-local wall time through UTC (how the app stores decided_at)."""
    local_now = datetime.now().astimezone()  # server-local, tz-aware
    today = local_now.date()
    seven_pm_local = local_now.replace(hour=19, minute=0, second=0, microsecond=0)
    decided_as_stored = seven_pm_local.astimezone(timezone.utc)  # the app stores UTC
    assert count_filed_today([_stored_confirm(decided_as_stored)], today=today) == 1
