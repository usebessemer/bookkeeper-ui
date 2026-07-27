"""Shared test fixtures and builders.

`make_txn` / `make_stmt_line` build framework models directly (the app must not
depend on the framework's private test fakes, which live in the agent-classes
repo). The `examples_dir` fixture points at the committed runnable dataset.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

from bookkeeper.model import StatementLine, Transaction

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def make_txn(
    *,
    attribution_target_id: str = "target-001",
    vendor: str = "Acme Supplies",
    amount: str = "45.99",
    tax: str = "3.50",
    date: datetime | None = None,
    description: str = "",
    artifact_bytes: bytes = b"",
) -> Transaction:
    """Build a `Transaction` for tests; money passed as strings → exact `Decimal`."""
    return Transaction(
        attribution_target_id=attribution_target_id,
        vendor=vendor,
        amount=Decimal(amount),
        tax=Decimal(tax),
        date=date or datetime(2026, 5, 15, 10, 0, 0),
        description=description,
        artifact_bytes=artifact_bytes,
    )


def make_stmt_line(
    *,
    statement_ref: str = "STMT-0001",
    date: datetime | None = None,
    amount: str = "45.99",
    description: str = "",
) -> StatementLine:
    """Build a `StatementLine` for tests; amount passed as a string → exact `Decimal`."""
    return StatementLine(
        statement_ref=statement_ref,
        date=date or datetime(2026, 5, 15, 10, 0, 0),
        amount=Decimal(amount),
        description=description,
    )


@pytest.fixture
def examples_dir() -> Path:
    return EXAMPLES_DIR


@pytest_asyncio.fixture(autouse=True)
async def _settle_background_tasks():
    """Drain any still-pending task on the test's event loop once the body returns.

    A banked write schedules its push **off the request path** (`BackupService.bank` →
    `_schedule_push` → a background task that spawns a `git` subprocess). Tests that hold
    the service drain it explicitly (the local `_settle` helpers); ones driven only through
    `create_app` / `build_app_from_env` — e.g. the `build_app_from_env` export tests — can't
    reach the service buried in the handler closure, so a banked export/sign leaves that push
    task pending. With pytest-asyncio's per-test event loops, a task still holding a subprocess
    transport when its loop is torn down **hangs on Linux** (the CI stall) even though macOS
    reaps it cleanly. Awaiting the stragglers here — never *cancelling*, which would orphan the
    live subprocess — makes every test self-contained regardless of whether it remembered to
    settle. Bounded, so a genuinely stuck task fails fast under the per-test `timeout` instead
    of blocking teardown; best-effort, so this never fails a test on its own.
    """
    yield
    try:
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)
    except Exception:  # teardown hygiene must never turn a green test red.
        pass
