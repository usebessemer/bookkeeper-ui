"""Slice 3 · E — the close-review UI: the screen, the htmx write twins, closed banners.

Drives the HTML surface `register_ui` mounts on `create_app` (httpx over ASGI, the
Slice 1/2 style) with injected temp-path stores and the committed sample config —

- `GET /ui/close` renders the SAME `views.build_close_review` projection the JSON
  `GET /close` serializes (one projection, asserted identical after each mutation);
- the framework checklist + blockers render VERBATIM, visually separated from the
  app-policy gates;
- the acknowledge / waive / sign htmx twins render 2xx partials (refusal-into-page
  on a bad write), the server re-verifying + guarding exactly as the C/D JSON twins;
- the queue / ledger / import / reconcile screens carry the closed banner and
  suppress the resolve/confirm controls for a closed period, the server guard still
  refusing a forged write.

The framework skills are called as-is; the app writes only through its own stores.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from markupsafe import escape

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.flag_anomaly import flag_anomaly

from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore, derive_flag_id
from bookkeeper_ui.api import create_app
from bookkeeper_ui.closes import FileCloseStore
from bookkeeper_ui.confirmations import (
    SOURCE_HUMAN,
    Confirmation,
    FileConfirmationStore,
)
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore
from bookkeeper_ui.waivers import FileWaiverStore, Waiver
from tests.conftest import make_stmt_line, make_txn

PERIOD = "2026-Q2"
AT = datetime(2026, 7, 1, tzinfo=timezone.utc)
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "bookkeeper_ui" / "templates"


# --- Harness + builders -----------------------------------------------------


@dataclass
class Harness:
    app: FastAPI
    config: BookkeeperConfig
    tmp: Path
    ledger_store: FileLedgerStore
    confirmation_store: FileConfirmationStore
    statement_store: FileStatementStore
    reconciliation_store: FileReconciliationStore
    close_store: FileCloseStore
    anomaly_review_store: FileAnomalyReviewStore
    waiver_store: FileWaiverStore

    def confirmations_path(self) -> Path:
        return self.tmp / "confirmations.jsonl"


def _config(examples_dir: Path, **overrides: object) -> BookkeeperConfig:
    """The shipped example config (+ overrides). `drop_materiality` makes the size
    check inert; `materiality_floor` overrides the floor."""
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    if "materiality_floor" in overrides:
        data["materiality_floor"] = overrides["materiality_floor"]
    if overrides.get("drop_materiality"):
        data.pop("materiality_floor", None)
    return BookkeeperConfig.from_mapping(data)


def _harness(examples_dir: Path, tmp_path: Path, config: BookkeeperConfig | None = None) -> Harness:
    config = config or _config(examples_dir)
    ledger_store = FileLedgerStore(tmp_path / "ledger.jsonl")
    confirmation_store = FileConfirmationStore(tmp_path / "confirmations.jsonl")
    statement_store = FileStatementStore(tmp_path / "statements.jsonl")
    reconciliation_store = FileReconciliationStore(tmp_path / "reconciliations.jsonl")
    close_store = FileCloseStore(tmp_path / "closes.jsonl")
    anomaly_review_store = FileAnomalyReviewStore(tmp_path / "anomaly_reviews.jsonl")
    waiver_store = FileWaiverStore(tmp_path / "reconciliation_waivers.jsonl")
    app = create_app(
        config=config,
        ledger_store=ledger_store,
        confirmation_store=confirmation_store,
        statement_store=statement_store,
        reconciliation_store=reconciliation_store,
        close_store=close_store,
        anomaly_review_store=anomaly_review_store,
        waiver_store=waiver_store,
    )
    return Harness(
        app, config, tmp_path, ledger_store, confirmation_store, statement_store,
        reconciliation_store, close_store, anomaly_review_store, waiver_store,
    )


@pytest.fixture
def harness(examples_dir, tmp_path) -> Harness:
    return _harness(examples_dir, tmp_path)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _delta_1200() -> object:
    """Delta 1200 — owner-rule proposal (5200) + an over_materiality flag (floor 1000)."""
    return make_txn(vendor="Delta Airlines", amount="1200.00", tax="0",
                    date=datetime(2026, 5, 2), description="Flight")


def _flagged() -> object:
    """Below the categorize floor → FLAGGED (blocks categorization_complete)."""
    return make_txn(vendor="Zzxq Gibberish", amount="40.00", tax="5.20",
                    date=datetime(2026, 5, 2), description="")


async def _make_signable(h: Harness, period: str = PERIOD) -> object:
    """The minimal green close via the waiver path — AWS confirmed + a waiver.

    AWS 50 (owner-rule → 5100, under the 1000 floor so no anomaly) confirmed to its
    proposed account, plus a reconciliation waiver (no statement). Framework READY +
    all three app gates met → signable.
    """
    txn = make_txn(vendor="AWS", amount="50.00", tax="6.50", date=datetime(2026, 5, 1), description="cloud")
    await h.ledger_store.store(txn)
    await h.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(txn), account="5100-software-subscriptions",
                     source=SOURCE_HUMAN, decided_at=AT)
    )
    await h.waiver_store.record(Waiver(period=period, waived_at=AT, waived_by="human", note="no feed"))
    return txn


# ============================================================================
# AC2 — one projection: GET /close (JSON) and GET /ui/close (HTML) agree, end to end
# ============================================================================


async def test_one_projection_json_and_html_agree_through_the_lifecycle(harness: Harness):
    """AC2: review → acknowledge → waive → confirm → sign → closed — at each step the
    JSON `/close` and the HTML `/ui/close` render the same state, both off
    `build_close_review` only."""
    delta = _delta_1200()
    await harness.ledger_store.store(delta)

    async with _client(harness.app) as client:
        # 1. Review — blocked (Delta is a proposal, not confirmed; no statement/waiver;
        #    anomaly unacknowledged). JSON not signable ⇔ HTML sign button disabled.
        j = (await client.get("/close", params={"period": PERIOD})).json()
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
        assert j["signable"] is False
        assert 'class="sign-button" disabled' in html
        anomaly = next(a for a in j["anomalies"] if a["kind"] == "over_materiality")
        assert anomaly["acknowledged"] is False
        assert "over_materiality" in html

        # 2. Acknowledge the anomaly (UI twin) → both surfaces show it acknowledged.
        await client.post("/ui/anomalies/review", data={"flag_id": anomaly["id"], "period": PERIOD, "note": "expected"})
        j = (await client.get("/close", params={"period": PERIOD})).json()
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
        assert j["app_gates"]["anomalies_reviewed"]["met"] is True
        assert "status-acknowledged" in html

        # 3. Waive reconciliation (UI twin) → gate C met on both.
        await client.post("/ui/reconciliation/waive", data={"period": PERIOD, "note": "no feed"})
        j = (await client.get("/close", params={"period": PERIOD})).json()
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
        assert j["reconciliation_source"] == "waived"
        assert "reconciliation waived for this period" in html

        # 4. Confirm the category (gate A) → now signable on both surfaces.
        await client.post("/ui/resolve", data={"transaction_id": transaction_key(delta), "account": "5200-travel", "period": PERIOD})
        j = (await client.get("/close", params={"period": PERIOD})).json()
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
        assert j["signable"] is True
        assert "disabled" not in html.split("sign-button")[1][:30]  # button enabled

        # 5. Sign (UI twin) → closed on both surfaces, same signer/snapshot.
        await client.post("/ui/sign", data={"period": PERIOD, "signed_by": "Stu"})
        j = (await client.get("/close", params={"period": PERIOD})).json()
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
        assert j["closed"] is True
        assert j["close_record"]["signed_by"] == "Stu"
        assert "signed closed" in html and "Stu" in html


# ============================================================================
# AC3 — checklist fidelity (rendered): the five checks + blockers + reasons verbatim
# ============================================================================


async def test_blocked_period_renders_all_five_checks_and_blocker_verbatim(harness: Harness):
    """AC3: a BLOCKED period renders `blocked`, all five framework checks (in order),
    and every blocker's framework reason string verbatim."""
    await harness.ledger_store.store(_flagged())
    await harness.waiver_store.record(Waiver(period=PERIOD, waived_at=AT, waived_by="human", note=""))

    async with _client(harness.app) as client:
        j = (await client.get("/close", params={"period": PERIOD})).json()
        html = (await client.get("/ui/close", params={"period": PERIOD})).text

    assert j["framework"]["status"] == "blocked"
    assert 'class="tag status-blocked">blocked' in html
    # All five checks, by name, appear.
    for name in ("period_closeable", "period_coherent", "reconciliation_clean",
                 "categorization_complete", "tax_clean"):
        assert name in html
    # Each check's framework reason renders verbatim (HTML-escaped, as Jinja does).
    for c in j["framework"]["checklist"]:
        assert str(escape(c["reason"])) in html
    # The categorization blocker's framework reason + its underlying flag render verbatim.
    cat_blocker = next(b for b in j["framework"]["blockers"] if b["check"] == "categorization_complete")
    assert str(escape(cat_blocker["reason"])) in html
    assert "Zzxq Gibberish" in html  # the underlying flagged transaction


