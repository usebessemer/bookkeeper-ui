"""Slice 2 · C — the reconcile queue UI + ledger fold, end to end (httpx over ASGI).

Drives the HTML surface `register_ui` mounts on `create_app` — the statement
upload, the reconcile queue (to-confirm cards, gap cards, the matched + resolved
trails), the htmx `/ui/reconcile/resolve` swap, and the ledger fold — over
injected temp-path stores and the committed sample config (which now configures
`reconcile_vendor: 0.7`). Every surface reads the *one* `build_reconciliation`
projection: the JSON `/reconcile/view`, the queue HTML, and the ledger annotation
agree over the same stores. The JSON reconcile API itself is covered by
`test_reconcile_api.py`.

The all-buckets demo is the committed `examples/reconcile-demo.*` fixture: one Q2
reconcile run over it + `examples/transactions.*` surfaces every bucket at once —
two confident matches (Blue Bottle via a mangled `SQ *` descriptor, WeWork), one
divergent `to_confirm` (Delta vs an Amazon descriptor), and all three gap kinds
(Staples amount_mismatch delta -2.50, a mystery statement-only line, AWS uncovered).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from markupsafe import escape

from bookkeeper.config import BookkeeperConfig

from bookkeeper_ui.api import create_app
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore

PERIOD = "2026-Q2"

# The exact inert wording the header must render verbatim when the reconcile_vendor
# boundary is unset (AC2) — a §5 boundary honestly reported as inert.
INERT_FLOOR_WORDING = (
    "vendor floor: not configured - every linked pair surfaces for confirmation, "
    "nothing is auto-matched (section 5)"
)


@dataclass
class ReconUi:
    app: FastAPI
    ledger_path: Path
    statements_path: Path
    confirmations_path: Path
    reconciliations_path: Path
    examples_dir: Path


def _app(tmp_path: Path, config: BookkeeperConfig, examples_dir: Path) -> ReconUi:
    ledger_path = tmp_path / "ledger.jsonl"
    statements_path = tmp_path / "statements.jsonl"
    confirmations_path = tmp_path / "confirmations.jsonl"
    reconciliations_path = tmp_path / "reconciliations.jsonl"
    app = create_app(
        config=config,
        ledger_store=FileLedgerStore(ledger_path),
        confirmation_store=FileConfirmationStore(confirmations_path),
        statement_store=FileStatementStore(statements_path),
        reconciliation_store=FileReconciliationStore(reconciliations_path),
    )
    return ReconUi(
        app, ledger_path, statements_path, confirmations_path, reconciliations_path, examples_dir
    )


def _config_no_floor(examples_dir: Path) -> BookkeeperConfig:
    """The shipped example config with `reconcile_vendor` omitted (the boundary inert).

    Built in-test rather than by mutating the shipped `examples/config.json` (which
    C now configures with the floor at 0.7) — AC2: the unset-floor assertion cannot
    use the shipped config, and the shipped example must not be mutated under test.
    """
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    thresholds = dict(data.get("confidence_thresholds") or {})
    thresholds.pop("reconcile_vendor", None)
    data["confidence_thresholds"] = thresholds
    return BookkeeperConfig.from_mapping(data)


@pytest.fixture
def ui(tmp_path, examples_dir) -> ReconUi:
    """The reconcile UI over the shipped config — `reconcile_vendor` set to 0.7."""
    return _app(tmp_path, load_config(examples_dir / "config.json"), examples_dir)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _import_transactions(client: httpx.AsyncClient, examples_dir: Path) -> None:
    await client.post(
        "/ui/import",
        files={"file": ("transactions.csv", (examples_dir / "transactions.csv").read_bytes(), "text/csv")},
        data={"period": PERIOD},
    )


async def _import_statement(client: httpx.AsyncClient, examples_dir: Path) -> httpx.Response:
    return await client.post(
        "/ui/statements/import",
        files={"file": ("reconcile-demo.csv", (examples_dir / "reconcile-demo.csv").read_bytes(), "text/csv")},
        data={"period": PERIOD},
    )


async def _import_demo(client: httpx.AsyncClient, examples_dir: Path) -> None:
    """Import both example feeds through the UI — the all-buckets demo state."""
    await _import_transactions(client, examples_dir)
    await _import_statement(client, examples_dir)


async def _view(client: httpx.AsyncClient) -> dict:
    return (await client.get("/reconcile/view", params={"period": PERIOD})).json()


# --- Statement upload --------------------------------------------------------


async def test_import_files_renders_both_import_forms(ui: ReconUi):
    """B+ re-home: BOTH uploads (transactions *and* statement) demote from `/` to
    GET /ui/import-files — the re-home moved the route, not the forms."""
    async with _client(ui.app) as client:
        resp = await client.get("/ui/import-files")
        assert resp.status_code == 200
        assert 'hx-post="/ui/import"' in resp.text
        assert 'hx-post="/ui/statements/import"' in resp.text
        assert "Import a statement" in resp.text


async def test_statement_import_renders_result_with_reconcile_link(ui: ReconUi):
    """AC: import the demo statement → a result partial linking to the reconcile queue,
    with the per-period detected counts."""
    async with _client(ui.app) as client:
        resp = await _import_statement(client, ui.examples_dir)
        assert resp.status_code == 200
        assert "Imported 5 statement lines" in resp.text
        assert "/ui/reconcile?period=2026-Q2" in resp.text
    assert ui.statements_path.exists()  # persisted through the same store the API writes


async def test_statement_import_bad_file_renders_error_not_500(ui: ReconUi):
    """A bad statement upload renders the error into the page (200), not a 500;
    nothing is persisted (all-or-nothing)."""
    async with _client(ui.app) as client:
        resp = await client.post(
            "/ui/statements/import",
            files={"file": ("notes.txt", b"just some text", "text/plain")},
        )
        assert resp.status_code == 200
        assert "Statement import failed" in resp.text
    assert not ui.statements_path.exists()


# --- AC3: the no-statement guard, rendered ----------------------------------


async def test_no_statement_guard_renders_empty_state(ui: ReconUi):
    """AC3: transactions imported but no statement → the empty state, no gap cards
    (never render every transaction as a discrepancy when no feed was imported)."""
    async with _client(ui.app) as client:
        await _import_transactions(client, ui.examples_dir)  # transactions only
        resp = await client.get("/ui/reconcile", params={"period": PERIOD})
        assert resp.status_code == 200
        assert "No statement imported for 2026-Q2" in resp.text
        assert "recon-item" not in resp.text  # no cards of any kind
        assert "Missing from the books" not in resp.text  # no fake gap cards


# --- AC5 / AC6: all buckets + trust-trail fidelity, rendered ----------------


async def test_reconcile_queue_renders_all_buckets_and_trail_fidelity(ui: ReconUi):
    """AC5/AC6: one demo run surfaces every bucket; the queue renders the trust trail
    honestly — vendor similarity (not a match confidence), reasons verbatim, the
    signed delta as-is, and a matched trail with no confidence figure."""
    view = None
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        view = await _view(client)
        resp = await client.get("/ui/reconcile", params={"period": PERIOD})
    html = resp.text

    # to_confirm: the vendor similarity is a percentage, labeled as similarity and
    # explicitly NOT a match confidence; the framework reason is verbatim (rendered
    # HTML-safe — Jinja escapes the quotes markupsafe would, nothing more).
    assert "% vendor similarity" in html
    assert "not a match confidence" in html
    ptc = view["to_confirm"][0]
    assert str(escape(ptc["reason"])) in html  # verbatim from the framework

    # The card posts the statement_line_key (StatementLineOut.id) — NOT the
    # statement_ref (a different value that would break the 404 membership check
    # and the resolution overlay). Lock the critical footgun the issue calls out.
    assert f'name="statement_line_id" value="{ptc["statement_line"]["id"]}"' in html
    assert 'value="STMT-DEMO-003"' not in html  # the ref never becomes an id
    assert f'name="transaction_id" value="{ptc["transaction"]["id"]}"' in html

    # Every gap kind renders, with its human label and the reason verbatim.
    assert "Amount mismatch" in html
    assert "Missing from the books" in html  # unmatched_in_ledger
    assert "Not on the statement" in html  # unmatched_on_statement
    mismatch = next(g for g in view["gaps"] if g["kind"] == "amount_mismatch")
    assert str(escape(mismatch["reason"])) in html
    # amount_mismatch shows both amounts + the signed delta as-is.
    assert "$82.50" in html and "$85.00" in html
    assert "delta -2.50" in html

    # The matched trail lists the confident pairs with NO confidence figure.
    trail = re.search(r'<details class="matched-trail">.*?</details>', html, re.DOTALL)
    assert trail is not None
    assert "STMT-DEMO-001" in trail.group(0)  # Blue Bottle
    assert "%" not in trail.group(0)  # no confidence/similarity number in matched


async def test_example_demo_run_produces_all_buckets(ui: ReconUi):
    """AC6: the committed demo fixture + examples/transactions.* + examples/config.json,
    driven import → reconcile → view, yields at least one item in matched, to_confirm,
    and each gap kind (a single Q2 run)."""
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        view = await _view(client)
    assert len(view["matched"]) >= 1
    assert len(view["to_confirm"]) >= 1
    kinds = {g["kind"] for g in view["gaps"]}
    assert kinds == {"amount_mismatch", "unmatched_in_ledger", "unmatched_on_statement"}


# --- AC2: boundary honesty, rendered ----------------------------------------


async def test_floor_set_matched_trail_shows_confident_pairs(ui: ReconUi):
    """AC2: with the floor set (shipped config), the matched trail shows the confident
    mangled-descriptor pair(s)."""
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        resp = await client.get("/ui/reconcile", params={"period": PERIOD})
    html = resp.text
    assert "vendor floor: 0.70" in html
    trail = re.search(r'<details class="matched-trail">.*?</details>', html, re.DOTALL)
    assert trail is not None
    assert "Blue Bottle Coffee" in trail.group(0)
    assert "WeWork" in trail.group(0)


async def test_floor_unset_renders_inert_wording(tmp_path, examples_dir):
    """AC2: with `reconcile_vendor` unset the header renders the inert truth verbatim,
    and nothing lands in matched (no matched trail)."""
    ui = _app(tmp_path, _config_no_floor(examples_dir), examples_dir)
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        resp = await client.get("/ui/reconcile", params={"period": PERIOD})
        view = await _view(client)
    assert INERT_FLOOR_WORDING in resp.text
    assert view["matched"] == []  # inert → nothing auto-matched
    assert '<details class="matched-trail">' not in resp.text  # no matched trail


# --- htmx resolve swap + AC1 one projection ----------------------------------


async def test_resolve_confirm_swaps_card_and_updates_counter(ui: ReconUi):
    """A Confirm through the UI records the resolution and swaps the card out (htmx OOB
    counter shrinks 4 → 3); a re-rendered queue no longer offers that open card."""
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        # Header open counter starts at 4 (1 to_confirm + 3 open gaps).
        first = await client.get("/ui/reconcile", params={"period": PERIOD})
        assert '<span id="recon-open-count">4</span>' in first.text

        view = await _view(client)
        pair = view["to_confirm"][0]  # Delta
        resp = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "confirm",
                "transaction_id": pair["transaction"]["id"],
                "statement_line_id": pair["statement_line"]["id"],
                "period": PERIOD,
            },
        )
        assert resp.status_code == 200
        assert 'id="recon-open-count"' in resp.text and 'hx-swap-oob="true"' in resp.text
        assert ">3<" in resp.text  # counter decremented
        assert "recon-item" not in resp.text  # only the OOB counter — the card is removed

        assert ui.reconciliations_path.exists()
        again = await client.get("/ui/reconcile", params={"period": PERIOD})
    # The confirmed pair drops to the resolved trail; no open to-confirm card remains.
    assert "to-confirm" not in again.text
    assert "confirmed" in again.text  # resolved trail badge


async def test_one_projection_view_queue_ledger_agree(ui: ReconUi):
    """AC1: a confirmed pair reads `confirmed` and an acknowledged gap `gap_acknowledged`
    in ALL of: the JSON /reconcile/view, the queue HTML resolved trail, and the ledger
    annotation — the three surfaces over the same stores agree."""
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        view = await _view(client)
        delta = next(p for p in view["to_confirm"] if p["transaction"]["vendor"] == "Delta Airlines")
        staples = next(g for g in view["gaps"] if g["kind"] == "amount_mismatch")

        # Confirm the divergent pair; acknowledge the amount_mismatch. Also
        # categorize-confirm both txns so they render (with a badge) in the ledger table.
        await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "confirm",
                "transaction_id": delta["transaction"]["id"],
                "statement_line_id": delta["statement_line"]["id"],
                "period": PERIOD,
            },
        )
        await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "acknowledge",
                "transaction_id": staples["transaction"]["id"],
                "statement_line_id": staples["statement_line"]["id"],
                "note": "vendor confirmed the $2.50",
                "period": PERIOD,
            },
        )
        await client.post(
            "/ui/resolve",
            data={"transaction_id": delta["transaction"]["id"], "account": "5200-travel", "period": PERIOD},
        )
        await client.post(
            "/ui/resolve",
            data={"transaction_id": staples["transaction"]["id"], "account": "5000-office-supplies", "period": PERIOD},
        )

        after_view = await _view(client)
        queue = (await client.get("/ui/reconcile", params={"period": PERIOD})).text
        ledger_json = (await client.get("/ledger", params={"period": PERIOD})).json()
        ledger_html = (await client.get("/ui/ledger", params={"period": PERIOD})).text

    # The JSON view: pair carries `confirmed`, the gap `gap_acknowledged`.
    v_pair = next(p for p in after_view["to_confirm"] if p["transaction"]["vendor"] == "Delta Airlines")
    v_gap = next(g for g in after_view["gaps"] if g["kind"] == "amount_mismatch")
    assert v_pair["status"] == "confirmed"
    assert v_gap["status"] == "gap_acknowledged"
    assert v_gap["note"] == "vendor confirmed the $2.50"

    # The queue HTML resolved trail carries both statuses verbatim, with the note.
    resolved = queue.split("Resolved")[1]
    assert "status-confirmed" in resolved
    assert "status-gap_acknowledged" in resolved
    assert "vendor confirmed the $2.50" in resolved

    # The ledger annotation (JSON) folds to the same statuses per transaction.
    by_vendor = {e["transaction"]["vendor"]: e for e in ledger_json["entries"]}
    assert by_vendor["Delta Airlines"]["reconciliation"] == "confirmed"
    assert by_vendor["Staples"]["reconciliation"] == "gap_acknowledged"
    # The ledger HTML badge shows the same standing on the (categorize-confirmed) rows.
    assert "recon-confirmed" in ledger_html
    assert "recon-gap_acknowledged" in ledger_html


# --- AC4: UI resolve validation (identical guards, defensive machine 4xx) -----


async def test_resolve_422_unknown_decision(ui: ReconUi):
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        pair = (await _view(client))["to_confirm"][0]
        resp = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "frobnicate",
                "transaction_id": pair["transaction"]["id"],
                "statement_line_id": pair["statement_line"]["id"],
                "note": "x",
                "period": PERIOD,
            },
        )
    assert resp.status_code == 422
    assert not ui.reconciliations_path.exists()


async def test_resolve_422_both_ids_null(ui: ReconUi):
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        resp = await client.post(
            "/ui/reconcile/resolve",
            data={"decision": "acknowledge", "note": "seen it", "period": PERIOD},
        )
    assert resp.status_code == 422
    assert not ui.reconciliations_path.exists()


async def test_resolve_422_pair_decision_missing_an_id(ui: ReconUi):
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        pair = (await _view(client))["to_confirm"][0]
        resp = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "confirm",
                "transaction_id": pair["transaction"]["id"],
                "period": PERIOD,  # statement_line_id omitted → posts as ""
            },
        )
    assert resp.status_code == 422
    assert not ui.reconciliations_path.exists()


async def test_resolve_422_blank_required_note(ui: ReconUi):
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        pair = (await _view(client))["to_confirm"][0]
        resp = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "reject",
                "transaction_id": pair["transaction"]["id"],
                "statement_line_id": pair["statement_line"]["id"],
                "note": "   ",  # whitespace-only is blank
                "period": PERIOD,
            },
        )
    assert resp.status_code == 422
    assert not ui.reconciliations_path.exists()


async def test_resolve_404_unknown_ids(ui: ReconUi):
    """AC4 / N1: a supplied id absent from its store is a strict 404 (defensive —
    unreachable from the rendered queue), persisting nothing — each side."""
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        pair = (await _view(client))["to_confirm"][0]

        # Real txn + bogus statement id → 404 on the statement side.
        resp = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "confirm",
                "transaction_id": pair["transaction"]["id"],
                "statement_line_id": "not-a-real-statement-id",
                "period": PERIOD,
            },
        )
        assert resp.status_code == 404

        # Bogus txn + real statement id → 404 on the ledger side.
        resp = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "confirm",
                "transaction_id": "not-a-real-transaction-id",
                "statement_line_id": pair["statement_line"]["id"],
                "period": PERIOD,
            },
        )
        assert resp.status_code == 404
    assert not ui.reconciliations_path.exists()  # nothing dangled against nothing


async def test_resolve_one_sided_gap_acknowledge_works(ui: ReconUi):
    """A one-sided gap acknowledges with only its present id — the empty other id
    (posted as "") is normalized to None, so the both-ids-null guard doesn't misfire."""
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        view = await _view(client)
        on_statement = next(g for g in view["gaps"] if g["kind"] == "unmatched_on_statement")
        in_ledger = next(g for g in view["gaps"] if g["kind"] == "unmatched_in_ledger")

        # unmatched_on_statement carries only a transaction id.
        r1 = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "acknowledge",
                "transaction_id": on_statement["transaction"]["id"],
                "statement_line_id": "",
                "note": "duplicate — already booked",
                "period": PERIOD,
            },
        )
        # unmatched_in_ledger carries only a statement id.
        r2 = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "acknowledge",
                "transaction_id": "",
                "statement_line_id": in_ledger["statement_line"]["id"],
                "note": "will book it",
                "period": PERIOD,
            },
        )
        assert r1.status_code == 200 and r2.status_code == 200
        after = await _view(client)
    ack = {g["kind"]: g["status"] for g in after["gaps"]}
    assert ack["unmatched_on_statement"] == "gap_acknowledged"
    assert ack["unmatched_in_ledger"] == "gap_acknowledged"


