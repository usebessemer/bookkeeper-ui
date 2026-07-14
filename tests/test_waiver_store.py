"""The waiver store: per-period reconciliation waivers, append-only, latest-per-period."""

from __future__ import annotations

from datetime import datetime

from bookkeeper_ui.waivers import SOURCE_HUMAN, FileWaiverStore, Waiver


def _waiver(period: str = "2026-Q2", **overrides: object) -> Waiver:
    fields: dict[str, object] = {
        "period": period,
        "waived_at": datetime(2026, 7, 11, 9, 0, 0),
        "waived_by": SOURCE_HUMAN,
        "note": "recon still open, signing anyway",
    }
    fields.update(overrides)
    return Waiver(**fields)  # type: ignore[arg-type]


async def test_records_and_reads_back(tmp_path):
    store = FileWaiverStore(tmp_path / "reconciliation_waivers.jsonl")
    waiver = _waiver()
    await store.record(waiver)

    (read_back,) = await store.all()
    assert read_back == waiver


async def test_rewaiver_is_new_row_and_by_period_collapses(tmp_path):
    """A re-waiver of the same period is a new row; `by_period` is last-write-wins."""
    store = FileWaiverStore(tmp_path / "reconciliation_waivers.jsonl")
    first = _waiver(note="first")
    second = _waiver(note="second")
    await store.record(first)
    await store.record(second)

    assert await store.all() == [first, second]  # append-only
    assert (await store.by_period())["2026-Q2"] == second  # latest per period


async def test_note_none_round_trips(tmp_path):
    store = FileWaiverStore(tmp_path / "reconciliation_waivers.jsonl")
    await store.record(_waiver(note=None))

    (read_back,) = await store.all()
    assert read_back.note is None


async def test_by_period_keys_each_period(tmp_path):
    store = FileWaiverStore(tmp_path / "reconciliation_waivers.jsonl")
    await store.record(_waiver("2026-Q1", waived_at=datetime(2026, 4, 1)))
    await store.record(_waiver("2026-Q2"))

    by_period = await store.by_period()
    assert set(by_period) == {"2026-Q1", "2026-Q2"}


async def test_persists_across_instances(tmp_path):
    path = tmp_path / "reconciliation_waivers.jsonl"
    await FileWaiverStore(path).record(_waiver())
    assert "2026-Q2" in await FileWaiverStore(path).by_period()


async def test_empty_store_reads_empty(tmp_path):
    store = FileWaiverStore(tmp_path / "reconciliation_waivers.jsonl")
    assert await store.all() == []
    assert await store.by_period() == {}
