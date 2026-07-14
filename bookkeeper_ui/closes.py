"""The close store — the durable, append-only record of a signed period close.

Slice 3's system-of-record for the §5.7 human sign-off. `close_period` (the
framework skill) is propose-only: it produces a `READY`/`BLOCKED` checklist and
never marks anything closed — the signed state is entirely **the app's** artifact
(there is no `CLOSED`/`SIGNED` in the framework). This store holds that artifact.

**Self-contained records (the #14 immutability lesson).** A close record embeds
everything needed to know *what was signed*, snapshotted at sign time — the
checklist, the per-transaction final state, and the tax / reconciliation / anomaly
snapshots — rather than a decision row that relies on re-derivation. A row that
re-derives is silently rewritten by later config drift (a reworded materiality
reason, a changed chart, a new prior-period state); a snapshot is not. The exact
snapshot *population* (which fields land in each payload) is finalized in issue D,
where `POST /sign` builds a `CloseRecord`; this module defines the store, the
record dataclass, and its (de)serialization so D plugs in.

A close is **append-only** — every sign is a new line, in order (charter §1:
traceable). A period can only be signed once (enforced at the sign handler in D),
but the store itself never rewrites or truncates: `by_period()` collapses the
trail (last write wins) and `latest()` is the last appended record.

Storage format: JSONL, one close per line; datetimes as ISO 8601; **money
everywhere as exact-`Decimal` strings, never a JSON number** — the snapshot
payloads carry money pre-stringified, and the serializer refuses a raw `float` on
any path (a lossy float can never reach `closes.jsonl`).

This module is also the one home for **closed-period truth** and the guard
helpers that read it: a period is closed iff it appears in `by_period()`, and the
write-path guards (`api.py` / `web.py`) probe closed truth through the helpers
below rather than re-deriving it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.statement_store import FileStatementStore, statement_line_key

# The `signed_by` / row `source` recorded for a close made through the app's sign
# action — the same named discipline the other stores use: a close is a **human**
# sign-off (§5.7: the agent assembles and proposes; the human signs).
SOURCE_HUMAN = "human"


def _jsonable(value: object) -> object:
    """Recursively coerce a snapshot payload to JSON-safe primitives.

    Money discipline, enforced at the store boundary regardless of what the sign
    handler (D) hands in: an exact `Decimal` becomes its exact string, and a raw
    `float` on **any** path is refused — money in a close record is exact-Decimal
    strings, never a lossy JSON number (the whole slice's money rule). Datetimes
    inside a payload serialize ISO 8601. Everything else passes through as its
    JSON-native shape.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        raise TypeError(
            "float in a close-record payload — money must be exact Decimal-as-string, "
            "never a lossy float (§ money discipline)."
        )
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


@dataclass(frozen=True)
class CloseRecord:
    """One durable, self-contained record of a signed period close (the D3 snapshot).

    `period` is the period signed; `signed_at` / `signed_by` are the §5.7 sign-off
    audit (who, when). The remaining fields are the **snapshot payload** — the
    period's state frozen at sign time so the record answers "what was signed"
    without re-derivation:

    - `checklist` — the `close_period` checklist (each precondition + verdict);
    - `transactions` — the per-transaction final state (confirmed account etc.);
    - `tax` — the `track_tax` summary snapshot;
    - `reconciliation` — the effective reconciliation snapshot;
    - `anomalies` — the period's anomaly flags + their review disposition;
    - `summary` — the disposition counts: the framework `PeriodSummary`
      (`processed`/`auto_filed`/`reviewed`/`open`, snapshotted as-is — its v1
      approximation and all) alongside the app-truth per-transaction disposition
      read from `build_ledger`, so both the framework tally and the app's are on
      the trail, neither fabricated (issue D populates it; defaults to `{}`);
    - `effective_prior_period_state` — the prior-period label the close was struck
      against (the D4 effective substitution), and `config_prior_period_state` —
      the config's raw `prior_period_state` at sign time, kept for the trail.

    The payload fields are held as JSON-native structures (money already
    exact-Decimal strings): this module round-trips them verbatim and leaves their
    exact shape to issue D. `checklist` / `transactions` / `anomalies` are frozen
    to tuples on construction so a record is an immutable snapshot.

    **D note (round-trip equality):** build the record with money **pre-stringified**
    and JSON-native **lists** (not tuples) inside the `tax` / `reconciliation`
    mappings — a raw `Decimal` or a nested tuple survives the JSONL round-trip as a
    string / list, so a freshly-built record would otherwise not compare equal to
    the one `by_period()` / `latest()` read back.
    """

    period: str
    signed_at: datetime
    signed_by: str
    checklist: tuple[Mapping[str, object], ...]
    transactions: tuple[Mapping[str, object], ...]
    tax: Mapping[str, object]
    reconciliation: Mapping[str, object]
    anomalies: tuple[Mapping[str, object], ...]
    effective_prior_period_state: str | None
    config_prior_period_state: str | None
    # The disposition counts (framework `PeriodSummary` + app-truth). Optional with
    # an empty default so the issue-A/B/C call sites that predate it keep working;
    # issue D's sign path populates it.
    summary: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Freeze the multi-item payloads to tuples, so a record is an immutable
        # snapshot and a read-back (which arrives as JSON lists) compares equal to
        # the constructed record.
        object.__setattr__(self, "checklist", tuple(self.checklist))
        object.__setattr__(self, "transactions", tuple(self.transactions))
        object.__setattr__(self, "anomalies", tuple(self.anomalies))


def _to_record(close: CloseRecord) -> dict[str, object]:
    """Flatten a `CloseRecord` to its JSONL row (ISO datetimes, money as strings).

    Every payload goes through `_jsonable`, so a stray `Decimal` is stringified and
    a raw `float` is refused before it can reach disk.
    """
    return {
        "period": close.period,
        "signed_at": close.signed_at.isoformat(),
        "signed_by": close.signed_by,
        "checklist": _jsonable(list(close.checklist)),
        "transactions": _jsonable(list(close.transactions)),
        "tax": _jsonable(close.tax),
        "reconciliation": _jsonable(close.reconciliation),
        "anomalies": _jsonable(list(close.anomalies)),
        "summary": _jsonable(close.summary),
        "effective_prior_period_state": close.effective_prior_period_state,
        "config_prior_period_state": close.config_prior_period_state,
    }


def _opt_str(value: object) -> str | None:
    """A stored label as a `str`, or `None` when the row carried a null.

    Distinct from `str(value)`: a close struck with no prior period on record
    stores `null` for its prior-state fields, and `str(None)` would corrupt that
    into the string ``"None"``.
    """
    return None if value is None else str(value)


def _from_record(record: dict[str, object]) -> CloseRecord:
    """Reconstruct a `CloseRecord` from a JSONL row (payloads verbatim, ISO datetime)."""
    return CloseRecord(
        period=str(record["period"]),
        signed_at=datetime.fromisoformat(str(record["signed_at"])),
        signed_by=str(record["signed_by"]),
        checklist=tuple(record.get("checklist") or ()),  # type: ignore[arg-type]
        transactions=tuple(record.get("transactions") or ()),  # type: ignore[arg-type]
        tax=dict(record.get("tax") or {}),  # type: ignore[arg-type]
        reconciliation=dict(record.get("reconciliation") or {}),  # type: ignore[arg-type]
        anomalies=tuple(record.get("anomalies") or ()),  # type: ignore[arg-type]
        summary=dict(record.get("summary") or {}),  # type: ignore[arg-type]
        effective_prior_period_state=_opt_str(record.get("effective_prior_period_state")),
        config_prior_period_state=_opt_str(record.get("config_prior_period_state")),
    )


class FileCloseStore:
    """A JSONL-backed, append-only store of signed period closes.

    Construct with the path to the closes file (created on first write, parents
    included). Mirrors the other stores' discipline — async methods, one JSON
    object per line, whole-file reads. A distinct file: the signed-close artifact
    is the app's own, kept apart from the ledger / statements / resolutions it
    snapshots.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    async def record(self, close: CloseRecord) -> None:
        """Append one signed close to the trail (never rewrites/truncates)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_record(close)) + "\n")

    async def all(self) -> list[CloseRecord]:
        """Every recorded close, in sign (insertion) order — the full trail."""
        results: list[CloseRecord] = []
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                results.append(_from_record(json.loads(line)))
        return results

    async def by_period(self) -> dict[str, CloseRecord]:
        """The current close per period (last write wins) — the closed-period truth.

        A period is **closed** iff it appears here. A period is signed once (the
        sign handler enforces it in D), but the store stays honest if the trail
        ever carries two: the later-appended close wins.
        """
        latest: dict[str, CloseRecord] = {}
        for close in await self.all():
            latest[close.period] = close
        return latest

    async def latest(self) -> CloseRecord | None:
        """The last-appended close, or `None` when nothing is signed yet.

        "Latest = last appended" holds by construction: every sign passed the
        framework's strictly-after prior-period guard, so the closes are appended
        in strictly-increasing period order. D's D4 effective-prior-state logic
        relies on this — so it never re-parses period labels here.
        """
        records = await self.all()
        return records[-1] if records else None


