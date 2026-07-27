"""Slice 6 · #85 — the backup engine wired into app assembly.

Drives the full app (`create_app` + the mounted HTML twins) with a **real**
`BackupService` over the data dir, and asserts the two things the wiring owes:

- every banked business event fires exactly ONE semantic commit (import once for the
  whole batch, not per row) — on Javed's HTML surface AND the JSON surface;
- the wiring is off the request path and born-inert: a doomed push never blocks the
  sign handler (the local commit — the durability floor — still lands, 200 still
  returned), and with no service wired (`backup=None`) the app is exactly pre-feature
  (no `.git`, unchanged behaviour).

Git assertions read the tmp repo directly (blocking `subprocess.run` is fine test-side);
the service under test always uses async exec. The push has no reachable `origin` in
most tests, so its scheduled cycle is a fast unconfigured no-op that `_settle` drains.
"""

from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.flag_anomaly import flag_anomaly

import bookkeeper_ui.backup as backup_mod
from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore, derive_flag_id
from bookkeeper_ui.api import create_app
from bookkeeper_ui.backup import BackupService
from bookkeeper_ui.candidates import (
    FileArtifactStore,
    FileCandidateDecisionStore,
    FileCandidateStore,
)
from bookkeeper_ui.closes import FileCloseStore
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import SOURCE_HUMAN, Confirmation, FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore, statement_line_key
from bookkeeper_ui.waivers import FileWaiverStore, Waiver
from tests.conftest import make_stmt_line, make_txn

PERIOD = "2026-Q2"
AT = datetime(2026, 7, 1, tzinfo=timezone.utc)

# A 3-row transactions CSV, all in 2026-Q2 (none closed) — for the import-once test.
_CSV_3 = (
    b"date,vendor,amount,tax,description,attribution_target_id\n"
    b"2026-05-01,AWS,50.00,6.50,cloud,target-001\n"
    b"2026-05-02,GitHub,21.00,0.00,subscription,target-002\n"
    b"2026-05-03,Staples,82.50,6.60,office,target-001\n"
)

_RECEIPT = b"\xff\xd8\xff\x00 a small sample receipt jpeg \x01\x02\x03"


# --- Harness ----------------------------------------------------------------


@dataclass
class Wired:
    app: FastAPI
    data_dir: Path
    backup: BackupService | None
    config: BookkeeperConfig
    ledger_store: FileLedgerStore
    confirmation_store: FileConfirmationStore
    statement_store: FileStatementStore
    waiver_store: FileWaiverStore