async def test_ready_only_when_composition_ready(harness: Harness):
    """AC3: the screen renders READY only when the composition returned READY."""
    await _make_signable(harness)
    async with _client(harness.app) as client:
        j = (await client.get("/close", params={"period": PERIOD})).json()
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
    assert j["framework"]["status"] == "ready"
    assert 'class="tag status-ready">ready' in html


# ============================================================================
# AC4 — app-policy separation: the framework checklist markup carries only the five
# ============================================================================


async def test_framework_checklist_markup_carries_only_the_five_checks(harness: Harness):
    """AC4: the `framework-checklist` list holds exactly the five framework checks and
    none of the app gates; the app gates render under their own "App policy" label."""
    await harness.ledger_store.store(_delta_1200())
    async with _client(harness.app) as client:
        html = (await client.get("/ui/close", params={"period": PERIOD})).text

    checklist = re.search(r'<ul class="framework-checklist">(.*?)</ul>', html, re.DOTALL)
    assert checklist is not None
    block = checklist.group(1)
    # Exactly the five framework check names, and nothing else check-shaped.
    names = re.findall(r'class="tag check-name">([a-z_]+)<', block)
    assert names == [
        "period_closeable", "period_coherent", "reconciliation_clean",
        "categorization_complete", "tax_clean",
    ]
    # The app-policy gate names never leak into the framework checklist block.
    for gate in ("all confirmed", "anomalies reviewed", "statement or waiver"):
        assert gate not in block
    # The app gates live under their own labeled section, distinct from the checklist.
    assert "App policy" in html
    assert "all confirmed" in html and "anomalies reviewed" in html


