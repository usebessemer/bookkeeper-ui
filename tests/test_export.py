"""Slice 4 · B — the export write path: exporter.py + POST /export + GET /exports.

Drives `create_app` with injected temp-path stores (the Slice 1/2/3/4-A style) plus
an injected `export_dir` under `tmp_path`, and exercises the §5.4 write path:

- `POST /export` — re-obtains the close server-side (the same `build_package`
  projection `GET /package` reads), gates on the rebuilt PROPOSED status, and only
  then writes a fresh `exports/<export_id>/` folder (four Core files) + one
  append-only log row. A BLOCKED rebuild is a 409 quoting `unmet_close`, nothing
  written.
- `GET /exports` — the append-only log in export order (read-only).
- `exporter.export_package` / `FileExportStore` — the folder writer + the log store.

The framework skill (`generate_accountant_package`, via `build_package`) is called
**as-is**; the app writes only local files (no transmission of any kind). Scaffolding
mirrors `tests/test_package.py`; builders come from `tests/conftest.py`.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper.config import BookkeeperConfig

from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore
from bookkeeper_ui.api import build_app_from_env, create_app
from bookkeeper_ui.closes import FileCloseStore
from bookkeeper_ui.confirmations import (
    SOURCE_HUMAN,
    Confirmation,
    FileConfirmationStore,
)
from bookkeeper_ui.exporter import export_package
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore
from bookkeeper_ui.views import build_package
from bookkeeper_ui.waivers import FileWaiverStore, Waiver
from tests.conftest import make_stmt_line, make_txn

PERIOD = "2026-Q2"
CORE_FILES = {"package.json", "entries.csv", "tax_summary.csv", "manifest.json"}
HASHED_FILES = {"package.json", "entries.csv", "tax_summary.csv"}


# --- Harness + config builders (mirrors test_package.py) ----------------------


@dataclass
class Harness:
    app: FastAPI
    config: BookkeeperConfig
    data_dir: Path
    export_dir: Path
    ledger_store: FileLedgerStore
    confirmation_store: FileConfirmationStore
    statement_store: FileStatementStore
    reconciliation_store: FileReconciliationStore
    close_store: FileCloseStore
    anomaly_review_store: FileAnomalyReviewStore
    waiver_store: FileWaiverStore

    @property
    def log_path(self) -> Path:
        return self.export_dir / "exports.jsonl"


def _write_config(examples_dir: Path, tmp_path: Path, **overrides: object) -> BookkeeperConfig:
    """Load the shipped example config (with optional overrides) as a `BookkeeperConfig`."""
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    if "tax_regime" in overrides:
        data["tax_regime"] = overrides["tax_regime"]
    return BookkeeperConfig.from_mapping(data)


def _harness(examples_dir: Path, tmp_path: Path, **overrides: object) -> Harness:
    config = _write_config(examples_dir, tmp_path, **overrides)
    export_dir = tmp_path / "exports"
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
        export_dir=export_dir,
    )
    return Harness(
        app, config, tmp_path, export_dir, ledger_store, confirmation_store,
        statement_store, reconciliation_store, close_store, anomaly_review_store,
        waiver_store,
    )


@pytest.fixture
def harness(examples_dir, tmp_path) -> Harness:
    return _harness(examples_dir, tmp_path)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _post_export(app: FastAPI, period: str = PERIOD) -> httpx.Response:
    async with _client(app) as client:
        return await client.post("/export", params={"period": period})


# Grounded against the shipped example config (see test_package.py):
#   "AWS"  → owner-rule proposal (5100), conf 1.0.  "Rent" → chart-match (6100), 0.9.
#   "Zzxq Gibberish" → below the categorize floor → FLAGGED (blocks until confirmed).
def _aws(amount: str = "50.00", tax: str = "0") -> object:
    return make_txn(vendor="AWS", amount=amount, tax=tax, date=datetime(2026, 5, 1), description="cloud")


def _rent() -> object:
    return make_txn(vendor="Rent", amount="20.00", tax="0", date=datetime(2026, 5, 2), description="")


def _flagged() -> object:
    return make_txn(vendor="Zzxq Gibberish", amount="5.00", tax="0", date=datetime(2026, 5, 3), description="")


async def _waive(
    waiver_store: FileWaiverStore, period: str = PERIOD
) -> None:
    """Waive reconciliation so a no-statement period reads clean (source='waived')."""
    await waiver_store.record(
        Waiver(period=period, waived_at=datetime(2026, 7, 1, tzinfo=timezone.utc), waived_by="human", note="")
    )


async def _seed_ready(
    ledger_store: FileLedgerStore,
    confirmation_store: FileConfirmationStore,
    waiver_store: FileWaiverStore,
) -> object:
    """Seed a READY close (all three proposal sources + a waiver) → a PROPOSED package.

    AWS (owner-rule) · Rent (chart-match) · Zzxq (flagged → confirmed → human). The
    confirmed flag clears `categorization_complete`; the waiver clears reconciliation.
    Returns the flagged (now human-confirmed) transaction. Works over any set of
    stores so both the harness and the `build_app_from_env` env tests reuse it.
    """
    await ledger_store.store(_aws())
    await ledger_store.store(_rent())
    flagged = _flagged()
    await ledger_store.store(flagged)
    await confirmation_store.record(
        Confirmation(transaction_id=transaction_key(flagged), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    )
    await _waive(waiver_store)
    return flagged


async def _ready_three_sources(h: Harness) -> object:
    return await _seed_ready(h.ledger_store, h.confirmation_store, h.waiver_store)


def _log_rows(harness: Harness) -> list[str]:
    if not harness.log_path.exists():
        return []
    return [line for line in harness.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _export_folders(harness: Harness) -> list[Path]:
    if not harness.export_dir.exists():
        return []
    return sorted(p for p in harness.export_dir.iterdir() if p.is_dir())


# Every input-store file `build_package` reads on the export rebuild — snapshotted to
# prove the refusal path is read-only (a file that does not exist snapshots as None).
_INPUT_STORE_FILES = (
    "ledger.jsonl",
    "confirmations.jsonl",
    "statements.jsonl",
    "reconciliations.jsonl",
    "closes.jsonl",
    "anomaly_reviews.jsonl",
    "reconciliation_waivers.jsonl",
)


def _input_snapshot(data_dir: Path) -> dict[str, bytes | None]:
    return {
        name: ((data_dir / name).read_bytes() if (data_dir / name).exists() else None)
        for name in _INPUT_STORE_FILES
    }


# ============================================================================
# AC-15 — a successful export: four Core files, one log row, matching hashes
# ============================================================================


async def test_successful_export_writes_four_core_files_one_row_matching_hashes(harness: Harness):
    """A PROPOSED close → 200; the folder holds exactly the four Core files; the log
    gains exactly one row; each manifest sha256/bytes matches the file's exact bytes;
    the manifest excludes itself from hashing."""
    await _ready_three_sources(harness)
    resp = await _post_export(harness.app)
    assert resp.status_code == 200
    result = resp.json()
    assert result["package_status"] == "proposed"

    folder = harness.export_dir / result["export_id"]
    assert {p.name for p in folder.iterdir()} == CORE_FILES

    # Exactly one appended log row.
    assert len(_log_rows(harness)) == 1

    # The manifest hashes the OTHER three files; each hash + byte count matches disk.
    manifest = json.loads((folder / "manifest.json").read_bytes())
    hashed = {f["name"]: f for f in manifest["files"]}
    assert set(hashed) == HASHED_FILES  # never manifest.json itself
    for name, meta in hashed.items():
        data = (folder / name).read_bytes()
        assert meta["sha256"] == hashlib.sha256(data).hexdigest()
        assert meta["bytes"] == len(data)

    # The result echoes the same three hashed files.
    assert {f["name"] for f in result["files"]} == HASHED_FILES
    # Manifest basis is stamped verbatim from config.
    assert manifest["basis"] == {
        "accounting_method": harness.config.accounting_method,
        "jurisdiction": harness.config.jurisdiction,
        "tax_regime": harness.config.tax_regime,
        "accountant_format": harness.config.accountant_format,
    }

    # The manifest's non-hash body fields (dropping/corrupting any of these otherwise
    # survives — AC-15 pins only `files` + `basis`).
    assert manifest["export_id"] == result["export_id"]
    assert manifest["period"] == PERIOD
    assert manifest["package_status"] == "proposed"
    assert manifest["app_version"]  # non-empty
    parsed = datetime.fromisoformat(manifest["exported_at"])  # parseable ISO 8601
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0  # UTC


async def test_package_json_is_the_get_package_serialization_verbatim(harness: Harness):
    """package.json equals the GET /package body verbatim (same projection, serialized once)."""
    await _ready_three_sources(harness)
    resp = await _post_export(harness.app)
    folder = harness.export_dir / resp.json()["export_id"]
    written = json.loads((folder / "package.json").read_bytes())
    async with _client(harness.app) as client:
        preview = (await client.get("/package", params={"period": PERIOD})).json()
    assert written == preview


# ============================================================================
# AC-16 — re-export appends a new folder + row; prior untouched (append-only)
# ============================================================================


async def test_reexport_same_period_appends_new_folder_and_row_prior_untouched(harness: Harness):
    """Two exports of the same period → two distinct export_ids, two folders, two log
    rows; the first folder's bytes are byte-identical before/after the second export."""
    await _ready_three_sources(harness)
    r1 = (await _post_export(harness.app)).json()
    folder1 = harness.export_dir / r1["export_id"]
    before = {p.name: p.read_bytes() for p in folder1.iterdir()}

    r2 = (await _post_export(harness.app)).json()
    assert r1["export_id"] != r2["export_id"]
    assert len(_export_folders(harness)) == 2
    assert len(_log_rows(harness)) == 2

    after = {p.name: p.read_bytes() for p in folder1.iterdir()}
    assert after == before  # the first export was never rewritten


