"""Shared test fixtures and builders.

`make_txn` builds a framework `Transaction` directly (the app must not depend on
the framework's private test fakes, which live in the agent-classes repo). The
`examples_dir` fixture points at the committed runnable dataset.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from bookkeeper.model import Transaction

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


@pytest.fixture
def examples_dir() -> Path:
    return EXAMPLES_DIR