# ============================================================================
# AC5 — anomaly acknowledge (UI)
# ============================================================================


async def test_anomaly_ack_reacknowledges_and_rerenders_card(harness: Harness):
    """AC5: `POST /ui/anomalies/review` acknowledges the flag and re-renders the card as
    acknowledged (the htmx outerHTML twin of the card)."""
    await harness.ledger_store.store(_delta_1200())
    async with _client(harness.app) as client:
        j = (await client.get("/close", params={"period": PERIOD})).json()
        anomaly = next(a for a in j["anomalies"] if a["kind"] == "over_materiality")
        resp = await client.post("/ui/anomalies/review", data={"flag_id": anomaly["id"], "period": PERIOD, "note": "reviewed"})
    assert resp.status_code == 200
    assert f'id="anomaly-{anomaly["id"]}"' in resp.text
    assert "status-acknowledged" in resp.text
    assert "reviewed" in resp.text  # the note on the trail
    assert 'hx-post="/ui/anomalies/review"' not in resp.text  # no ack form once acknowledged
    rows = await harness.anomaly_review_store.all()
    assert len(rows) == 1 and rows[0].flag_id == anomaly["id"]


async def test_anomaly_ack_non_current_flag_renders_refusal_not_500(harness: Harness):
    """AC5: an ack for a `flag_id` matching no current flag renders the refusal partial
    (a 200), not a 500 — and writes nothing."""
    await harness.ledger_store.store(_delta_1200())
    async with _client(harness.app) as client:
        resp = await client.post("/ui/anomalies/review", data={"flag_id": "not-a-real-flag", "period": PERIOD})
    assert resp.status_code == 200
    assert "Refused" in resp.text and "matches no current anomaly" in resp.text
    assert not (harness.tmp / "anomaly_reviews.jsonl").exists()


async def test_anomaly_ack_on_closed_period_renders_refusal(harness: Harness):
    """AC5 / §5.7: an ack on a closed period is refused into the page (dispositions frozen)."""
    txn = await _make_signable(harness)
    assert txn
    async with _client(harness.app) as client:
        await client.post("/ui/sign", data={"period": PERIOD})
        resp = await client.post("/ui/anomalies/review", data={"flag_id": "anything", "period": PERIOD})
    assert resp.status_code == 200
    assert "Refused" in resp.text and "closed" in resp.text


# ============================================================================
# AC6 — waiver (UI)
# ============================================================================


async def test_waiver_control_shows_only_when_missing_and_rerenders_waived(harness: Harness):
    """AC6: the waive control shows only when `reconciliation_source == "missing"`;
    waiving re-renders the gate as waived (never "reconciled")."""
    await harness.ledger_store.store(_delta_1200())
    async with _client(harness.app) as client:
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
        assert 'hx-post="/ui/reconciliation/waive"' in html  # control present when missing

        resp = await client.post("/ui/reconciliation/waive", data={"period": PERIOD, "note": "no feed"})
    assert resp.status_code == 200
    assert 'id="reconciliation-gate"' in resp.text
    assert "waived" in resp.text
    assert "reconciled against the imported statement" not in resp.text  # never "reconciled"
    assert 'hx-post="/ui/reconciliation/waive"' not in resp.text  # control gone once waived
    rows = await harness.waiver_store.all()
    assert len(rows) == 1