# --- AC8: view-delta money pin ----------------------------------------------


async def test_view_delta_money_pin(ui: ReconUi):
    """AC8: the amount_mismatch `delta` is the exact signed string `-2.50` off the view
    AND the rendered queue — the figure a human reads to judge the discrepancy."""
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        view = await _view(client)
        html = (await client.get("/ui/reconcile", params={"period": PERIOD})).text
    mismatch = next(g for g in view["gaps"] if g["kind"] == "amount_mismatch")
    assert mismatch["delta"] == "-2.50"  # signed, exact, trailing zero preserved
    assert "delta -2.50" in html


# --- AC9: decision-kind → status lock ---------------------------------------


async def test_confirm_keyed_to_amount_mismatch_stays_gap_open(ui: ReconUi):
    """AC9: a `confirm` recorded on an amount_mismatch's (transaction_id,
    statement_line_id) does NOT resolve the gap — the gap overlay fires only on
    `acknowledge`, so the gap still renders `gap_open` in the view and as an open
    card in the queue."""
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        view = await _view(client)
        mismatch = next(g for g in view["gaps"] if g["kind"] == "amount_mismatch")

        resp = await client.post(
            "/ui/reconcile/resolve",
            data={
                "decision": "confirm",  # a pair decision, keyed to a gap
                "transaction_id": mismatch["transaction"]["id"],
                "statement_line_id": mismatch["statement_line"]["id"],
                "period": PERIOD,
            },
        )
        assert resp.status_code == 200  # recorded (audit trail), but not a gap ack

        after = await _view(client)
        html = (await client.get("/ui/reconcile", params={"period": PERIOD})).text
    v_gap = next(g for g in after["gaps"] if g["kind"] == "amount_mismatch")
    assert v_gap["status"] == "gap_open"  # a confirm never acknowledges a gap
    # Still an open amount_mismatch card (not dropped to the resolved trail).
    assert 'class="recon-item card gap amount_mismatch"' in html