async def test_export_id_collision_is_a_hard_error_prior_untouched(harness: Harness):
    """The no-clobber defense (`exporter.py`'s `folder.mkdir(exist_ok=False)`): two
    exports of the same period at the SAME `exported_at` map to the SAME export_id — the
    second raises FileExistsError and the first export's bytes are left untouched (never
    a silent overwrite). Drives `export_package` directly with a fixed timestamp, since
    the HTTP route mints its own (microsecond-resolution) time per call — the collision
    is otherwise unreachable through the endpoint."""
    await _ready_three_sources(harness)
    package = await build_package(
        config=harness.config,
        ledger_store=harness.ledger_store,
        confirmation_store=harness.confirmation_store,
        statement_store=harness.statement_store,
        reconciliation_store=harness.reconciliation_store,
        close_store=harness.close_store,
        anomaly_review_store=harness.anomaly_review_store,
        waiver_store=harness.waiver_store,
        period=PERIOD,
    )
    assert package.status == "proposed"
    fixed = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)

    first = export_package(
        package=package, config=harness.config, export_dir=harness.export_dir,
        exported_at=fixed, app_version="test",
    )
    folder = harness.export_dir / first.export_id
    before = {p.name: p.read_bytes() for p in folder.iterdir()}

    # A second export at the identical timestamp collides onto the identical folder.
    with pytest.raises(FileExistsError):
        export_package(
            package=package, config=harness.config, export_dir=harness.export_dir,
            exported_at=fixed, app_version="test",
        )

    after = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert after == before  # the prior export was never overwritten


