"""The waiver store — the per-period reconciliation-waiver layer.

Slice 3's close-review gate lets a human **waive** the reconciliation
precondition for a period: a recorded, dated, attributable decision to sign the
close despite an open reconciliation, kept separate from the reconcile
resolutions (a waiver is a period-level disposition, not an item-level one).

A waiver is **append-only** — a re-waiver of the same period is a new row, not an
overwrite; `by_period()` collapses the trail to the latest waiver per period,
while `all()` keeps every row for audit. A period counts as **waived** iff a
waiver row exists for it.

Storage format: JSONL, one waiver per line; `waived_at` as ISO 8601.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The `waived_by` recorded for a waiver made through the app — the same named
# discipline as the other stores: a waiver is a **human** decision.
from bookkeeper_ui.confirmations import SOURCE_HUMAN


@dataclass(frozen=True)
class Waiver:
    """One human waiver of a period's reconciliation precondition.

    `period` is the period waived; `waived_at` / `waived_by` are the audit (when,
    who); `note` is the human's optional why.
    """

    period: str
    waived_at: datetime
    waived_by: str
    note: str | None


def _to_record(waiver: Waiver) -> dict[str, object]:
    return {
        "period": waiver.period,
        "waived_at": waiver.waived_at.isoformat(),
        "waived_by": waiver.waived_by,
        "note": waiver.note,
    }


def _from_record(record: dict[str, object]) -> Waiver:
    raw_note = record.get("note")
    return Waiver(
        period=str(record["period"]),
        waived_at=datetime.fromisoformat(str(record["waived_at"])),
        waived_by=str(record["waived_by"]),
        note=None if raw_note is None else str(raw_note),
    )


class FileWaiverStore:
    """A JSONL-backed, append-only store of per-period reconciliation waivers.

    Construct with the path to the waivers file (created on first write, parents
    included). A distinct file from the reconciliations — a waiver is a
    period-level disposition, kept apart from the item-level resolutions.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    async def record(self, waiver: Waiver) -> None:
        """Append one waiver to the trail (append-only; a re-waiver is a new row)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_record(waiver)) + "\n")

    async def all(self) -> list[Waiver]:
        """Every recorded waiver, in insertion order — the full trail."""
        results: list[Waiver] = []
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                results.append(_from_record(json.loads(line)))
        return results

    async def by_period(self) -> dict[str, Waiver]:
        """The latest waiver per period (last write wins).

        Collapses the append-only trail by `period`: a period is waived iff it
        appears here, and a re-waiver replaces the earlier one while `all()` keeps
        both for audit.
        """
        latest: dict[str, Waiver] = {}
        for waiver in await self.all():
            latest[waiver.period] = waiver
        return latest


__all__ = [
    "SOURCE_HUMAN",
    "Waiver",
    "FileWaiverStore",
]