# --- AC7: the ledger fold ----------------------------------------------------


async def test_ledger_fold_badge_and_summary(ui: ReconUi):
    """AC7: the ledger renders a per-row reconciliation badge and a reconcile summary
    line, both off the shared projection."""
    async with _client(ui.app) as client:
        await _import_demo(client, ui.examples_dir)
        # Confirm a category for a reconcile-matched txn so it lands in the ledger table.
        view = await _view(client)
        blue = next(m for m in view["matched"] if m["transaction"]["vendor"] == "Blue Bottle Coffee")
        await client.post(
            "/ui/resolve",
            data={"transaction_id": blue["transaction"]["id"], "account": "5300-meals-entertainment", "period": PERIOD},
        )
        resp = await client.get("/ui/ledger", params={"period": PERIOD})
    html = resp.text
    # The summary line, off build_reconciliation (2 matched, 1 awaiting, 3 gaps open).
    assert "Reconcile: 2 matched" in html
    assert "1 awaiting confirmation" in html
    assert "3 gaps open" in html
    # The per-row badge on the confirmed, matched row.
    assert "recon-badge recon-matched" in html


async def test_ledger_summary_no_statement(ui: ReconUi):
    """AC7 (guard): with no statement imported the summary reads 'no statement imported',
    never an all-zero tally that would read as 'reconciled'."""
    async with _client(ui.app) as client:
        await _import_transactions(client, ui.examples_dir)
        resp = await client.get("/ui/ledger", params={"period": PERIOD})
    assert "Reconcile: no statement imported" in resp.text
