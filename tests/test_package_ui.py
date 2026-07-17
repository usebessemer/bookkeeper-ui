"""Slice 4 · C — the accountant-package preview UI: GET /ui/package + package.html.

Drives the HTML surface `register_ui` mounts on `create_app` (httpx over ASGI, the
Slice 1/2/3 style) with injected temp-path stores and the committed sample config.
`GET /ui/package` renders the SAME `views.build_package` projection the JSON
`GET /package` (issue A) serializes — one projection, no second computation, no
direct `generate_accountant_package` call. The UI halves of the acceptance criteria:

- AC-1  (§5.4 fidelity): a BLOCKED close renders `unmet_close` VERBATIM and offers
  NO export control (no button, no checkbox, no form).
- AC-7  (trust trail): each entry's proposed_account / confidence / source render
  exactly — owner-rule / chart-match verbatim, a human-confirmation entry as
  `human-confirmed` with its confidence suppressed (the synthetic 1.0 is never
  presented as an agent confidence).
- AC-11 (UnknownTaxRegime): an unregistered regime renders a human-readable error
  into the page (a 200), never a bare 500/traceback.
- AC-13 (overlay honesty): a matched confirmation renders `confirmed_account` with no
  "diverges" badge; a corrected one renders the badge; the divergence-count banner
  appears iff `divergence_count > 0`; the framework fields render identically with and
  without confirmations (the overlay never rewrites the package).

The framework skill (`generate_accountant_package`) is called as-is; the app writes
nothing here (the preview is read-only). Scaffolding mirrors tests/test_package.py.
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

from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore
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


# --- Harness + config builders (mirrors test_package.py) ----------------------


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


def _config(examples_dir: Path, **overrides: object) -> BookkeeperConfig:
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    if "tax_regime" in overrides:
        data["tax_regime"] = overrides["tax_regime"]
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


def _row(html: str, needle: str) -> str:
    """The single `<tr>…</tr>` fragment containing `needle` (vendors are unique)."""
    rows = re.findall(r"<tr\b.*?</tr>", html, re.DOTALL)
    matches = [r for r in rows if needle in r]
    assert matches, f"no table row containing {needle!r}"
    assert len(matches) == 1, f"expected one row containing {needle!r}, got {len(matches)}"
    return matches[0]


# Grounded against the shipped example config (chart + owner_policies + HST), same as
# test_package.py:
#   "AWS"            → owner-rule proposal (5100-software-subscriptions), conf 1.0.
#   "Rent"           → chart-match proposal (6100-rent), conf 0.9.
#   "Zzxq Gibberish" → below the categorize floor → FLAGGED (blocks until confirmed).
def _aws(amount: str = "50.00", tax: str = "0") -> object:
    return make_txn(vendor="AWS", amount=amount, tax=tax, date=datetime(2026, 5, 1), description="cloud")


def _rent() -> object:
    return make_txn(vendor="Rent", amount="20.00", tax="0", date=datetime(2026, 5, 2), description="office rent")


def _flagged() -> object:
    return make_txn(vendor="Zzxq Gibberish", amount="5.00", tax="0", date=datetime(2026, 5, 3), description="")


async def _waive(h: Harness) -> None:
    await h.waiver_store.record(Waiver(period=PERIOD, waived_at=AT, waived_by="human", note=""))


async def _confirm(h: Harness, txn: object, account: str, at: datetime = AT) -> None:
    await h.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(txn), account=account, source=SOURCE_HUMAN, decided_at=at)
    )


async def _ready_three_sources(h: Harness) -> object:
    """A READY close with all three proposal sources + a waiver → a PROPOSED package.

    AWS (owner-rule) · Rent (chart-match) · Zzxq (flagged → confirmed → human). The
    confirmed flag clears `categorization_complete`; the waiver clears reconciliation.
    Returns the flagged (now human-confirmed) transaction.
    """
    await h.ledger_store.store(_aws())
    await h.ledger_store.store(_rent())
    flagged = _flagged()
    await h.ledger_store.store(flagged)
    await _confirm(h, flagged, "5000-office-supplies")
    await _waive(h)
    return flagged


async def _get(app: FastAPI, period: str = PERIOD) -> httpx.Response:
    async with _client(app) as client:
        return await client.get("/ui/package", params={"period": period})


# ============================================================================
# AC-1 — §5.4 fidelity: a BLOCKED close renders unmet_close verbatim + no export control
# ============================================================================


async def test_blocked_renders_unmet_close_verbatim_and_no_export_control(harness: Harness):
    """AC-1: an open period whose effective close is BLOCKED (an unresolved flag)
    renders the `unmet_close` reason VERBATIM (naming the failing check) and offers no
    export control anywhere on the page."""
    await harness.ledger_store.store(_flagged())  # blocks categorization_complete
    await _waive(harness)  # isolate the flag as the sole blocker

    async with _client(harness.app) as client:
        j = (await client.get("/package", params={"period": PERIOD})).json()
        resp = await client.get("/ui/package", params={"period": PERIOD})
    html = resp.text

    assert resp.status_code == 200
    assert j["status"] == "blocked"
    # The framework reason renders verbatim (HTML-escaped, as Jinja does), naming the check.
    assert "categorization_complete" in j["unmet_close"]
    assert str(escape(j["unmet_close"])) in html
    assert '<span class="tag status-blocked">blocked' in html

    # NO export control of any kind on a blocked page.
    assert "/ui/export" not in html
    assert 'type="checkbox"' not in html
    assert "export-button" not in html
    assert "export-form" not in html


async def test_blocked_from_already_closed_period_also_has_no_export_control(harness: Harness):
    """AC-1: an already-closed period is a BLOCKED preview (deferred-snapshot message)
    — same discipline: unmet_close rendered, no export control."""
    from bookkeeper_ui.closes import CloseRecord

    await harness.ledger_store.store(_aws())
    await harness.close_store.record(
        CloseRecord(
            period=PERIOD, signed_at=datetime(2026, 7, 5, tzinfo=timezone.utc), signed_by="human",
            checklist=[{"name": "period_closeable", "met": True, "reason": "ok"}],
            transactions=[], tax={"period_total": "0"}, reconciliation={"waived": True},
            anomalies=[], summary={}, effective_prior_period_state="2026-Q1", config_prior_period_state=None,
        )
    )
    html = (await _get(harness.app)).text
    assert '<span class="tag status-blocked">blocked' in html
    assert "already closed" in html
    assert "export-button" not in html and "/ui/export" not in html


# ============================================================================
# AC-7 — trust trail truthfully rendered (owner-rule / chart-match verbatim; human suppressed)
# ============================================================================


async def test_trust_trail_all_three_sources_render_truthfully(harness: Harness):
    """AC-7: an owner-rule entry shows `owner-rule` + its confidence; a chart-match
    shows `chart-match` + its confidence; a human-confirmation entry renders
    `human-confirmed` with its confidence cell suppressed (never the synthetic 1.0)."""
    human = await _ready_three_sources(harness)
    assert human
    html = (await _get(harness.app)).text

    # owner-rule (AWS, conf 1.0 → 100%): source + confidence render verbatim.
    aws_row = _row(html, "AWS")
    assert "owner-rule" in aws_row
    assert "100%" in aws_row
    assert "5100-software-subscriptions" in aws_row

    # chart-match (Rent, conf 0.9 → 90%).
    rent_row = _row(html, "Rent")
    assert "chart-match" in rent_row
    assert "90%" in rent_row
    assert "6100-rent" in rent_row

    # human-confirmed: the label, NOT the raw source; confidence suppressed (no "%").
    human_row = _row(html, "Zzxq Gibberish")
    assert "human-confirmed" in human_row
    assert "confidence-suppressed" in human_row
    assert "%" not in human_row  # the synthetic 1.0 is never shown as a confidence
    assert ">human<" not in human_row  # not the raw "human" source string


# ============================================================================
# AC-11 — UnknownTaxRegime on the UI surface (rendered error, not a 500)
# ============================================================================


async def test_unknown_tax_regime_renders_error_not_500(examples_dir, tmp_path):
    """AC-11: an unregistered `tax_regime` surfaces on `GET /ui/package` as a rendered,
    human-readable error naming the registered regimes — a 200, never a bare 500."""
    h = _harness(examples_dir, tmp_path, config=_config(examples_dir, tax_regime="VAT"))
    await h.ledger_store.store(_aws())
    resp = await _get(h.app)
    assert resp.status_code == 200
    assert "Cannot assemble the package" in resp.text
    assert "Unknown tax_regime" in resp.text  # the framework message names the regimes
    assert "tag status-proposed" not in resp.text  # no package rendered


# ============================================================================
# AC-13 — overlay honesty (display): confirmed_account, diverges badge, banner, additivity
# ============================================================================


async def test_overlay_matched_confirmation_no_badge(harness: Harness):
    """AC-13: an entry whose latest confirmation matches the proposal renders
    `confirmed_account` set with NO "diverges" badge, and no divergence banner."""
    aws = _aws()
    await harness.ledger_store.store(aws)
    await harness.ledger_store.store(_rent())
    await _waive(harness)
    await _confirm(harness, aws, "5100-software-subscriptions")  # matches the proposal

    html = (await _get(harness.app)).text
    aws_row = _row(html, "AWS")
    assert "5100-software-subscriptions" in aws_row  # confirmed account shown
    assert "diverges-badge" not in aws_row  # matched → no badge
    assert "divergence-banner" not in html  # count 0 → no banner


async def test_overlay_diverging_confirmation_shows_badge_and_banner(harness: Harness):
    """AC-13: a corrected entry (confirmation account ≠ proposed) renders the "diverges"
    badge, and the divergence-count banner appears when `divergence_count > 0`."""
    aws = _aws()
    await harness.ledger_store.store(aws)
    await harness.ledger_store.store(_rent())
    await _waive(harness)
    await _confirm(harness, aws, "5000-office-supplies")  # corrected away from 5100

    html = (await _get(harness.app)).text
    aws_row = _row(html, "AWS")
    assert "diverges-badge" in aws_row
    assert "5000-office-supplies" in aws_row
    # Rent was not corrected → no badge on its row.
    assert "diverges-badge" not in _row(html, "Rent")
    # The banner appears, naming the one correction.
    assert "divergence-banner" in html
    assert "1 correction" in html or ">1</strong>" in html


async def test_overlay_is_additive_framework_fields_identical(harness: Harness):
    """AC-13: the framework fields (proposed_account / confidence / source) render
    identically with and without confirmations — the overlay never rewrites the
    package — and the banner is absent at count 0, present at count > 0."""
    aws = _aws()
    await harness.ledger_store.store(aws)
    await harness.ledger_store.store(_rent())
    await _waive(harness)

    # Baseline — no confirmations: framework cells present, no confirmed account, no banner.
    base_row = _row((await _get(harness.app)).text, "AWS")
    for token in ("owner-rule", "100%", "5100-software-subscriptions"):
        assert token in base_row
    assert "divergence-banner" not in (await _get(harness.app)).text

    # Confirm matching → framework cells unchanged, still no divergence.
    await _confirm(harness, aws, "5100-software-subscriptions")
    matched_html = (await _get(harness.app)).text
    matched_row = _row(matched_html, "AWS")
    for token in ("owner-rule", "100%", "5100-software-subscriptions"):
        assert token in matched_row
    assert "divergence-banner" not in matched_html

    # Correct to a different account → framework cells STILL unchanged; banner appears.
    await _confirm(harness, aws, "5000-office-supplies", at=datetime(2026, 7, 2, tzinfo=timezone.utc))
    corrected_html = (await _get(harness.app)).text
    corrected_row = _row(corrected_html, "AWS")
    for token in ("owner-rule", "100%", "5100-software-subscriptions"):
        assert token in corrected_row  # the proposal is byte-identical to baseline
    assert "divergence-banner" in corrected_html


# ============================================================================
# Proposed shape + export trigger (client-convenience) + nav + no-transmission
# ============================================================================


async def test_proposed_page_renders_banner_basis_summary_and_tax_reconciliation(harness: Harness):
    """A PROPOSED package renders the 'proposed / never auto-published' banner, the
    basis line (period · method · jurisdiction · regime), the summary counts, and the
    tax + reconciliation sections with exact-string money."""
    await harness.ledger_store.store(_aws(amount="82.50", tax="6.50"))
    await _waive(harness)
    html = (await _get(harness.app)).text

    assert '<span class="tag status-proposed">proposed' in html
    assert "never" in html and "auto-published" in html
    # Basis line carries the config method/jurisdiction + the HST regime.
    assert harness.config.accounting_method in html
    assert harness.config.jurisdiction in html
    assert "HST" in html
    # Money renders as the exact string (trailing zeros preserved), never a float.
    assert "82.50" in html
    assert "6.50" in html
    assert "82.5<" not in html  # not truncated to 82.5


async def test_export_control_requires_ack_only_when_divergences_exist(harness: Harness):
    """The Export button is client-convenience only: with no divergences it renders
    without an acknowledgment checkbox; with divergences a required checkbox gates it
    (a UX nudge, not a server gate)."""
    aws = _aws()
    await harness.ledger_store.store(aws)
    await _waive(harness)

    # No divergence → button present, no ack checkbox.
    html = (await _get(harness.app)).text
    assert 'hx-post="/ui/export"' in html
    assert "export-button" in html
    assert 'type="checkbox"' not in html

    # Correct AWS → divergence → the required ack checkbox appears.
    await _confirm(harness, aws, "5000-office-supplies")
    html = (await _get(harness.app)).text
    assert 'type="checkbox"' in html
    assert "required" in html
    assert 'name="acknowledged"' in html


async def test_matched_pair_trail_renders_statement_side(harness: Harness):
    """The reconciliation trail renders the matched pair's statement_ref / date / amount
    / description + the ledger transaction_id (the package links by reference; a
    StatementLine has no vendor field)."""
    txn = make_txn(vendor="Travel Agency", amount="100.00", tax="0",
                   date=datetime(2026, 5, 5), description="travel")
    await harness.ledger_store.store(txn)
    await harness.statement_store.store(
        make_stmt_line(statement_ref="STMT-100", amount="100.00",
                       date=datetime(2026, 5, 5), description="Travel Agency")
    )
    html = (await _get(harness.app)).text
    pair_row = _row(html, "STMT-100")
    assert "2026-05-05" in pair_row
    assert "100.00" in pair_row
    assert "Travel Agency" in pair_row
    assert transaction_key(txn) in pair_row


async def test_nav_carries_package_link(harness: Harness):
    """The shared nav carries a 'Package' link (with the period), on every screen."""
    async with _client(harness.app) as client:
        home = (await client.get("/", params={"period": PERIOD})).text
        pkg = (await client.get("/ui/package", params={"period": PERIOD})).text
    for page in (home, pkg):
        assert f'href="/ui/package?period={PERIOD}"' in page
        assert ">Package</a>" in page


async def test_proposed_page_uses_only_vendored_assets(harness: Harness):
    """No transmission / no CDN: the rendered package page pulls only the vendored
    /static assets (this runs local + offline), and carries no outbound reference."""
    await harness.ledger_store.store(_aws())
    await _waive(harness)
    html = (await _get(harness.app)).text
    assert "/static/htmx.min.js" in html
    assert "/static/app.css" in html
    assert re.search(r'(?:src|href)\s*=\s*["\'](?:https?:)?//', html, re.IGNORECASE) is None