async def test_waiver_control_absent_and_post_refused_when_statement_present(harness: Harness):
    """AC6: with a statement present the control is absent, and a forged
    `POST /ui/reconciliation/waive` renders the refusal (never waivable)."""
    await harness.ledger_store.store(_delta_1200())
    await harness.statement_store.store(make_stmt_line(statement_ref="S-1", amount="10.00", date=datetime(2026, 5, 2)))
    async with _client(harness.app) as client:
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
        assert 'hx-post="/ui/reconciliation/waive"' not in html  # control absent
        assert "reconciled against the imported statement" in html  # source == statement

        resp = await client.post("/ui/reconciliation/waive", data={"period": PERIOD})
    assert resp.status_code == 200
    assert "Refused" in resp.text and "statement on file" in resp.text
    assert not (harness.tmp / "reconciliation_waivers.jsonl").exists()


# ============================================================================
# AC7 — sign (UI)
# ============================================================================


async def test_sign_form_enabled_only_when_signable_and_signs(harness: Harness):
    """AC7: the SIGN form is enabled only when signable; signing a signable period
    renders the signed close and writes exactly one record."""
    await _make_signable(harness)
    async with _client(harness.app) as client:
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
        assert 'class="sign-button">' in html  # enabled (no disabled attr)

        resp = await client.post("/ui/sign", data={"period": PERIOD, "signed_by": "Stu"})
    assert resp.status_code == 200
    assert "signed closed" in resp.text and "Stu" in resp.text
    assert 'id="close-screen"' in resp.text  # replaces the whole screen
    rows = await harness.close_store.all()
    assert len(rows) == 1 and rows[0].signed_by == "Stu"


async def test_sign_non_signable_renders_refusal_enumerating_gates(harness: Harness):
    """AC7: signing a not-signable period renders the refusal enumerating the failed
    gates (server-enforced), and writes no close record."""
    await harness.ledger_store.store(_delta_1200())  # proposal (pending), anomaly unacked, no statement
    async with _client(harness.app) as client:
        resp = await client.post("/ui/sign", data={"period": PERIOD})
    assert resp.status_code == 200
    assert "not ready to sign" in resp.text
    # It enumerates the specific unmet app-policy gates.
    assert "pending confirmation" in resp.text
    assert "not yet acknowledged" in resp.text
    assert "reconciliation not waived" in resp.text
    assert not (harness.tmp / "closes.jsonl").exists()  # nothing signed


async def test_sign_non_quarterly_label_refused_before_composition(harness: Harness):
    """AC7 / period precondition: a non-quarterly label is refused into the page, no write."""
    await _make_signable(harness, period="2026-Q2")
    async with _client(harness.app) as client:
        resp = await client.post("/ui/sign", data={"period": "garbage"})
    assert resp.status_code == 200
    assert "Refused" in resp.text and "quarterly label" in resp.text
    assert not (harness.tmp / "closes.jsonl").exists()


async def test_sign_empty_period_refused(harness: Harness):
    """AC7 / period precondition: a quarterly label with no ledger transactions is refused."""
    async with _client(harness.app) as client:
        resp = await client.post("/ui/sign", data={"period": "2030-Q4"})
    assert resp.status_code == 200
    assert "Refused" in resp.text and "no ledger transactions" in resp.text


async def test_sign_already_closed_renders_snapshot_no_second_row(harness: Harness):
    """AC7 / closed guard: signing an already-closed period renders the stored snapshot
    and never writes a second close row."""
    await _make_signable(harness)
    async with _client(harness.app) as client:
        await client.post("/ui/sign", data={"period": PERIOD})
        resp = await client.post("/ui/sign", data={"period": PERIOD})
    assert resp.status_code == 200
    assert "signed closed" in resp.text and "already closed" in resp.text
    rows = await harness.close_store.all()
    assert len(rows) == 1  # no double-close


# ============================================================================
# AC8 — closed banners + control suppression (+ server guard still enforces)
# ============================================================================


