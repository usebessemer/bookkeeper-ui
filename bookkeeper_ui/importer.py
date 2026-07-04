"""Transaction import — CSV and JSON rows → framework `Transaction` objects.

The import boundary maps a plain tabular/record source onto the framework's
`Transaction` model (`bookkeeper/model.py`). It is the *only* place raw source
strings become `Decimal` money and `datetime` dates, so every coercion the
framework relies on happens once, here.

Expected columns / keys (same names for CSV headers and JSON object keys):

    | field                  | required | maps to                          |
    |------------------------|----------|----------------------------------|
    | date                   | yes      | Transaction.date (ISO 8601)      |
    | vendor                 | yes      | Transaction.vendor               |
    | amount                 | yes      | Transaction.amount (Decimal)     |
    | attribution_target_id  | yes      | Transaction.attribution_target_id|
    | tax                    | no       | Transaction.tax (Decimal; blank → 0) |
    | description            | no       | Transaction.description ("" if absent) |

Boundary rules (matching the framework's model contract):
- **Money is `Decimal`, never float** — string amounts go through `str`→`Decimal`
  (``"45.99"`` exact), and JSON is parsed with ``parse_float=Decimal`` so an
  *unquoted* numeric amount (``"amount": 45.99``) is exact currency too, never a
  lossy float.
- **Absent / blank `tax` coalesces to `Decimal("0")`** — the framework never
  holds None-money (see `Extractor.extract` / `LedgerSource`).
- **`date` is ISO 8601** (``2026-04-03`` or a full timestamp), via
  `datetime.fromisoformat`.
- **`artifact_bytes`** is set to the row's own JSON serialization — the source
  record stays linked to the stored figure (charter §1: fully traceable). This
  is a CSV/JSON line, so its bytes *are* the source artifact.

`import_csv` / `import_json` return `Transaction`s in file order; persist them
with `FileLedgerStore.store` (or use `import_and_store`). Import itself writes
nothing — it only builds models.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bookkeeper.model import Transaction

from bookkeeper.ports import LedgerSink

_REQUIRED_FIELDS = ("date", "vendor", "amount", "attribution_target_id")

# csv.DictReader stashes any values past the header row under this key. A row that
# has them is ragged (an unquoted comma / stray column); it is rejected with a
# named error rather than letting a `None` restkey crash artifact serialization.
_CSV_RESTKEY = "__extra_columns__"


class TransactionImportError(ValueError):
    """Raised when an import row is missing a required field or has a bad value.

    A distinct type (not the builtin `ImportError`) so callers can catch import
    problems specifically and a bad file fails clearly, naming the offending row.
    """


def _clean(value: object) -> str:
    """A row value as a stripped string ("" for None)."""
    return "" if value is None else str(value).strip()


def _parse_decimal(value: object, field: str, row_index: int) -> Decimal:
    text = _clean(value)
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise TransactionImportError(
            f"row {row_index}: {field} {text!r} is not a valid decimal amount"
        ) from exc


def _parse_date(value: object, row_index: int) -> datetime:
    text = _clean(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise TransactionImportError(
            f"row {row_index}: date {text!r} is not ISO 8601 (e.g. 2026-04-03)"
        ) from exc


def row_to_transaction(row: Mapping[str, object], row_index: int = 0) -> Transaction:
    """Map one import row to a `Transaction`, coercing money/date at the boundary.

    Raises `TransactionImportError` (naming the row) on a missing required field
    or an unparseable amount/date, so a bad import fails clearly rather than
    silently dropping or mis-typing a row.
    """
    missing = [f for f in _REQUIRED_FIELDS if not _clean(row.get(f))]
    if missing:
        raise TransactionImportError(
            f"row {row_index}: missing required field(s): {', '.join(missing)}"
        )

    # Blank / absent tax → Decimal("0"): the framework never holds None-money.
    raw_tax = _clean(row.get("tax"))
    tax = Decimal("0") if raw_tax == "" else _parse_decimal(raw_tax, "tax", row_index)

    return Transaction(
        attribution_target_id=_clean(row["attribution_target_id"]),
        vendor=_clean(row["vendor"]),
        amount=_parse_decimal(row["amount"], "amount", row_index),
        tax=tax,
        date=_parse_date(row["date"], row_index),
        description=_clean(row.get("description")),
        # The source row itself is the traceable artifact for a CSV/JSON import.
        artifact_bytes=json.dumps(
            {k: _clean(v) for k, v in row.items()}, sort_keys=True
        ).encode("utf-8"),
    )


def _rows_to_transactions(rows: Sequence[Mapping[str, object]]) -> list[Transaction]:
    """Map rows to `Transaction`s in order, numbering each for error messages."""
    return [row_to_transaction(row, index) for index, row in enumerate(rows, start=1)]


def _parse_csv(text: str) -> list[Transaction]:
    """Parse CSV text (headers = the module's columns) → models, in file order.

    Read through ``io.StringIO(text, newline="")`` — the csv module's required
    newline handling — so a quoted field with an embedded newline parses intact,
    and the path and bytes import paths agree byte-for-byte on the same file (see
    `import_csv` / `import_bytes`). A ragged row (more values than headers) is a
    `TransactionImportError` naming the row, not a downstream crash.
    """
    reader = csv.DictReader(io.StringIO(text, newline=""), restkey=_CSV_RESTKEY)
    transactions: list[Transaction] = []
    for index, row in enumerate(reader, start=1):
        if row.get(_CSV_RESTKEY):
            raise TransactionImportError(
                f"row {index}: more values than headers — check for an unquoted "
                f"comma or a stray column"
            )
        transactions.append(row_to_transaction(row, index))
    return transactions


def _parse_json(text: str) -> list[Transaction]:
    """Parse JSON text → models. Accepts a top-level list of row objects or a
    ``{"transactions": [...]}`` wrapper; keys match the CSV columns.

    Numbers are parsed with ``parse_float=Decimal`` so an *unquoted* amount
    (``"amount": 45.99``) is exact currency, never a lossy float. A malformed
    document, a wrapper missing its ``transactions`` key, or a non-object row is a
    `TransactionImportError` naming the problem — never a 500 or a silent drop.
    """
    try:
        data = json.loads(text, parse_float=Decimal)
    except ValueError as exc:  # JSONDecodeError is a ValueError
        raise TransactionImportError(f"not valid JSON — {exc}") from exc

    if isinstance(data, Mapping):
        if "transactions" not in data:
            raise TransactionImportError(
                "JSON object is missing a 'transactions' key — expected a "
                '{"transactions": [...]} wrapper or a top-level list of rows'
            )
        rows = data["transactions"]
    else:
        rows = data

    if not isinstance(rows, list):
        raise TransactionImportError(
            "expected a list of transaction rows (a JSON array)"
        )
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise TransactionImportError(
                f"row {index}: expected a JSON object, got {type(row).__name__}"
            )
    return _rows_to_transactions(rows)


def import_csv(path: str | Path) -> list[Transaction]:
    """Read a CSV of transactions (see module docstring for columns) → models."""
    # newline="" (not read_text) so csv sees raw line endings — matches the bytes
    # path in `import_bytes` and keeps embedded-newline quoted fields intact.
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return _parse_csv(handle.read())


def import_json(path: str | Path) -> list[Transaction]:
    """Read a JSON of transactions → models.

    Accepts either a top-level list of row objects, or a ``{"transactions":
    [...]}`` wrapper. Keys match the CSV columns (see module docstring).
    """
    return _parse_json(Path(path).read_text(encoding="utf-8"))


def import_bytes(data: bytes, filename: str) -> list[Transaction]:
    """Import an uploaded CSV/JSON blob → models, dispatched by `filename` suffix.

    The upload counterpart to `import_file`: same columns, same boundary rules
    (exact `Decimal` money, blank tax → 0, ISO dates), but sourced from a
    request body rather than a path — so the API's `POST /import` reuses the one
    import boundary rather than re-deriving the coercions. Raises
    `TransactionImportError` on an unsupported suffix, non-UTF-8 bytes, or any
    bad row (naming it).
    """
    suffix = Path(filename).suffix.lower()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TransactionImportError(
            f"{filename!r} is not valid UTF-8 text — expected a CSV or JSON file"
        ) from exc
    if suffix == ".csv":
        return _parse_csv(text)
    if suffix == ".json":
        return _parse_json(text)
    raise TransactionImportError(
        f"unsupported import format {suffix!r} — expected .csv or .json"
    )


def import_file(path: str | Path) -> list[Transaction]:
    """Import `.csv` or `.json` by suffix → models."""
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return import_csv(path)
    if suffix == ".json":
        return import_json(path)
    raise TransactionImportError(
        f"unsupported import format {suffix!r} — expected .csv or .json"
    )


async def import_and_store(path: str | Path, sink: LedgerSink) -> list[Transaction]:
    """Import a CSV/JSON file and persist each transaction via `sink` (idempotent).

    Returns the imported transactions (in file order). Re-running the same import
    is a no-op on an idempotent sink like `FileLedgerStore` — no duplicate rows.
    """
    transactions = import_file(path)
    for transaction in transactions:
        await sink.store(transaction)
    return transactions
