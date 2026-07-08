"""The local, file-based statement store — the reconcile read side.

Implements the framework's `StatementSource` over its own JSONL file
(`statements.jsonl`, beside `ledger.jsonl` in the data dir). It is the reconcile
counterpart to `ledger_store.py`: `LedgerSource` reads what the books
*captured*, `StatementSource` reads what the bank / card issuer *says happened*,
and (in issue B) `reconcile_account` matches the two.

- `fetch_statement(period)` — the read side (the `StatementSource` contract).
  Returns the period's stored statement lines in **deterministic order**
  (insertion / file order), defensively deduped on key.
- `store(line)` — the app-internal write side the importer persists through.
  **Idempotent on a stable natural key**: re-storing an already-filed line is a
  no-op, never a duplicate row. There is deliberately **no** framework statement
  *writer* port (reconcile mutates nothing, §5.5), so `store` implements no port —
  it is only how this app lands the file the user uploads.

Storage format: one JSON object per line (JSONL). Money is serialized as a
**string** so the exact `Decimal` round-trips (never a lossy float); `date` as
ISO 8601. Each row also records the derived `period` (so the read side filters
without re-deriving) and the stable `key` (so the write side can dedupe) —
mirrors `ledger_store.py` exactly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from bookkeeper.model import StatementLine
from bookkeeper.ports import StatementSource

from bookkeeper_ui.periods import period_of


def _money(value: Decimal) -> str:
    """Serialize exact currency as a string (a float would be lossy)."""
    return str(value)


def statement_line_key(line: StatementLine) -> str:
    """The stable dedupe key for a statement line — its natural key.

    A deterministic SHA-256 over the line's identifying fields (`statement_ref`,
    date, exact amount, description) — the analog of
    `ledger_store.transaction_key`. Derived only from what it persists, so the
    *same* logical line always maps to the *same* key regardless of the format
    it was imported from (`.csv` then `.json` re-import is a no-op).

    Unlike the ledger key, `statement_ref` is included: a statement line carries
    the feed's own stable reference (§1 traceability), so two genuinely distinct
    charges that happen to share a vendor/amount/date still key apart on their
    distinct refs — no silent under-count.
    """
    canonical = "|".join(
        (
            line.statement_ref,
            line.date.isoformat(),
            _money(line.amount),
            line.description,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_record(line: StatementLine, key: str) -> dict[str, object]:
    """Flatten a `StatementLine` to its JSONL row (exact money as a string, ISO date)."""
    return {
        "key": key,
        "period": period_of(line.date),
        "statement_ref": line.statement_ref,
        "date": line.date.isoformat(),
        "amount": _money(line.amount),
        "description": line.description,
    }


def _from_record(record: dict[str, object]) -> StatementLine:
    """Reconstruct a `StatementLine` from a JSONL row (exact `Decimal` from the string)."""
    return StatementLine(
        statement_ref=str(record["statement_ref"]),
        date=datetime.fromisoformat(str(record["date"])),
        amount=Decimal(str(record["amount"])),
        description=str(record.get("description", "")),
    )


class FileStatementStore(StatementSource):
    """A JSONL-backed statement store implementing `StatementSource`.

    One file, append-only writes, whole-file reads — deliberately simple and
    always-consistent-with-disk for the local, single-user slice, mirroring
    `FileLedgerStore`. Construct with the path to the statement file (created on
    first write, parents included).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        # Lazily-loaded set of stable keys already on disk, so a bulk import is
        # O(n) for its dedupe rather than re-reading the file per store call.
        self._keys: set[str] | None = None

    def _load_keys(self) -> set[str]:
        keys: set[str] = set()
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    keys.add(str(json.loads(line)["key"]))
        return keys

    async def store(self, line: StatementLine) -> None:
        """Persist a statement line; idempotent on its stable key (a re-store is a no-op)."""
        if self._keys is None:
            self._keys = self._load_keys()
        key = statement_line_key(line)
        if key in self._keys:
            return  # already filed — idempotent no-op
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_record(line, key)) + "\n")
        self._keys.add(key)

    async def fetch_statement(self, period: str) -> list[StatementLine]:
        """Return `period`'s stored statement lines in deterministic (insertion) order."""
        results: list[StatementLine] = []
        seen: set[str] = set()
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("period") != period:
                continue
            key = str(record["key"])
            if key in seen:  # defensive: a hand-edited file can't yield dupes
                continue
            seen.add(key)
            results.append(_from_record(record))
        return results