async def test_closed_banner_and_suppression_on_all_screens(harness: Harness):
    """AC8: after signing, the queue / ledger / import / reconcile screens show the
    closed banner and render no resolve/confirm controls; the server guard still
    refuses a forged write."""
    txn = await _make_signable(harness)
    async with _client(harness.app) as client:
        await client.post("/ui/sign", data={"period": PERIOD, "signed_by": "owner"})

        queue = (await client.get("/ui/queue", params={"period": PERIOD})).text
        ledger = (await client.get("/ui/ledger", params={"period": PERIOD})).text
        reconcile = (await client.get("/ui/reconcile", params={"period": PERIOD})).text
        home = (await client.get("/", params={"period": PERIOD})).text

        # Banner on every screen.
        for page in (queue, ledger, reconcile, home):
            assert "signed closed" in page
            assert "owner" in page

        # No resolve/confirm controls on the closed period's queue / reconcile.
        assert 'hx-post="/ui/resolve"' not in queue
        assert 'hx-post="/ui/reconcile/resolve"' not in reconcile

        # The server guard still refuses a forged resolve write against the closed period.
        forged = await client.post(
            "/ui/resolve",
            data={"transaction_id": transaction_key(txn), "account": "5200-travel", "period": PERIOD},
        )
        assert forged.status_code == 200
        assert "is closed" in forged.text  # the closed-refusal partial

    # The forged write persisted nothing new: exactly the one make-signable confirmation.
    confirmations = await harness.confirmation_store.all()
    assert len(confirmations) == 1


async def test_open_period_screens_unbannered_and_controls_present(harness: Harness):
    """AC8 (negative): an OPEN period shows no banner and keeps its controls — the
    banner/suppression are gated on the period being closed."""
    await harness.ledger_store.store(_delta_1200())
    async with _client(harness.app) as client:
        queue = (await client.get("/ui/queue", params={"period": PERIOD})).text
    assert "signed closed" not in queue
    assert 'hx-post="/ui/resolve"' in queue  # controls present on an open period


# ============================================================================
# AC9 — materiality-floor-unset honesty
# ============================================================================


async def test_materiality_floor_unset_states_size_check_inactive(examples_dir, tmp_path):
    """AC9: with `materiality_floor` unset the anomalies block states the size check is
    inactive (never implies it ran), and no over_materiality flag renders."""
    h = _harness(examples_dir, tmp_path, config=_config(examples_dir, drop_materiality=True))
    await h.ledger_store.store(_delta_1200())  # 1200 would flag if the floor ran
    async with _client(h.app) as client:
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
    assert "the size check is inactive" in html
    # No over_materiality anomaly *card* renders (the kind name in the inactive-check
    # explanation is not a flag) — the check never ran.
    assert '<span class="tag kind">over_materiality</span>' not in html


async def test_materiality_floor_set_renders_over_materiality_flag(harness: Harness):
    """AC9: with the floor set (example config 1000.00) the over_materiality flag renders."""
    await harness.ledger_store.store(_delta_1200())
    async with _client(harness.app) as client:
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
    assert '<span class="tag kind">over_materiality</span>' in html  # the flag card renders
    assert "the size check is inactive" not in html


# ============================================================================
# AC10 — no CDN / vendored assets only
# ============================================================================


async def test_no_remote_asset_references_in_templates():
    """AC10: no template references a remote asset — htmx/CSS are the vendored
    /static/* assets (this runs local + offline)."""
    external = re.compile(r'(?:src|href)\s*=\s*["\'](?:https?:)?//', re.IGNORECASE)
    for path in TEMPLATES_DIR.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not external.search(text), f"{path.name} references a remote asset"


async def test_rendered_close_screen_uses_only_vendored_assets(harness: Harness):
    """AC10: the rendered close screen pulls only the vendored /static assets (no CDN)."""
    await harness.ledger_store.store(_delta_1200())
    async with _client(harness.app) as client:
        html = (await client.get("/ui/close", params={"period": PERIOD})).text
    assert '/static/htmx.min.js' in html
    assert '/static/app.css' in html
    assert re.search(r'(?:src|href)\s*=\s*["\'](?:https?:)?//', html, re.IGNORECASE) is None


# ============================================================================
# UnknownTaxRegime — rendered into the page, never a 500
# ============================================================================


async def test_unknown_tax_regime_rendered_as_error_not_500(examples_dir, tmp_path):
    """A `tax_regime` the framework does not register makes `track_tax` fail fast; the
    close screen renders the error into the page (the Slice-1 rule), never a 500."""
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    data["tax_regime"] = "VAT"
    h = _harness(examples_dir, tmp_path, config=BookkeeperConfig.from_mapping(data))
    await h.ledger_store.store(_delta_1200())
    async with _client(h.app) as client:
        resp = await client.get("/ui/close", params={"period": PERIOD})
    assert resp.status_code == 200
    assert "Cannot assemble the close" in resp.text
    assert "Unknown tax_regime" in resp.text
