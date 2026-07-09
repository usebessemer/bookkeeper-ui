"""The thin UI (#3), end to end (httpx over the ASGI app).

Drives the HTML surface `register_ui` mounts on `create_app` — the import screen,
the confirm queue (the trust trail), the htmx `/ui/resolve` swap, and the
categorized ledger — over an injected temp-path ledger + confirmation trail and
the committed sample config. Pages render; the htmx endpoints return the expected
partials. The JSON API itself is already covered by `test_api.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper_ui.api import create_app
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore

# One card per (status, transaction id, vendor) in a rendered queue — the card id
# is the transaction id the UI posts back to /ui/resolve.
_CARD = re.compile(
    r'class="card (?P<status>proposed|flagged)" id="card-(?P<id>[0-9a-f]{64})">'
    r'.*?<span class="vendor">(?P<vendor>[^<]+)</span>',
    re.DOTALL,
)


@dataclass
class UiHarness:
    app: FastAPI
    ledger_path: Path
    confirmations_path: Path
    examples_dir: Path


@pytest.fixture
def ui(tmp_path, examples_dir) -> UiHarness:
    ledger_path = tmp_path / "ledger.jsonl"
    confirmations_path = tmp_path / "confirmations.jsonl"
    app = create_app(
        config=load_config(examples_dir / "config.json"),
        ledger_store=FileLedgerStore(ledger_path),
        confirmation_store=FileConfirmationStore(confirmations_path),
        statement_store=FileStatementStore(tmp_path / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(tmp_path / "reconciliations.jsonl"),
    )
    return UiHarness(app, ledger_path, confirmations_path, examples_dir)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _import_csv(client: httpx.AsyncClient, csv_path: Path, period: str = "2026-Q2"):
    return await client.post(
        "/ui/import",
        files={"file": ("transactions.csv", csv_path.read_bytes(), "text/csv")},
        data={"period": period},
    )


def _cards(html: str) -> dict[str, dict[str, str]]:
    """Map vendor -> {id, status} for every card in a rendered queue."""
    return {
        m.group("vendor"): {"id": m.group("id"), "status": m.group("status")}
        for m in _CARD.finditer(html)
    }


async def test_home_renders_import_form(ui: UiHarness):
    """AC: the import screen renders — an htmx upload form with a period field."""
    async with _client(ui.app) as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert 'hx-post="/ui/import"' in resp.text
        assert 'type="file"' in resp.text
        assert 'name="period"' in resp.text


async def test_import_renders_result_with_queue_link(ui: UiHarness):
    """AC: import the examples dataset → a result partial linking to the queue."""
    async with _client(ui.app) as client:
        resp = await _import_csv(client, ui.examples_dir / "transactions.csv")
        assert resp.status_code == 200
        assert "Imported 6" in resp.text
        # Primary CTA to the chosen period, plus the detected-period convenience links.
        assert '/ui/queue?period=2026-Q2' in resp.text
        assert '/ui/queue?period=2026-Q1' in resp.text  # GitHub row lands in Q1
    # The upload persisted through the same #1 store the API writes.
    assert ui.ledger_path.exists()


async def test_import_bad_file_renders_error_not_500(ui: UiHarness):
    """AC-adjacent: a bad upload renders the error into the page (200), not a 500;
    nothing is persisted."""
    async with _client(ui.app) as client:
        resp = await client.post(
            "/ui/import",
            files={"file": ("notes.txt", b"just some text", "text/plain")},
        )
        assert resp.status_code == 200
        assert "Import failed" in resp.text
    assert not ui.ledger_path.exists()  # all-or-nothing: nothing stored


async def test_queue_renders_full_trust_trail(ui: UiHarness):
    """AC (the core): every proposal card carries proposed account + confidence +
    the rule that fired; flagged items show their reason and no proposal."""
    async with _client(ui.app) as client:
        await _import_csv(client, ui.examples_dir / "transactions.csv")
        resp = await client.get("/ui/queue", params={"period": "2026-Q2"})
        assert resp.status_code == 200
        html = resp.text

        cards = _cards(html)
        # Owner-rule proposal: exact vendor→account, rendered as owner-rule.
        assert cards["Delta Airlines"]["status"] == "proposed"
        assert "5200-travel" in html
        assert 'source-owner-rule' in html
        # Chart-match proposal: the other rule, scaled below owner-rule certainty.
        assert cards["Staples"]["status"] == "proposed"
        assert "5000-office-supplies" in html
        assert 'source-chart-match' in html
        # The confidence signal is on the trail.
        assert "% confident" in html
        # Below-threshold transaction is flagged with a human-readable reason.
        assert cards["Blue Bottle Coffee"]["status"] == "flagged"
        assert "Needs categorization" in html
        # A flagged card offers a pick-an-account action (no confident proposal).
        assert 'name="account"' in html


async def test_resolve_swaps_card_out_and_persists(ui: UiHarness):
    """AC: Confirm persists via /ui/resolve and the card leaves the queue (htmx OOB
    counter shrinks); a re-rendered queue no longer shows it."""
    async with _client(ui.app) as client:
        await _import_csv(client, ui.examples_dir / "transactions.csv")
        resp = await client.get("/ui/queue", params={"period": "2026-Q2"})
        cards = _cards(resp.text)
        assert len(cards) == 5  # five Q2 transactions awaiting review
        delta_id = cards["Delta Airlines"]["id"]

        resp = await client.post(
            "/ui/resolve",
            data={"transaction_id": delta_id, "account": "5200-travel", "period": "2026-Q2"},
        )
        assert resp.status_code == 200
        # The response is the out-of-band counter update (now 4); the empty
        # remainder is what removes the card from the queue.
        assert 'id="pending-count"' in resp.text
        assert 'hx-swap-oob="true"' in resp.text
        assert ">4<" in resp.text
        assert "card-" not in resp.text  # no card markup — the card is being removed

        # The decision persisted, and a fresh queue no longer carries the card.
        assert ui.confirmations_path.exists()
        resp = await client.get("/ui/queue", params={"period": "2026-Q2"})
        assert "Delta Airlines" not in _cards(resp.text)


async def test_resolve_rejects_off_chart_account(ui: UiHarness):
    """§5.2 holds through the UI too: an off-chart account is a 422, persists nothing
    (unreachable from the rendered select — a defensive guard)."""
    async with _client(ui.app) as client:
        await _import_csv(client, ui.examples_dir / "transactions.csv")
        resp = await client.post(
            "/ui/resolve",
            data={"transaction_id": "abc", "account": "9999-nope", "period": "2026-Q2"},
        )
        assert resp.status_code == 422
    assert not ui.confirmations_path.exists()


async def test_resolve_unknown_transaction_is_404(ui: UiHarness):
    """AC (#21 / N1): /ui/resolve mirrors the API — an unknown transaction id is a
    strict 404 (unreachable from the rendered queue; a defensive guard), and
    persists nothing. A valid account isolates the txn-existence guard so the 404
    is what fires, not the §5.2 account 422."""
    async with _client(ui.app) as client:
        await _import_csv(client, ui.examples_dir / "transactions.csv")
        resp = await client.post(
            "/ui/resolve",
            data={
                "transaction_id": "not-a-real-transaction-id",
                "account": "5200-travel",
                "period": "2026-Q2",
            },
        )
        assert resp.status_code == 404
    assert not ui.confirmations_path.exists()  # no orphan confirmation


async def test_ledger_shows_confirmed_and_pending_count(ui: UiHarness):
    """AC: after a resolve the ledger view shows the confirmed item with its account
    and who decided, plus the remaining pending count."""
    async with _client(ui.app) as client:
        await _import_csv(client, ui.examples_dir / "transactions.csv")
        resp = await client.get("/ui/queue", params={"period": "2026-Q2"})
        delta_id = _cards(resp.text)["Delta Airlines"]["id"]
        await client.post(
            "/ui/resolve",
            data={"transaction_id": delta_id, "account": "5200-travel", "period": "2026-Q2"},
        )

        resp = await client.get("/ui/ledger", params={"period": "2026-Q2"})
        assert resp.status_code == 200
        html = resp.text
        assert "Delta Airlines" in html
        assert "5200-travel" in html
        assert "human" in html  # who decided
        assert "1 confirmed" in html
        assert "4</strong> still pending" in html


async def test_flagged_item_categorizable_via_pick(ui: UiHarness):
    """A flagged transaction (no proposal) is resolvable by picking an account; it
    then lands in the ledger as confirmed."""
    async with _client(ui.app) as client:
        await _import_csv(client, ui.examples_dir / "transactions.csv")
        resp = await client.get("/ui/queue", params={"period": "2026-Q2"})
        blue = _cards(resp.text)["Blue Bottle Coffee"]
        assert blue["status"] == "flagged"

        resp = await client.post(
            "/ui/resolve",
            data={
                "transaction_id": blue["id"],
                "account": "5300-meals-entertainment",
                "period": "2026-Q2",
            },
        )
        assert resp.status_code == 200

        resp = await client.get("/ui/ledger", params={"period": "2026-Q2"})
        assert "Blue Bottle Coffee" in resp.text
        assert "5300-meals-entertainment" in resp.text


async def test_queue_empty_state_for_period_with_no_transactions(ui: UiHarness):
    """A period with nothing to review renders a friendly empty state, not a blank
    page or an error."""
    async with _client(ui.app) as client:
        resp = await client.get("/ui/queue", params={"period": "2030-Q4"})
        assert resp.status_code == 200
        assert "Nothing awaiting review" in resp.text
