"""The local, file-based ledger store — the framework's `booksLocation` adapter.

Implements **both** framework ports over one JSONL file:

- `LedgerSink.store(transaction)` — the write side. **Idempotent on a stable
  key** (the port's contract): re-storing an already-filed transaction is a
  no-op, never a duplicate row. The stable key here is a hash of the
  transaction's natural business fields (§1: derived deterministically from what
  it persists, so the *same* transaction always maps to the *same* key).
- `LedgerSource.fetch_for_period(period)` — the read side. Returns the period's
  stored transactions in **deterministic order** (insertion / file order).

This is the generic open-source "local store": the default `booksLocation` when
a business has no external system of record. One store instance implements both
ports against the same file, so the framework reads back exactly what it filed
(mirrors the framework's own `FakeLedger`).

Storage format: one JSON object per line (JSONL). Money is serialized as a
**string** so the exact `Decimal` round-trips (never a lossy float); `date` as
ISO 8601; `artifact_bytes` base64-encoded. Each row also records the derived
`period` (so the read side filters without re-deriving) and the stable `key`
(so the write side can dedupe).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from bookkeeper.model import Transaction
from bookkeeper.ports import LedgerSink, LedgerSource

from bookkeeper_ui.periods import period_of


def _money(value: Decimal) -> str:
    """Serialize exact currency as a string (a float would be lossy)."""
    return str(value)


def transaction_key(transaction: Transaction) -> str:
    """The stable dedupe key for a transaction — its natural business key.

    A deterministic SHA-256 over the transaction's identifying business fields
    (target, vendor, exact amount + tax, date, description). `artifact_bytes` is
    **excluded**: it is the traceable source blob, not part of the transaction's
    identity, and the read-path projection may drop it — so the same logical
    transaction keys the same whether or not the bytes are carried.

    This is also the id the confirmation store references (a resolution points
    at the transaction it resolves), so the two stores link on one key.
    """
    canonical = "|".join(
        (
            transaction.attribution_target_id,
            transaction.vendor,
            _money(transaction.amount),
            _money(transaction.tax),
            transaction.date.isoformat(),
            transaction.description,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_record(transaction: Transaction, key: str) -> dict[str, object]:
    """Flatten a `Transaction` to its JSONL row (exact money, ISO date, b64 bytes)."""
    return {
        "key": key,
        "period": period_of(transaction.date),
        "attribution_target_id": transaction.attribution_target_id,
        "vendor": transaction.vendor,
        "amount": _money(transaction.amount),
        "tax": _money(transaction.tax),
        "date": transaction.date.isoformat(),
        "description": transaction.description,
        "artifact_bytes": base64.b64encode(transaction.artifact_bytes).decode("ascii"),
    }


def _from_record(record: dict[str, object]) -> Transaction:
    """Reconstruct a `Transaction` from a JSONL row.

    Coalesces an absent / NULL `tax` to ``Decimal("0")`` at this boundary (the
    `LedgerSource` contract: the framework never holds None-money), and rebuilds
    exact `Decimal` money from the stored strings.
    """
    raw_tax = record.get("tax")
    tax = Decimal("0") if raw_tax in (None, "") else Decimal(str(raw_tax))
    raw_bytes = record.get("artifact_bytes") or ""

    return Transaction(
        attribution_target_id=str(record["attribution_target_id"]),
        vendor=str(record["vendor"]),
        amount=Decimal(str(record["amount"])),
        tax=tax,
        date=datetime.fromisoformat(str(record["date"])),
        description=str(record.get("description", "")),
        artifact_bytes=base64.b64decode(str(raw_bytes)),
    )


class FileLedgerStore(LedgerSink, LedgerSource):
    """A JSONL-backed ledger implementing `LedgerSink` + `LedgerSource`.

    One file, append-only writes, whole-file reads — deliberately simple and
    always-consistent-with-disk for the local, single-user slice. Construct with
    the path to the ledger file (created on first write, parents included).
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

    async def store(self, transaction: Transaction) -> None:
        """Persist a transaction; idempotent on its stable key (a re-store is a no-op)."""
        if self._keys is None:
            self._keys = self._load_keys()
        key = transaction_key(transaction)
        if key in self._keys:
            return  # already filed — idempotent no-op (LedgerSink contract)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_record(transaction, key)) + "\n")
        self._keys.add(key)

    async def contains(self, transaction_id: str) -> bool:
        """True if a stored transaction carries this stable key (its `transaction_key`).

        The confirm/correct write path checks this before recording a resolution,
        so a confirmation can never dangle against a transaction that was never
        imported (issue #21 / N1: strict 404, typo-safe). Reuses the same on-disk
        key set the idempotent `store` already relies on — no period is needed, a
        resolution is keyed on the transaction alone.
        """
        if self._keys is None:
            self._keys = self._load_keys()
        return transaction_id in self._keys

    async def fetch_for_period(self, period: str) -> list[Transaction]:
        """Return `period`'s stored transactions in deterministic (insertion) order."""
        results: list[Transaction] = []
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