# --- Closed-period truth + the write-path guard helpers ---------------------
#
# One source of closed truth (`by_period()`), read through these helpers so every
# write path (`api.py` / `web.py`) probes the same mechanism rather than
# re-deriving it. All three tolerate an unwired (`None`) close store — treating it
# as "nothing is closed" — so the four shipped `create_app` call sites (which pass
# no close store) keep their exact pre-Slice-3 behaviour.


async def closed_periods(close_store: FileCloseStore | None) -> set[str]:
    """The set of periods with a signed close on record (empty if unwired)."""
    if close_store is None:
        return set()
    return set(await close_store.by_period())


def closed_import_refusal(offending: list[tuple[str, str]]) -> str:
    """The whole-upload refusal message for an import touching a closed period.

    `offending` is each refused row's human descriptor paired with its closed
    period. The upload is refused **whole** (nothing persisted), so the message
    names every offending row and its period — the human sees exactly what blocked
    it. Shared by the JSON 400 and the UI error partial so both read identically.
    """
    rows = "; ".join(f"{descriptor} → {period}" for descriptor, period in offending)
    return (
        f"{len(offending)} row(s) fall in a closed period and cannot be imported — "
        f"the whole upload is refused and nothing was saved: {rows}. "
        f"A closed period is write-guarded (§5.7: a signed close is durable)."
    )


