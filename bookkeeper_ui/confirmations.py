"""The confirmation store — the human confirm/correct resolution layer.

Kept **deliberately separate** from the ledger (`FileLedgerStore`): the ledger
holds the imported transactions as captured; this store holds the human
*resolutions* layered on top (charter: `categorize` never auto-assigns — the
human confirm/correct step is the point of the app). Slice 1 issue #3 renders
these; issue #2 writes them from the API. The two stores link on one key: a
`Confirmation.transaction_id` is the ledger's `transaction_key(transaction)`.

A resolution is **append-only** — every confirm/correct decision is recorded in
order as an audit trail (charter §1: traceable). A *correction* of an earlier
decision is a new row, not an overwrite; `latest_by_transaction()` collapses the
trail to the current decision per transaction (last write wins).

Storage format: JSONL, one decision per line; `decided_at` as ISO 8601.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The `source` recorded for a decision made through the app's confirm/correct
# loop. A distinct, named constant because the whole point of this store is that
# these accounts came from a **human**, not the agent's proposal.
SOURCE_HUMAN = "human"


@dataclass(frozen=True)
class Confirmation:
    """One human confirm/correct decision on a transaction's category.

    `transaction_id` links to the ledger transaction it resolves (its
    `transaction_key`). `account` is the chosen chart account — the agent's
    proposed account on a *confirm*, a different one on a *correct*. `source`
    records who decided (`human` for the confirm/correct loop). `decided_at` is
    when — the audit timestamp.
    """

    transaction_id: str
    account: str
    source: str
    decided_at: datetime


def _to_record(confirmation: Confirmation) -> dict[str, str]:
    return {
        "transaction_id": confirmation.transaction_id,
        "account": confirmation.account,
        "source": confirmation.source,
        "decided_at": confirmation.decided_at.isoformat(),
    }


def _from_record(record: dict[str, object]) -> Confirmation:
    return Confirmation(
        transaction_id=str(record["transaction_id"]),
        account=str(record["account"]),
        source=str(record["source"]),
        decided_at=datetime.fromisoformat(str(record["decided_at"])),
    )


class FileConfirmationStore:
    """A JSONL-backed, append-only store of confirm/correct decisions.

    Construct with the path to the confirmations file (created on first write,
    parents included). Distinct file from the ledger — the resolution layer is
    kept separate from the raw ledger it resolves.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    async def record(self, confirmation: Confirmation) -> None:
        """Append a confirm/correct decision to the audit trail."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_record(confirmation)) + "\n")

    async def all(self) -> list[Confirmation]:
        """Every recorded decision, in decision (insertion) order — the full trail."""
        results: list[Confirmation] = []
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                results.append(_from_record(json.loads(line)))
        return results

    async def latest_by_transaction(self) -> dict[str, Confirmation]:
        """The current decision per transaction id (last write wins).

        Collapses the append-only trail: a correction recorded after an earlier
        decision replaces it here, while `all()` still holds both for audit.
        """
        latest: dict[str, Confirmation] = {}
        for confirmation in await self.all():
            latest[confirmation.transaction_id] = confirmation
        return latest
