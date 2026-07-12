"""Slice 3 · C — the thin write endpoints: POST /anomalies/review + POST /reconciliation/waive.

Drives `create_app` with injected temp-path stores (the Slice 1/2 style), exercising
the two small Slice-3 writes that feed close review's gates:

- `POST /anomalies/review` — acknowledge one **current** `flag_anomaly` flag (gate B):
  422 on a non-current / stale `flag_id`, 409 on a closed period, append-only.
- `POST /reconciliation/waive` — waive reconciliation for a **no-statement** period
  (gate C): 409 when a statement exists or the period is closed, append-only.

The framework skill `flag_anomaly` is called **as-is** (read-only); each endpoint
writes only its own store's row (the ledger / confirmations / statements /
reconciliations / closes files stay byte-identical) and the flag id is derived with
issue A's exact recipe (`derive_flag_id`, imported — never re-implemented), so the
ack→gate-B linkage holds end-to-end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.flag_anomaly import flag_anomaly

from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore, derive_flag_id
from bookkeeper_ui.api import create_app
from bookkeeper_ui.closes import CloseRecord, FileCloseStore
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore
from bookkeeper_ui.waivers import FileWaiverStore
from tests.conftest import make_stmt_line, make_txn

PERIOD = "2026-Q2"


# --- Harness + builders -----------------------------------------------------


@dataclass
class Harness:
    app: FastAPI
    config: BookkeeperConfig
    ledger_store: FileLedgerStore
    confirmation_store: FileConfirmationStore
    statement_store: FileStatementStore
    reconciliation_store: FileReconciliationStore
    close_store: FileCloseStore
    anomaly_review_store: FileAnomalyReviewStore
    waiver_store: FileWaiverStore
    tmp: Path
    anomaly_path: Path
    waiver_path: Path

    def other_files(self) -> list[Path]:
        """The files the two write endpoints must NEVER touch (byte-identity set).

        Everything but the endpoint's own append target — the ledger it flags, the
        confirmations, the statement, the reconciliations, and the signed closes.
        """
        return [
            self.tmp / name
            for name in (
                "ledger.jsonl",
                "confirmations.jsonl",
                "statements.jsonl",
                "reconciliations.jsonl",
                "closes.jsonl",
            )
        ]


def _config(examples_dir: Path, **overrides: object) -> BookkeeperConfig:
    """The shipped example config, optionally with `materiality_floor` overridden.

    The stale-flag test builds two floors over one ledger (the over-materiality
    reason — and so the derived flag id — embeds the floor); everything else uses the
    shipped example (floor 1000.00, regime HST).
    """
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    if "materiality_floor" in overrides:
        data["materiality_floor"] = overrides["materiality_floor"]
    return BookkeeperConfig.from_mapping(data)


def _harness(examples_dir: Path, tmp_path: Path, config: BookkeeperConfig | None = None) -> Harness:
    config = config or _config(examples_dir)
    anomaly_path = tmp_path / "anomaly_reviews.jsonl"
    waiver_path = tmp_path / "reconciliation_waivers.jsonl"
    ledger_store = FileLedgerStore(tmp_path / "ledger.jsonl")
    confirmation_store = FileConfirmationStore(tmp_path / "confirmations.jsonl")
    statement_store = FileStatementStore(tmp_path / "statements.jsonl")
    reconciliation_store = FileReconciliationStore(tmp_path / "reconciliations.jsonl")
    close_store = FileCloseStore(tmp_path / "closes.jsonl")
    anomaly_review_store = FileAnomalyReviewStore(anomaly_path)
    waiver_store = FileWaiverStore(waiver_path)
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
        app, config, ledger_store, confirmation_store, statement_store,
        reconciliation_store, close_store, anomaly_review_store, waiver_store,
        tmp_path, anomaly_path, waiver_path,
    )


@pytest.fixture
def harness(examples_dir, tmp_path) -> Harness:
    return _harness(examples_dir, tmp_path)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _delta_1200() -> object:
    """A Delta charge over the example floor 1000.00 → exactly one over_materiality flag."""
    return make_txn(vendor="Delta Airlines", amount="1200.00", tax="0", date=datetime(2026, 5, 2), description="Flight")


async def _current_flag_id(h: Harness, period: str = PERIOD) -> str:
    """The app-derived id of the period's first current anomaly flag (fixtures raise one)."""
    report = await flag_anomaly(h.ledger_store, h.config, period)
    assert report.flags, "fixture expected at least one anomaly flag"
    return derive_flag_id(report.flags[0])