async def transaction_in_closed_period(
    close_store: FileCloseStore | None,
    ledger_store: FileLedgerStore,
    transaction_id: str,
) -> str | None:
    """The closed period a transaction id lands in, or `None`.

    A resolve request carries no date — so closed truth is mechanized off the
    close store, not a date read: each closed period's ledger key set is built
    (via `transaction_key`) and probed for membership. Returns the first closed
    period the id belongs to (closed periods are few, so probing each is cheap), or
    `None` when the id is in no closed period (an unknown id included — its N1
    behaviour is left to the existing existence guard, untouched).
    """
    for period in sorted(await closed_periods(close_store)):
        keys = {transaction_key(t) for t in await ledger_store.fetch_for_period(period)}
        if transaction_id in keys:
            return period
    return None


async def statement_line_in_closed_period(
    close_store: FileCloseStore | None,
    statement_store: FileStatementStore,
    statement_line_id: str,
) -> str | None:
    """The closed period a statement line id lands in, or `None`.

    The statement-side twin of `transaction_in_closed_period`: each closed
    period's statement key set (via `statement_line_key`) is probed for membership.
    """
    for period in sorted(await closed_periods(close_store)):
        keys = {statement_line_key(s) for s in await statement_store.fetch_statement(period)}
        if statement_line_id in keys:
            return period
    return None


__all__ = [
    "SOURCE_HUMAN",
    "CloseRecord",
    "FileCloseStore",
    "closed_periods",
    "closed_import_refusal",
    "transaction_in_closed_period",
    "statement_line_in_closed_period",
]
