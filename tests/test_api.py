"""The Tier-2 API, end to end (httpx over the ASGI app).

Drives `create_app` with an injected temp-path ledger + confirmation trail and
the committed sample config, exercising the full slice: import → categorize
(the trust trail) → resolve → the categorized ledger. The framework `categorize`
is called as-is; the app writes only through its own #1 stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper_ui.api import create_app
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore


@dataclass
class ApiHarness:
    app: FastAPI
    ledger_path: Path
    confirmations_path: Path
    examples_dir: Path


@pytest.fixture
def api(tmp_path, examples_dir) -> ApiHarness:
    ledger_path = tmp_path / "ledger.jsonl"
    confirmations_path = tmp_path / "confirmations.jsonl"
    app = create_app(
        config=load_config(examples_dir / "config.json"),
        ledger_store=FileLedgerStore(ledger_path),
        confirmation_store=FileConfirmationStore(confirmations_path),
    )
    return ApiHarness(app, ledger_path, confirmations_path, examples_dir)


def _client(app: FastAPI) -> httpx.AsyncClient:
    """An httpx client that speaks to the app in-process (no socket)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _import_csv(client: httpx.AsyncClient, csv_path: Path) -> httpx.Response:
    return await client.post(
        "/import",
        files={"file": ("transactions.csv", csv_path.read_bytes(), "text/csv")},
    )


async def test_import_categorize_resolve_ledger_end_to_end(api: ApiHarness):
    """AC: import → /categorize carries the trust trail → /resolve → /ledger
    reflects the confirmed account."""
    async with _client(api.app) as client:
        # 1. Import the sample CSV → persisted via the #1 store.
        resp = await _import_csv(client, api.examples_dir / "transactions.csv")
        assert resp.status_code == 200
        assert resp.json()["imported"] == 6

        # 2. Categorize Q2 → proposals + flagged, each carrying the trust trail.
        resp = await client.post("/categorize", params={"period": "2026-Q2"})
        assert resp.status_code == 200
        report = resp.json()
        assert report["period"] == "2026-Q2"

        proposals = report["proposals"]
        flagged = report["flagged"]
        # Every proposal carries proposed_account + confidence + source (the rule).
        for proposal in proposals:
            assert proposal["proposed_account"]
            assert 0.0 <= proposal["confidence"] <= 1.0
            assert proposal["source"] in ("owner-rule", "chart-match")
            assert proposal["transaction"]["id"]  # the id to resolve against
        # Every flag carries the human-readable reason.
        for flag in flagged:
            assert flag["reason"]

        proposals_by_vendor = {p["transaction"]["vendor"]: p for p in proposals}
        # Owner-rule proposal: exact vendor→account map, full confidence.
        delta = proposals_by_vendor["Delta Airlines"]
        assert delta["proposed_account"] == "5200-travel"
        assert delta["source"] == "owner-rule"
        assert delta["confidence"] == 1.0
        # Chart-match proposal: scaled below owner-rule certainty.
        assert proposals_by_vendor["Staples"]["proposed_account"] == "5000-office-supplies"
        assert proposals_by_vendor["Staples"]["source"] == "chart-match"
        # Below-threshold transaction is flagged, not silently pre-filled.
        assert any(f["transaction"]["vendor"] == "Blue Bottle Coffee" for f in flagged)

        # 3. Resolve (confirm) the Delta proposal, using the id the API handed back.
        delta_id = delta["transaction"]["id"]
        resp = await client.post(
            "/resolve", json={"transaction_id": delta_id, "account": "5200-travel"}
        )
        assert resp.status_code == 200
        confirmation = resp.json()
        assert confirmation["account"] == "5200-travel"
        assert confirmation["source"] == "human"

        # 4. The ledger reflects the confirmation; the rest keep their trust trail.
        resp = await client.get("/ledger", params={"period": "2026-Q2"})
        assert resp.status_code == 200
        entries = {e["transaction"]["vendor"]: e for e in resp.json()["entries"]}

        assert entries["Delta Airlines"]["status"] == "confirmed"
        assert entries["Delta Airlines"]["account"] == "5200-travel"
        assert entries["Delta Airlines"]["source"] == "human"

        staples = entries["Staples"]
        assert staples["status"] == "proposed"
        assert staples["account"] == "5000-office-supplies"
        assert staples["source"] == "chart-match"
        assert staples["confidence"] == pytest.approx(0.9)
        # Wire money is an exact string, trailing zero intact (never a lossy float).
        assert staples["transaction"]["amount"] == "82.50"

        blue_bottle = entries["Blue Bottle Coffee"]
        assert blue_bottle["status"] == "flagged"
        assert blue_bottle["reason"]
        assert blue_bottle["account"] is None