async def _seed_close(store: FileCloseStore, period: str) -> None:
    """Record a minimal signed close so `period` reads closed (the write-guard truth)."""
    await store.record(
        CloseRecord(
            period=period, signed_at=datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc), signed_by="human",
            checklist=[], transactions=[], tax={}, reconciliation={}, anomalies=[],
            effective_prior_period_state=None, config_prior_period_state=None,
        )
    )


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    """The byte content of each path (or `None` if absent) — for a before/after diff."""
    return {p: (p.read_bytes() if p.exists() else None) for p in paths}


# ============================================================================
# POST /anomalies/review  (AC2 · AC3 · AC5 · AC6)
# ============================================================================


async def test_ack_current_flag_appends_one_row_and_echoes(harness: Harness):
    """An ack for a current flag appends exactly one self-describing row and returns it."""
    await harness.ledger_store.store(_delta_1200())
    flag_id = await _current_flag_id(harness)
    stored = (await harness.ledger_store.fetch_for_period(PERIOD))[0]

    async with _client(harness.app) as client:
        resp = await client.post(
            "/anomalies/review",
            json={"flag_id": flag_id, "period": PERIOD, "note": "reviewed the flight"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["flag_id"] == flag_id
    assert body["kind"] == "over_materiality"
    assert "materiality floor" in body["reason"]  # the flag's reason, snapshotted verbatim
    assert body["transaction_ids"] == [transaction_key(stored)]  # the flag's member(s)
    assert body["note"] == "reviewed the flight"
    assert body["source"] == "human"
    assert body["acknowledged_at"]  # an ISO 8601 audit timestamp

    rows = await harness.anomaly_review_store.all()
    assert len(rows) == 1 and rows[0].flag_id == flag_id


async def test_ack_unknown_flag_id_is_422_and_writes_nothing(harness: Harness):
    """A `flag_id` matching no current flag is a 422 — never a fabricated ack."""
    await harness.ledger_store.store(_delta_1200())
    async with _client(harness.app) as client:
        resp = await client.post(
            "/anomalies/review", json={"flag_id": "not-a-real-flag-id", "period": PERIOD}
        )
    assert resp.status_code == 422
    assert "no current anomaly flag" in resp.json()["detail"]
    assert not harness.anomaly_path.exists()  # nothing dangled against a non-existent flag


async def test_ack_on_closed_period_is_409(harness: Harness):
    """An ack on a closed period is a 409 — a signed close freezes its dispositions."""
    await harness.ledger_store.store(_delta_1200())
    flag_id = await _current_flag_id(harness)
    await _seed_close(harness.close_store, PERIOD)
    async with _client(harness.app) as client:
        resp = await client.post("/anomalies/review", json={"flag_id": flag_id, "period": PERIOD})
    assert resp.status_code == 409
    assert "closed" in resp.json()["detail"]
    assert not harness.anomaly_path.exists()


async def test_closed_guard_precedes_flag_check(harness: Harness):
    """On a closed period even a bogus `flag_id` is a 409, not a 422 — the period-level
    write guard is checked before the current-flag existence check."""
    await _seed_close(harness.close_store, PERIOD)
    async with _client(harness.app) as client:
        resp = await client.post("/anomalies/review", json={"flag_id": "bogus", "period": PERIOD})
    assert resp.status_code == 409
    assert not harness.anomaly_path.exists()


async def test_second_ack_of_same_flag_is_a_new_row(harness: Harness):
    """A second ack of the same flag is a new append-only row (never an overwrite);
    `by_flag_id` collapses the trail to the latest disposition."""
    await harness.ledger_store.store(_delta_1200())
    flag_id = await _current_flag_id(harness)
    async with _client(harness.app) as client:
        r1 = await client.post(
            "/anomalies/review", json={"flag_id": flag_id, "period": PERIOD, "note": "first look"}
        )
        after_first = harness.anomaly_path.read_bytes()
        r2 = await client.post(
            "/anomalies/review", json={"flag_id": flag_id, "period": PERIOD, "note": "second look"}
        )
    assert r1.status_code == 200 and r2.status_code == 200
    after_second = harness.anomaly_path.read_bytes()
    # Append-only: the earlier bytes are untouched and exactly one line is added.
    assert after_second.startswith(after_first)
    assert len(after_second) > len(after_first)

    rows = await harness.anomaly_review_store.all()
    assert len(rows) == 2  # both kept for audit
    latest = await harness.anomaly_review_store.by_flag_id()
    assert latest[flag_id].note == "second look"  # last write wins


async def test_stale_over_materiality_flag_id_is_not_current_422(examples_dir, tmp_path):
    """A `flag_id` derived from a stale flag (its over-materiality reason changed after a
    `materiality_floor` change) is not current → 422; the current id acknowledges fine."""
    h = _harness(examples_dir, tmp_path, config=_config(examples_dir, materiality_floor="500.00"))
    await h.ledger_store.store(_delta_1200())

    # The stale id: the same Delta flag as it read under the shipped floor 1000.00.
    stale_report = await flag_anomaly(h.ledger_store, _config(examples_dir, materiality_floor="1000.00"), PERIOD)
    stale_id = derive_flag_id(stale_report.flags[0])
    current_id = await _current_flag_id(h)  # under the live floor 500.00
    assert stale_id != current_id  # the reason (and so the id) moved with the floor

    async with _client(h.app) as client:
        stale = await client.post("/anomalies/review", json={"flag_id": stale_id, "period": PERIOD})
        assert stale.status_code == 422  # a stale acknowledgment is never inherited
        current = await client.post("/anomalies/review", json={"flag_id": current_id, "period": PERIOD})
        assert current.status_code == 200

    rows = await h.anomaly_review_store.all()
    assert [r.flag_id for r in rows] == [current_id]  # only the current ack landed


async def test_ack_flips_gate_b_via_close_review(harness: Harness):
    """The id `GET /close` surfaces, posted back, lands on gate B: acknowledged flips
    True and `anomalies_reviewed` becomes met (the derived-id linkage, end to end)."""
    await harness.ledger_store.store(_delta_1200())
    async with _client(harness.app) as client:
        before = (await client.get("/close", params={"period": PERIOD})).json()
        anomaly = next(a for a in before["anomalies"] if a["kind"] == "over_materiality")
        assert anomaly["acknowledged"] is False
        assert before["app_gates"]["anomalies_reviewed"] == {"met": False, "unacknowledged_count": 1}

        ack = await client.post("/anomalies/review", json={"flag_id": anomaly["id"], "period": PERIOD})
        assert ack.status_code == 200

        after = (await client.get("/close", params={"period": PERIOD})).json()
    anomaly_after = next(a for a in after["anomalies"] if a["kind"] == "over_materiality")
    assert anomaly_after["acknowledged"] is True
    assert after["app_gates"]["anomalies_reviewed"] == {"met": True, "unacknowledged_count": 0}


async def test_ack_writes_only_its_own_store(harness: Harness):
    """A successful ack leaves the ledger / confirmations / statements / reconciliations /
    closes files byte-identical — only anomaly_reviews.jsonl is written."""
    await harness.ledger_store.store(_delta_1200())
    account = next(iter(harness.config.chart_of_accounts))
    stored = (await harness.ledger_store.fetch_for_period(PERIOD))[0]

    async with _client(harness.app) as client:
        # A confirmation so confirmations.jsonl exists in the snapshot set too.
        resolved = await client.post(
            "/resolve", json={"transaction_id": transaction_key(stored), "account": account}
        )
        assert resolved.status_code == 200

        flag_id = await _current_flag_id(harness)
        before = _snapshot(harness.other_files())
        resp = await client.post("/anomalies/review", json={"flag_id": flag_id, "period": PERIOD})
        assert resp.status_code == 200

    assert _snapshot(harness.other_files()) == before  # nothing else touched
    assert harness.anomaly_path.exists()  # only its own store's row was written


# ============================================================================
# POST /reconciliation/waive  (AC4 · AC5 · AC6)
# ============================================================================


async def test_waive_no_statement_period_appends_and_defaults_owner(harness: Harness):
    """A waiver on a no-statement period appends one row and returns it; `waived_by`
    defaults to "owner"."""
    async with _client(harness.app) as client:
        resp = await client.post(
            "/reconciliation/waive", json={"period": PERIOD, "note": "no bank feed this quarter"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == PERIOD
    assert body["waived_by"] == "owner"  # single-user default, a label not an identity
    assert body["note"] == "no bank feed this quarter"
    assert body["waived_at"]

    rows = await harness.waiver_store.all()
    assert len(rows) == 1 and rows[0].period == PERIOD


async def test_waive_uses_provided_waived_by(harness: Harness):
    """A supplied `waived_by` is recorded verbatim (the default only fills a null)."""
    async with _client(harness.app) as client:
        resp = await client.post("/reconciliation/waive", json={"period": PERIOD, "waived_by": "Stu"})
    assert resp.status_code == 200
    assert resp.json()["waived_by"] == "Stu"


async def test_waive_with_statement_present_is_409(harness: Harness):
    """A present statement is never waivable → 409; nothing is written."""
    await harness.statement_store.store(
        make_stmt_line(statement_ref="S-1", amount="10.00", date=datetime(2026, 5, 2))
    )
    async with _client(harness.app) as client:
        resp = await client.post("/reconciliation/waive", json={"period": PERIOD})
    assert resp.status_code == 409
    assert "statement on file" in resp.json()["detail"]
    assert not harness.waiver_path.exists()


async def test_waive_on_closed_period_is_409(harness: Harness):
    """A waiver on a closed period is a 409 — a signed close is write-guarded."""
    await _seed_close(harness.close_store, PERIOD)
    async with _client(harness.app) as client:
        resp = await client.post("/reconciliation/waive", json={"period": PERIOD})
    assert resp.status_code == 409
    assert "closed" in resp.json()["detail"]
    assert not harness.waiver_path.exists()


async def test_closed_guard_precedes_statement_check_on_waive(harness: Harness):
    """A closed period with a statement on file is a 409 for *closed*, not for the
    statement — the period-level write guard is checked first (mirrors the docstring
    and the anomaly endpoint's ordering)."""
    await harness.statement_store.store(
        make_stmt_line(statement_ref="S-1", amount="10.00", date=datetime(2026, 5, 2))
    )
    await _seed_close(harness.close_store, PERIOD)
    async with _client(harness.app) as client:
        resp = await client.post("/reconciliation/waive", json={"period": PERIOD})
    assert resp.status_code == 409
    assert "closed" in resp.json()["detail"]  # the closed guard won, not "statement on file"
    assert not harness.waiver_path.exists()


async def test_second_waive_is_a_new_row(harness: Harness):
    """A second waiver of the same period is a new append-only row; `by_period` collapses
    the trail to the latest."""
    async with _client(harness.app) as client:
        r1 = await client.post("/reconciliation/waive", json={"period": PERIOD, "note": "first"})
        after_first = harness.waiver_path.read_bytes()
        r2 = await client.post("/reconciliation/waive", json={"period": PERIOD, "note": "second"})
    assert r1.status_code == 200 and r2.status_code == 200
    after_second = harness.waiver_path.read_bytes()
    assert after_second.startswith(after_first)  # append-only
    assert len(after_second) > len(after_first)

    rows = await harness.waiver_store.all()
    assert len(rows) == 2
    latest = await harness.waiver_store.by_period()
    assert latest[PERIOD].note == "second"  # last write wins


async def test_waive_writes_only_its_own_store(harness: Harness):
    """A successful waiver leaves every other store file byte-identical — only
    reconciliation_waivers.jsonl is written."""
    await harness.ledger_store.store(_delta_1200())  # a populated ledger to snapshot
    async with _client(harness.app) as client:
        before = _snapshot(harness.other_files())
        resp = await client.post("/reconciliation/waive", json={"period": PERIOD})
        assert resp.status_code == 200

    assert _snapshot(harness.other_files()) == before
    assert harness.waiver_path.exists()


# ============================================================================
# Wiring: the endpoints require their Slice-3 store (never a silent no-op)
# ============================================================================


async def test_write_endpoints_503_when_stores_unwired(examples_dir, tmp_path):
    """A Slice-1/2 app (no anomaly/waiver stores wired) refuses both writes with 503,
    rather than crashing or silently dropping the write."""
    app = create_app(
        config=_config(examples_dir),
        ledger_store=FileLedgerStore(tmp_path / "ledger.jsonl"),
        confirmation_store=FileConfirmationStore(tmp_path / "confirmations.jsonl"),
        statement_store=FileStatementStore(tmp_path / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(tmp_path / "reconciliations.jsonl"),
    )  # no close / anomaly / waiver stores
    async with _client(app) as client:
        ack = await client.post("/anomalies/review", json={"flag_id": "x", "period": PERIOD})
        waive = await client.post("/reconciliation/waive", json={"period": PERIOD})
    assert ack.status_code == 503
    assert waive.status_code == 503