# ============================================================================
# divergence_count flows package → POST result → manifest → log (exact value)
# ============================================================================


async def test_divergence_count_flows_package_to_result_manifest_and_log(harness: Harness):
    """A human correction (a confirmation overriding a proposal to a DIFFERENT account)
    makes divergence_count == 1; that exact value flows package → the POST result →
    manifest.json → the append-only log row (a mutation hardcoding 0 anywhere on the
    write path is caught)."""
    aws = _aws()
    await harness.ledger_store.store(aws)
    await harness.ledger_store.store(_rent())
    await harness.confirmation_store.record(  # correct AWS away from its 5100 proposal
        Confirmation(transaction_id=transaction_key(aws), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    )
    await _waive(harness.waiver_store)

    # The preview carries the divergence.
    async with _client(harness.app) as client:
        preview = (await client.get("/package", params={"period": PERIOD})).json()
    assert preview["divergence_count"] == 1

    resp = await _post_export(harness.app)
    assert resp.status_code == 200
    result = resp.json()
    assert result["divergence_count"] == 1  # the POST result

    folder = harness.export_dir / result["export_id"]
    manifest = json.loads((folder / "manifest.json").read_bytes())
    assert manifest["divergence_count"] == 1  # the manifest body

    rows = _log_rows(harness)
    assert len(rows) == 1
    assert json.loads(rows[0])["divergence_count"] == 1  # the append-only log row


# ============================================================================
# AC-1 / AC-2 — a BLOCKED rebuild is a 409 that writes NOTHING
# ============================================================================


async def test_export_rebuild_reads_blocked_status_server_side(harness: Harness):
    """AC-1: with a BLOCKED close (an unresolved flag), the server-side rebuild reads
    status='blocked' with unmet_close naming the blocking check — surfaced in the 409."""
    await harness.ledger_store.store(_flagged())  # blocks categorization_complete
    await _waive(harness.waiver_store)  # isolate the flag as the sole blocker
    resp = await _post_export(harness.app)
    assert resp.status_code == 409
    assert "categorization_complete" in resp.json()["detail"]


async def test_export_blocked_writes_nothing_when_no_prior_export(harness: Harness):
    """AC-2 (still-absent): a BLOCKED package → 409 quoting unmet_close; no folder is
    created and no log row is written (the export dir stays absent)."""
    await harness.ledger_store.store(_flagged())
    await _waive(harness.waiver_store)
    assert not harness.export_dir.exists()

    resp = await _post_export(harness.app)
    assert resp.status_code == 409
    assert resp.json()["detail"]  # unmet_close verbatim, non-null

    # Mutation-proven: nothing was written.
    assert not harness.log_path.exists()
    assert _export_folders(harness) == []


async def test_export_blocked_after_prior_export_is_byte_identical(harness: Harness):
    """AC-2 (byte-identical): after a successful export, a later BLOCKED export → 409
    leaves exports.jsonl byte-identical and adds no second folder."""
    await _ready_three_sources(harness)
    ok = (await _post_export(harness.app)).json()
    log_before = harness.log_path.read_bytes()
    folders_before = _export_folders(harness)

    # Now block the period: a ghost statement line makes reconciliation not clean
    # (a waiver is ignored once a statement exists).
    await harness.statement_store.store(
        make_stmt_line(statement_ref="STMT-GHOST", amount="999.99",
                       date=datetime(2026, 5, 20), description="Ghost charge")
    )
    resp = await _post_export(harness.app)
    assert resp.status_code == 409

    assert harness.log_path.read_bytes() == log_before  # not one byte added
    assert _export_folders(harness) == folders_before  # no second folder
    assert (harness.export_dir / ok["export_id"]).exists()  # the first survives


# ============================================================================
# AC-3 — the rebuild path is read-only on refusal (input stores untouched)
# ============================================================================


async def test_export_rebuild_is_read_only_on_refusal(harness: Harness):
    """AC-3: a refused export leaves ledger.jsonl / confirmations.jsonl byte-identical
    and the export dir absent — build_package only reads its stores + runs pure skills."""
    await harness.ledger_store.store(_flagged())
    await harness.confirmation_store.record(
        Confirmation(transaction_id="does-not-matter", account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    )
    await _waive(harness.waiver_store)
    # Sweep EVERY input store build_package reads on the rebuild — not just ledger +
    # confirmations (it also reads statements / reconciliations / closes / waivers).
    before = _input_snapshot(harness.data_dir)

    resp = await _post_export(harness.app)
    assert resp.status_code == 409

    assert _input_snapshot(harness.data_dir) == before  # not one input-store byte changed
    assert not harness.export_dir.exists()


# ============================================================================
# AC-4 — no silent auto-commit: only POST /export writes an export
# ============================================================================


async def test_no_export_is_written_without_an_explicit_post(harness: Harness):
    """AC-4: previewing (GET /package), reading the close/ledger, and listing exports
    write no export folder and no log row — an export happens only via POST /export."""
    await _ready_three_sources(harness)
    async with _client(harness.app) as client:
        assert (await client.get("/package", params={"period": PERIOD})).status_code == 200
        assert (await client.get("/close", params={"period": PERIOD})).status_code == 200
        assert (await client.get("/ledger", params={"period": PERIOD})).status_code == 200
        assert (await client.get("/exports")).json() == []  # reads only, no side effect

    assert not harness.log_path.exists()
    assert _export_folders(harness) == []


# ============================================================================
# AC-5 — export re-obtains the close server-side (never rides stale preview)
# ============================================================================


async def test_export_reobtains_close_and_never_rides_stale_preview(harness: Harness):
    """AC-5: a period that previews PROPOSED but is made BLOCKED before the POST gets a
    409 — the export rebuilds from its own stores, never trusting the earlier preview."""
    await _ready_three_sources(harness)
    async with _client(harness.app) as client:
        preview = (await client.get("/package", params={"period": PERIOD})).json()
    assert preview["status"] == "proposed"

    # Introduce an unreconciled gap AFTER the successful preview.
    await harness.statement_store.store(
        make_stmt_line(statement_ref="STMT-GHOST", amount="999.99",
                       date=datetime(2026, 5, 20), description="Ghost charge")
    )
    resp = await _post_export(harness.app)
    assert resp.status_code == 409
    assert "reconciliation_clean" in resp.json()["detail"]
    assert not harness.log_path.exists()
    assert _export_folders(harness) == []


# ============================================================================
# Money exactness — exact str(Decimal) everywhere, no float artefacts
# ============================================================================


async def test_exported_money_is_exact_strings_no_float_artefacts(harness: Harness):
    """Every money field in package.json / entries.csv / tax_summary.csv is the exact
    str(Decimal) — trailing zeros preserved ('82.50'), 0.10+0.20 → '0.30' (never
    0.30000000000000004), never a JSON number."""
    await harness.ledger_store.store(_aws(amount="82.50", tax="0.10"))
    await harness.ledger_store.store(_aws(amount="60.00", tax="0.20"))
    await _waive(harness.waiver_store)

    resp = await _post_export(harness.app)
    assert resp.status_code == 200
    folder = harness.export_dir / resp.json()["export_id"]

    # package.json — money as exact strings.
    pkg = json.loads((folder / "package.json").read_bytes())
    assert pkg["tax_breakout"]["period_total"] == "0.30"
    assert isinstance(pkg["tax_breakout"]["period_total"], str)
    assert {e["transaction"]["amount"] for e in pkg["entries"]} == {"82.50", "60.00"}

    # entries.csv — exact strings, no float drift.
    entries_text = (folder / "entries.csv").read_text(encoding="utf-8")
    assert "82.50" in entries_text
    assert "0.30000" not in entries_text

    # tax_summary.csv — per-target + a PERIOD_TOTAL row carrying regime + period_total.
    tax_text = (folder / "tax_summary.csv").read_text(encoding="utf-8")
    assert "target-001,2,0.30" in tax_text
    assert "PERIOD_TOTAL,HST,0.30" in tax_text
    assert "0.30000000" not in tax_text


# ============================================================================
# entries.csv shape — both account columns; human line blanks confidence
# ============================================================================


async def test_entries_csv_columns_and_human_confirmed_line(harness: Harness):
    """entries.csv carries both the framework proposal and the human confirmation
    columns; a human-source line writes 'human-confirmed' in source_rule and leaves
    confidence blank; agent lines keep their true source rule + confidence."""
    flagged = await _ready_three_sources(harness)
    resp = await _post_export(harness.app)
    folder = harness.export_dir / resp.json()["export_id"]
    rows = list(csv.DictReader((folder / "entries.csv").read_text(encoding="utf-8").splitlines()))
    assert set(rows[0].keys()) == {
        "date", "vendor", "description", "attribution_target_id", "amount", "tax",
        "proposed_account", "confidence", "source_rule", "confirmed_account",
        "confirmed_at", "transaction_id",
    }
    by_txn = {r["transaction_id"]: r for r in rows}

    human = by_txn[transaction_key(flagged)]
    assert human["source_rule"] == "human-confirmed"
    assert human["confidence"] == ""  # the synthetic 1.0 is never written
    assert human["confirmed_account"] == "5000-office-supplies"
    assert human["confirmed_at"]  # ISO timestamp, present

    by_vendor = {r["vendor"]: r for r in rows}
    assert by_vendor["AWS"]["source_rule"] == "owner-rule"
    assert by_vendor["AWS"]["confidence"] == "1.0"
    assert by_vendor["Rent"]["source_rule"] == "chart-match"
    assert by_vendor["Rent"]["confidence"] == "0.9"
    # An unconfirmed agent line leaves the confirmation columns empty.
    assert by_vendor["AWS"]["confirmed_account"] == ""
    assert by_vendor["AWS"]["confirmed_at"] == ""


# ============================================================================
# GET /exports — the append-only log, in order
# ============================================================================


async def test_get_exports_lists_the_log_in_export_order(harness: Harness):
    """GET /exports returns every export in insertion order; empty before any export."""
    async with _client(harness.app) as client:
        assert (await client.get("/exports")).json() == []

    await _ready_three_sources(harness)
    r1 = (await _post_export(harness.app)).json()
    r2 = (await _post_export(harness.app)).json()

    async with _client(harness.app) as client:
        listing = (await client.get("/exports")).json()
    assert [e["export_id"] for e in listing] == [r1["export_id"], r2["export_id"]]
    assert all(e["package_status"] == "proposed" for e in listing)
    assert all(set(e.keys()) == {
        "export_id", "period", "package_status", "exported_at", "files", "divergence_count",
    } for e in listing)


# ============================================================================
# Unknown tax regime surfaces as 400 (mirrors GET /package)
# ============================================================================


async def test_export_unknown_tax_regime_surfaces_as_400(examples_dir, tmp_path):
    """An unregistered tax_regime makes the server-side rebuild surface the framework
    error (400), never a swallowed 200 — and nothing is written."""
    h = _harness(examples_dir, tmp_path, tax_regime="VAT")
    await h.ledger_store.store(_aws())
    await _waive(h.waiver_store)
    resp = await _post_export(h.app)
    assert resp.status_code == 400
    assert "Unknown tax_regime" in resp.json()["detail"]
    assert not h.export_dir.exists()


# ============================================================================
# 503 when the export dir is unwired (pre-Slice-4 call sites keep working)
# ============================================================================


async def test_export_routes_503_when_export_dir_unwired(examples_dir, tmp_path):
    """A create_app built WITHOUT export_dir (a Slice-1/2/3 call site) refuses both
    export routes with 503 — never a silent no-op."""
    config = _write_config(examples_dir, tmp_path)
    app = create_app(
        config=config,
        ledger_store=FileLedgerStore(tmp_path / "ledger.jsonl"),
        confirmation_store=FileConfirmationStore(tmp_path / "confirmations.jsonl"),
        statement_store=FileStatementStore(tmp_path / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(tmp_path / "reconciliations.jsonl"),
        close_store=FileCloseStore(tmp_path / "closes.jsonl"),
        anomaly_review_store=FileAnomalyReviewStore(tmp_path / "anomaly_reviews.jsonl"),
        waiver_store=FileWaiverStore(tmp_path / "reconciliation_waivers.jsonl"),
    )
    async with _client(app) as client:
        assert (await client.post("/export", params={"period": PERIOD})).status_code == 503
        assert (await client.get("/exports")).status_code == 503


# ============================================================================
# AC-18 — build_app_from_env honors BOOKKEEPER_UI_EXPORT_DIR + the default
# ============================================================================


def _set_env(monkeypatch, examples_dir, tmp_path, data_dir: Path, export_dir: Path | None) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text((examples_dir / "config.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("BOOKKEEPER_UI_CONFIG", str(config_path))
    monkeypatch.setenv("BOOKKEEPER_UI_DATA_DIR", str(data_dir))
    if export_dir is None:
        monkeypatch.delenv("BOOKKEEPER_UI_EXPORT_DIR", raising=False)
    else:
        monkeypatch.setenv("BOOKKEEPER_UI_EXPORT_DIR", str(export_dir))


async def test_build_app_from_env_honors_export_dir_env(examples_dir, tmp_path, monkeypatch):
    """AC-18: BOOKKEEPER_UI_EXPORT_DIR is honored — the export lands under it."""
    data_dir = tmp_path / "data"
    export_dir = tmp_path / "custom-exports"
    _set_env(monkeypatch, examples_dir, tmp_path, data_dir, export_dir)
    app = build_app_from_env()

    await _seed_ready(
        FileLedgerStore(data_dir / "ledger.jsonl"),
        FileConfirmationStore(data_dir / "confirmations.jsonl"),
        FileWaiverStore(data_dir / "reconciliation_waivers.jsonl"),
    )
    resp = await _post_export(app)
    assert resp.status_code == 200
    export_id = resp.json()["export_id"]
    assert (export_dir / export_id / "manifest.json").exists()
    assert (export_dir / "exports.jsonl").exists()


async def test_build_app_from_env_defaults_export_dir_under_data_dir(examples_dir, tmp_path, monkeypatch):
    """AC-18: unset BOOKKEEPER_UI_EXPORT_DIR → default <data_dir>/exports."""
    data_dir = tmp_path / "data"
    _set_env(monkeypatch, examples_dir, tmp_path, data_dir, export_dir=None)
    app = build_app_from_env()

    await _seed_ready(
        FileLedgerStore(data_dir / "ledger.jsonl"),
        FileConfirmationStore(data_dir / "confirmations.jsonl"),
        FileWaiverStore(data_dir / "reconciliation_waivers.jsonl"),
    )
    resp = await _post_export(app)
    assert resp.status_code == 200
    export_id = resp.json()["export_id"]
    assert (data_dir / "exports" / export_id / "manifest.json").exists()
    assert (data_dir / "exports" / "exports.jsonl").exists()