async def test_resolve_rejects_account_not_in_chart(api: ApiHarness):
    """AC: /resolve rejects an account not in config.chart_of_accounts."""
    async with _client(api.app) as client:
        resp = await client.post(
            "/resolve",
            json={"transaction_id": "some-txn-id", "account": "9999-not-a-real-account"},
        )
        assert resp.status_code == 422
        assert "chart_of_accounts" in resp.json()["detail"]
    # Nothing was written — a rejected resolution never touches the store.
    assert not api.confirmations_path.exists()


async def test_categorize_writes_nothing(api: ApiHarness):
    """AC: categorize is proposals-only (§5.4) — it writes to no store.

    The ledger file is byte-identical before/after a /categorize, and no
    confirmation is created (the write path is /resolve alone)."""
    async with _client(api.app) as client:
        await _import_csv(client, api.examples_dir / "transactions.csv")

        before = api.ledger_path.read_bytes()
        resp = await client.post("/categorize", params={"period": "2026-Q2"})
        assert resp.status_code == 200
        assert api.ledger_path.read_bytes() == before
        assert not api.confirmations_path.exists()


async def test_correction_supersedes_in_ledger(api: ApiHarness):
    """A correction (a second /resolve) is what the ledger shows — last write wins."""
    async with _client(api.app) as client:
        await _import_csv(client, api.examples_dir / "transactions.csv")
        resp = await client.post("/categorize", params={"period": "2026-Q2"})
        delta = next(
            p for p in resp.json()["proposals"]
            if p["transaction"]["vendor"] == "Delta Airlines"
        )
        delta_id = delta["transaction"]["id"]

        # Confirm as travel, then correct to meals-entertainment.
        await client.post(
            "/resolve", json={"transaction_id": delta_id, "account": "5200-travel"}
        )
        await client.post(
            "/resolve",
            json={"transaction_id": delta_id, "account": "5300-meals-entertainment"},
        )

        resp = await client.get("/ledger", params={"period": "2026-Q2"})
        entry = next(
            e for e in resp.json()["entries"]
            if e["transaction"]["vendor"] == "Delta Airlines"
        )
        assert entry["status"] == "confirmed"
        assert entry["account"] == "5300-meals-entertainment"


async def test_import_rejects_unsupported_format(api: ApiHarness):
    """A non-CSV/JSON upload is a clear 400, not a silent no-op import."""
    async with _client(api.app) as client:
        resp = await client.post(
            "/import",
            files={"file": ("notes.txt", b"just some text", "text/plain")},
        )
        assert resp.status_code == 400
    assert not api.ledger_path.exists()


async def test_json_import_equivalent_to_csv(api: ApiHarness):
    """The JSON upload path lands the same transactions as the CSV path."""
    async with _client(api.app) as client:
        resp = await client.post(
            "/import",
            files={
                "file": (
                    "transactions.json",
                    (api.examples_dir / "transactions.json").read_bytes(),
                    "application/json",
                )
            },
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] == 6

        resp = await client.get("/ledger", params={"period": "2026-Q2"})
        vendors = [e["transaction"]["vendor"] for e in resp.json()["entries"]]
        assert vendors == ["Staples", "AWS", "Delta Airlines", "Blue Bottle Coffee", "WeWork"]


async def test_import_malformed_json_is_400(api: ApiHarness):
    """A malformed JSON upload is the 400 the route's docstring promises, not a 500 (B2)."""
    async with _client(api.app) as client:
        resp = await client.post(
            "/import",
            files={"file": ("bad.json", b"{not valid json", "application/json")},
        )
        assert resp.status_code == 400
    assert not api.ledger_path.exists()  # all-or-nothing: nothing persisted


async def test_import_json_number_amount_survives_to_ledger_exact(api: ApiHarness):
    """An unquoted JSON amount reaches /ledger as the exact string, not a float (B1)."""
    body = (
        b'[{"date": "2026-05-02", "vendor": "Numeric Vendor", '
        b'"amount": 82.50, "attribution_target_id": "target-001"}]'
    )
    async with _client(api.app) as client:
        resp = await client.post(
            "/import", files={"file": ("nums.json", body, "application/json")}
        )
        assert resp.status_code == 200

        resp = await client.get("/ledger", params={"period": "2026-Q2"})
        entry = next(
            e for e in resp.json()["entries"]
            if e["transaction"]["vendor"] == "Numeric Vendor"
        )
        assert entry["transaction"]["amount"] == "82.50"
