"""The anomaly-review store: append-only acks, and the load-bearing flag-id derivation.

Pins issue-A's `FileAnomalyReviewStore` / `AnomalyReview` / `derive_flag_id`: a
review persists and reads back, the trail is append-only (a re-ack is a new row,
`by_flag_id` collapses it), and — the load-bearing detail — the derived flag id is
deterministic and *content-addressed*, so a changed flag (reason or member set)
derives a new, unacknowledged id. The real-skill test pins the config-drift
consequence: an `over_materiality` id moves when the floor changes (its reason
embeds the floor), while a `duplicate` id is floor-invariant.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from bookkeeper.skills.flag_anomaly import AnomalyFlag, AnomalyKind, flag_anomaly

from bookkeeper_ui.anomaly_reviews import (
    SOURCE_HUMAN,
    AnomalyReview,
    FileAnomalyReviewStore,
    derive_flag_id,
)
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from tests.conftest import make_txn


def _review(flag_id: str = "f1", **overrides: object) -> AnomalyReview:
    fields: dict[str, object] = {
        "flag_id": flag_id,
        "kind": "duplicate",
        "reason": "likely double capture",
        "transaction_ids": ("a", "b"),
        "note": "seen it",
        "acknowledged_at": datetime(2026, 7, 11, 9, 0, 0),
        "source": SOURCE_HUMAN,
    }
    fields.update(overrides)
    return AnomalyReview(**fields)  # type: ignore[arg-type]


# --- the store ---------------------------------------------------------------


async def test_records_and_reads_back(tmp_path):
    store = FileAnomalyReviewStore(tmp_path / "anomaly_reviews.jsonl")
    review = _review()
    await store.record(review)

    (read_back,) = await store.all()
    assert read_back == review


async def test_reack_is_new_row_and_by_flag_id_collapses(tmp_path):
    """AC2: a re-ack of the same flag is a new row; `by_flag_id` is last-write-wins."""
    store = FileAnomalyReviewStore(tmp_path / "anomaly_reviews.jsonl")
    first = _review(note="first look")
    second = _review(note="looked again")
    await store.record(first)
    await store.record(second)

    assert await store.all() == [first, second]  # append-only, both retained
    assert (await store.by_flag_id())["f1"] == second  # collapsed to latest


async def test_note_none_round_trips(tmp_path):
    store = FileAnomalyReviewStore(tmp_path / "anomaly_reviews.jsonl")
    await store.record(_review(note=None))

    (read_back,) = await store.all()
    assert read_back.note is None


async def test_persists_across_instances(tmp_path):
    path = tmp_path / "anomaly_reviews.jsonl"
    await FileAnomalyReviewStore(path).record(_review())
    assert "f1" in await FileAnomalyReviewStore(path).by_flag_id()


async def test_empty_store_reads_empty(tmp_path):
    store = FileAnomalyReviewStore(tmp_path / "anomaly_reviews.jsonl")
    assert await store.all() == []
    assert await store.by_flag_id() == {}


# --- derive_flag_id (the load-bearing detail) --------------------------------


def test_flag_id_is_deterministic_and_member_order_invariant():
    """AC3: the same flag derives the same id; member order never changes it."""
    a, b = make_txn(vendor="DupCo", description="one"), make_txn(vendor="DupCo", description="two")
    flag_ab = AnomalyFlag(AnomalyKind.DUPLICATE, "same charge twice", (a, b))
    flag_ba = AnomalyFlag(AnomalyKind.DUPLICATE, "same charge twice", (b, a))

    assert derive_flag_id(flag_ab) == derive_flag_id(flag_ab)  # deterministic
    assert derive_flag_id(flag_ab) == derive_flag_id(flag_ba)  # order-invariant


def test_flag_id_changes_with_reason_members_or_kind():
    """AC3: a changed reason, member set, or kind derives a different id."""
    a, b = make_txn(vendor="DupCo", description="one"), make_txn(vendor="DupCo", description="two")
    base = AnomalyFlag(AnomalyKind.DUPLICATE, "r", (a, b))

    assert derive_flag_id(base) != derive_flag_id(
        AnomalyFlag(AnomalyKind.DUPLICATE, "reworded", (a, b))  # changed reason
    )
    assert derive_flag_id(base) != derive_flag_id(
        AnomalyFlag(AnomalyKind.DUPLICATE, "r", (a,))  # changed member set
    )
    assert derive_flag_id(base) != derive_flag_id(
        AnomalyFlag(AnomalyKind.MALFORMED, "r", (a, b))  # changed kind
    )


async def test_id_moves_with_floor_for_over_materiality_but_not_duplicate(tmp_path, examples_dir):
    """AC3 (the config-drift consequence, over the real skill).

    An `over_materiality` flag's reason embeds the floor value, so its derived id
    moves when `materiality_floor` changes — config drift derives a *new,
    unacknowledged* id, never silently inheriting a stale ack. A `duplicate` flag's
    reason is floor-invariant, so its id is stable across the same floor change.
    """
    store = FileLedgerStore(tmp_path / "ledger.jsonl")
    # A big item (over either floor) + a duplicate pair (same vendor/amount/day,
    # distinct keys so both persist).
    await store.store(make_txn(vendor="BigCo", amount="5000.00", date=datetime(2026, 5, 1)))
    await store.store(make_txn(vendor="DupCo", amount="10.00", date=datetime(2026, 5, 2), description="a"))
    await store.store(make_txn(vendor="DupCo", amount="10.00", date=datetime(2026, 5, 2), description="b"))

    config = load_config(examples_dir / "config.json")  # materiality_floor 1000.00
    config_higher = replace(config, materiality_floor=Decimal("2000.00"))

    def ids(report):
        return {
            flag.kind: derive_flag_id(flag)
            for flag in report.flags
            if flag.kind in (AnomalyKind.OVER_MATERIALITY, AnomalyKind.DUPLICATE)
        }

    low = ids(await flag_anomaly(store, config, "2026-Q2"))
    high = ids(await flag_anomaly(store, config_higher, "2026-Q2"))

    assert low[AnomalyKind.OVER_MATERIALITY] != high[AnomalyKind.OVER_MATERIALITY]
    assert low[AnomalyKind.DUPLICATE] == high[AnomalyKind.DUPLICATE]


def test_flag_id_uses_transaction_key_and_kind_value():
    """The canonical string is `kind.value | sorted transaction_keys | reason`."""
    import hashlib

    t = make_txn(vendor="Acme")
    flag = AnomalyFlag(AnomalyKind.OVER_MATERIALITY, "big", (t,))
    expected = hashlib.sha256(
        f"over_materiality|{transaction_key(t)}|big".encode("utf-8")
    ).hexdigest()
    assert derive_flag_id(flag) == expected