def _wired(examples_dir: Path, tmp_path: Path, *, with_backup: bool = True) -> Wired:
    """The full app over ALL stores in one data dir, with a real `BackupService` (or none).

    The backup's data dir IS the stores' dir, so git tracks the very `*.jsonl` files the
    writes append to. `export_dir` sits inside the tree (its `exports.jsonl` log is tracked;
    the per-export blob subfolders are gitignored), and the intake stores are wired so the
    candidate reject / file-receipt twins are exercisable.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(examples_dir / "config.json")
    ledger_store = FileLedgerStore(data_dir / "ledger.jsonl")
    confirmation_store = FileConfirmationStore(data_dir / "confirmations.jsonl")
    statement_store = FileStatementStore(data_dir / "statements.jsonl")
    waiver_store = FileWaiverStore(data_dir / "reconciliation_waivers.jsonl")
    backup = BackupService(data_dir) if with_backup else None
    app = create_app(
        config=config,
        ledger_store=ledger_store,
        confirmation_store=confirmation_store,
        statement_store=statement_store,
        reconciliation_store=FileReconciliationStore(data_dir / "reconciliations.jsonl"),
        close_store=FileCloseStore(data_dir / "closes.jsonl"),
        anomaly_review_store=FileAnomalyReviewStore(data_dir / "anomaly_reviews.jsonl"),
        waiver_store=waiver_store,
        export_dir=data_dir / "exports",
        candidate_store=FileCandidateStore(data_dir / "candidates.jsonl"),
        candidate_decision_store=FileCandidateDecisionStore(
            data_dir / "candidate_decisions.jsonl"
        ),
        artifact_store=FileArtifactStore(data_dir / "artifacts"),
        backup=backup,
    )
    return Wired(
        app, data_dir, backup, config, ledger_store, confirmation_store,
        statement_store, waiver_store,
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- git inspection (test-side; blocking is fine) ---------------------------


def _git(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(data_dir), *args], capture_output=True, text=True)


def _commit_count(data_dir: Path) -> int:
    out = _git(data_dir, "rev-list", "--count", "HEAD")
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def _head_subject(data_dir: Path) -> str:
    return _git(data_dir, "log", "-1", "--format=%s").stdout.strip()


def _tracked(data_dir: Path) -> set[str]:
    return set(_git(data_dir, "ls-files").stdout.split())


async def _prebake(backup: BackupService) -> None:
    """One baseline commit so the lazy repo-init commit is out of the way — then each
    subsequent banked action lands EXACTLY one semantic commit we can assert on."""
    await backup.commit("baseline")


async def _settle(backup: BackupService | None) -> None:
    """Drain the last scheduled push cycle (a no-op with no reachable remote), so no
    pending task lingers into loop teardown."""
    if backup is not None and backup._push_task is not None:
        try:
            await backup._push_task
        except Exception:
            pass


# --- Precondition seeders (write stores directly; none of these bank) --------


async def _seed_signable(h: Wired) -> object:
    """The minimal green close via the waiver path — AWS confirmed + a reconciliation
    waiver (no statement). Framework READY + all three app gates met → signable."""
    txn = make_txn(vendor="AWS", amount="50.00", tax="6.50",
                   date=datetime(2026, 5, 1), description="cloud")
    await h.ledger_store.store(txn)
    await h.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(txn),
                     account="5100-software-subscriptions", source=SOURCE_HUMAN, decided_at=AT)
    )
    await h.waiver_store.record(
        Waiver(period=PERIOD, waived_at=AT, waived_by="human", note="no feed")
    )
    return txn


def _candidate_payload() -> dict:
    return {
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
        "artifact": base64.b64encode(_RECEIPT).decode("ascii"),
        "artifact_media_type": "image/jpeg",
    }


# ============================================================================
# born-inert: backup=None is exactly pre-feature
# ============================================================================


async def test_backup_none_creates_no_repo_and_behaves_as_before(examples_dir, tmp_path):
    """With no service wired, a banked write path runs unchanged and NO git repo appears."""
    h = _wired(examples_dir, tmp_path, with_backup=False)
    txn = make_txn(vendor="AWS", amount="50.00", tax="6.50", date=datetime(2026, 5, 1))
    await h.ledger_store.store(txn)

    async with _client(h.app) as client:
        resp = await client.post(
            "/ui/resolve",
            data={"transaction_id": transaction_key(txn),
                  "account": "5100-software-subscriptions", "period": PERIOD},
        )

    assert resp.status_code == 200
    assert not (h.data_dir / ".git").exists()  # born-inert: no repo, exactly pre-feature


# ============================================================================
# import commits ONCE regardless of row count (not per row)
# ============================================================================


async def test_import_commits_once_for_the_whole_batch(examples_dir, tmp_path):
    """A 3-row import lands exactly ONE commit (the batch is one event), and it tracks
    the ledger the rows were stored into."""
    h = _wired(examples_dir, tmp_path)
    await _prebake(h.backup)
    before = _commit_count(h.data_dir)

    async with _client(h.app) as client:
        resp = await client.post(
            "/ui/import",
            files={"file": ("transactions.csv", _CSV_3, "text/csv")},
            data={"period": PERIOD},
        )

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1  # ONE commit, not three
    assert _head_subject(h.data_dir) == "banked: import 3 transactions"
    assert "ledger.jsonl" in _tracked(h.data_dir)
    await _settle(h.backup)


# ============================================================================
# a commit lands for each banked action on the WEB TWINS (Javed's surface)
# ============================================================================


async def test_confirm_resolution_banks_a_commit(examples_dir, tmp_path):
    h = _wired(examples_dir, tmp_path)
    await _prebake(h.backup)
    txn = make_txn(vendor="AWS", amount="50.00", tax="6.50", date=datetime(2026, 5, 1))
    await h.ledger_store.store(txn)
    before = _commit_count(h.data_dir)

    async with _client(h.app) as client:
        resp = await client.post(
            "/ui/resolve",
            data={"transaction_id": transaction_key(txn),
                  "account": "5100-software-subscriptions", "period": PERIOD},
        )

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1
    assert _head_subject(h.data_dir).startswith("banked: confirm ")
    await _settle(h.backup)


async def test_reconcile_resolution_banks_a_commit(examples_dir, tmp_path):
    h = _wired(examples_dir, tmp_path)
    await _prebake(h.backup)
    txn = make_txn(date=datetime(2026, 5, 15))
    line = make_stmt_line(date=datetime(2026, 5, 15))
    await h.ledger_store.store(txn)
    await h.statement_store.store(line)
    before = _commit_count(h.data_dir)

    async with _client(h.app) as client:
        resp = await client.post(
            "/ui/reconcile/resolve",
            data={"decision": "confirm",
                  "transaction_id": transaction_key(txn),
                  "statement_line_id": statement_line_key(line),
                  "period": PERIOD},
        )

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1
    assert _head_subject(h.data_dir).startswith("banked: reconcile confirm ")
    await _settle(h.backup)


async def test_anomaly_acknowledge_banks_a_commit(examples_dir, tmp_path):
    h = _wired(examples_dir, tmp_path)
    await _prebake(h.backup)
    # A 1200 txn > the example config's 1000 materiality floor → an over_materiality flag.
    txn = make_txn(vendor="Delta Airlines", amount="1200.00", tax="0",
                   date=datetime(2026, 5, 2), description="Flight")
    await h.ledger_store.store(txn)
    report = await flag_anomaly(h.ledger_store, h.config, PERIOD)
    flag = next(f for f in report.flags if f.kind.value == "over_materiality")
    flag_id = derive_flag_id(flag)
    before = _commit_count(h.data_dir)

    async with _client(h.app) as client:
        resp = await client.post(
            "/ui/anomalies/review",
            data={"flag_id": flag_id, "period": PERIOD, "note": "expected"},
        )

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1
    assert _head_subject(h.data_dir).startswith("banked: acknowledge anomaly ")
    await _settle(h.backup)


async def test_reconciliation_waiver_banks_a_commit(examples_dir, tmp_path):
    h = _wired(examples_dir, tmp_path)
    await _prebake(h.backup)
    before = _commit_count(h.data_dir)

    async with _client(h.app) as client:
        resp = await client.post(
            "/ui/reconciliation/waive", data={"period": PERIOD, "note": "no feed"}
        )

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1
    assert _head_subject(h.data_dir) == f"banked: waive reconciliation {PERIOD}"
    await _settle(h.backup)


async def test_sign_banks_a_commit(examples_dir, tmp_path):
    h = _wired(examples_dir, tmp_path)
    await _prebake(h.backup)
    await _seed_signable(h)
    before = _commit_count(h.data_dir)

    async with _client(h.app) as client:
        resp = await client.post("/ui/sign", data={"period": PERIOD, "signed_by": "Stu"})

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1
    assert _head_subject(h.data_dir) == f"banked: signed close {PERIOD}"
    await _settle(h.backup)


async def test_export_banks_a_commit(examples_dir, tmp_path):
    h = _wired(examples_dir, tmp_path)
    await _prebake(h.backup)
    await _seed_signable(h)  # a signable close → a PROPOSED package the export writes
    before = _commit_count(h.data_dir)

    async with _client(h.app) as client:
        resp = await client.post(
            "/ui/export", data={"period": PERIOD, "acknowledged": "on"}
        )

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1
    assert _head_subject(h.data_dir) == f"banked: export {PERIOD}"
    assert "exports/exports.jsonl" in _tracked(h.data_dir)  # the log is tracked (blobs aren't)
    await _settle(h.backup)


async def test_candidate_reject_banks_a_commit(examples_dir, tmp_path):
    h = _wired(examples_dir, tmp_path)
    async with _client(h.app) as client:
        cid = (await client.post("/intake/candidates", json=_candidate_payload())).json()[
            "candidate"
        ]["candidate_id"]
        await _prebake(h.backup)  # baseline sweeps in the seeded (un-banked) candidate
        before = _commit_count(h.data_dir)
        resp = await client.post(
            "/ui/intake/resolve",
            data={"candidate_id": cid, "action": "reject",
                  "reject_reason": "duplicate", "period": PERIOD},
        )

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1
    assert _head_subject(h.data_dir) == f"banked: reject candidate {cid}"
    await _settle(h.backup)


async def test_file_receipt_confirm_banks_a_commit(examples_dir, tmp_path):
    h = _wired(examples_dir, tmp_path)
    async with _client(h.app) as client:
        cid = (await client.post("/intake/candidates", json=_candidate_payload())).json()[
            "candidate"
        ]["candidate_id"]
        await _prebake(h.backup)
        before = _commit_count(h.data_dir)
        resp = await client.post(
            "/ui/intake/resolve",
            data={"candidate_id": cid, "action": "confirm",
                  "vendor": "Home Depot", "amount": "82.50", "tax": "10.73",
                  "date": "2026-06-14", "description": "Lumber and fasteners",
                  "attribution_target_id": "target-001", "period": PERIOD},
        )

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1
    assert _head_subject(h.data_dir) == "banked: file receipt Home Depot 82.50"
    await _settle(h.backup)


# ============================================================================
# the JSON surface banks too (both surfaces, not only the HTML twins)
# ============================================================================


async def test_json_resolve_also_banks_a_commit(examples_dir, tmp_path):
    h = _wired(examples_dir, tmp_path)
    await _prebake(h.backup)
    txn = make_txn(vendor="AWS", amount="50.00", tax="6.50", date=datetime(2026, 5, 1))
    await h.ledger_store.store(txn)
    before = _commit_count(h.data_dir)

    async with _client(h.app) as client:
        resp = await client.post(
            "/resolve",
            json={"transaction_id": transaction_key(txn),
                  "account": "5100-software-subscriptions"},
        )

    assert resp.status_code == 200
    assert _commit_count(h.data_dir) == before + 1
    assert _head_subject(h.data_dir).startswith("banked: confirm ")
    await _settle(h.backup)


# ============================================================================
# a push failure never blocks the sign endpoint (still 200; the commit still lands)
# ============================================================================


async def test_push_failure_never_blocks_sign(examples_dir, tmp_path, monkeypatch):
    """A doomed push (an unreachable `origin`) must not delay or fail the sign handler:
    the local commit (the durability floor) still lands and the endpoint still returns 200.
    The push is attempted off the request path and honestly records its failure class."""
    # Collapse the retry backoff so the doomed cycle is fast to drain (its non-blocking-ness
    # is what's under test — the schedule's timing is `test_backup_service`'s job).
    monkeypatch.setattr(backup_mod, "_PUSH_BACKOFF_SCHEDULE", ())

    h = _wired(examples_dir, tmp_path)
    await _prebake(h.backup)
    # A credential-free file:// remote pointing at nothing → push fails (offline), never hangs.
    _git(h.data_dir, "remote", "add", "origin", "file:///nonexistent/remote.git")
    await _seed_signable(h)
    before = _commit_count(h.data_dir)

    async with _client(h.app) as client:
        resp = await client.post("/ui/sign", data={"period": PERIOD, "signed_by": "Stu"})

    assert resp.status_code == 200                       # the doomed push never blocked it
    assert _commit_count(h.data_dir) == before + 1        # durability floor: the commit landed
    assert _head_subject(h.data_dir) == f"banked: signed close {PERIOD}"

    await _settle(h.backup)  # let the background cycle finish (one attempt, then gives up)
    assert h.backup._last_push_error_class == "offline"   # attempted + honestly failed, off-path
