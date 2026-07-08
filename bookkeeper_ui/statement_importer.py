"""Statement import — CSV and JSON rows → framework `StatementLine` objects.

The reconcile counterpart to `importer.py`: it maps a plain tabular/record bank
or card export onto the framework's `StatementLine` model (`bookkeeper/model.py`).
It is the *only* place raw statement strings become `Decimal` money and
`datetime` dates, so every coercion `reconcile_account` relies on happens once,
here.

Expected columns / keys (same names for CSV headers and JSON object keys):

    | field         | required | maps to                        |
    |---------------|----------|--------------------------------|
    | statement_ref | yes      | StatementLine.statement_ref    |
    | date          | yes      | StatementLine.date (ISO 8601)  |
    | amount        | yes      | StatementLine.amount (Decimal) |
    | description   | no       | StatementLine.description ("" if absent) |

`statement_ref` is **required** — it is the §1 link back to the authoritative
feed that every matched pair and surfaced gap traces to; a statement line with
no ref cannot be reconciled traceably, so an absent ref fails the import.

Boundary rules (matching the framework's model contract, same as `importer.py`):
- **Money is `Decimal`, never float** — string amounts go through `str`→`Decimal`
  (``"82.50"`` exact), and JSON is parsed with ``parse_float=Decimal`` so an
  *unquoted* numeric amount (``"amount": 45.99``) is exact currency too, never a
  lossy float. Money must also be *finite* — ``Infinity``/``NaN`` (JSON literal,
  string, or CSV value) is rejected with a named error, never stored. A NaN
  amount silently breaks reconcile's exact-equality amount match (which is even
  more sensitive than the ledger sum), so this guard is load-bearing here.
- **`date` is ISO 8601** (``2026-04-03`` or a full timestamp), via
  `datetime.fromisoformat`.
- **`description` is a plain `str`** — absent / blank coalesces to ``""`` (there
  is no None-description on the model), and it is the fuzzy disambiguator
  `reconcile_account` uses when several candidates share an amount and a date.

Sign convention (documented, **not** "fixed" in code): amounts use the same sign
convention as the imported ledger — the examples use positive expenses.
`reconcile_account` matches amounts by exact *signed* `Decimal` equality, so a
sign-flipped export (e.g. a bank that renders debits negative) reconciles as
all-gaps, truthfully — the app never silently normalizes a sign, because doing
so would manufacture matches the feed does not support.

`import_csv` / `import_json` / `import_file` (path) and `import_bytes` (upload)
all return `StatementLine`s in file order; persist them with
`FileStatementStore.store` (issue B). Import itself writes nothing — it only
builds models, and a malformed file builds *nothing* (it fails before any store).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bookkeeper.model import StatementLine

_REQUIRED_FIELDS = ("statement_ref", "date", "amount")

# csv.DictReader stashes any values past the header row under this key. A row that
# has them is ragged (an unquoted comma / stray column); it is rejected with a
# named error rather than letting a `None` restkey slip a mis-aligned line past.
_CSV_RESTKEY = "__extra_columns__"


class StatementImportError(ValueError):
    """Raised when a statement import row is missing a required field or has a bad value.

    A distinct type (not the builtin `ImportError`) so callers can catch import
    problems specifically and a bad file fails clearly, naming the offending row.
    The reconcile analog of `importer.TransactionImportError`.
    """


def _clean(value: object) -> str:
    """A row value as a stripped string ("" for None)."""
    return "" if value is None else str(value).strip()


def _parse_decimal(value: object, field: str, row_index: int) -> Decimal:
    text = _clean(value)
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise StatementImportError(
            f"row {row_index}: {field} {text!r} is not a valid decimal amount"
        ) from exc
    # Decimal accepts "Infinity"/"NaN" (and json turns the bare literals into
    # floats that stringify back to them) — a non-finite amount silently breaks
    # reconcile's exact-equality match, so reject it here at the one coercion point.
    if not parsed.is_finite():
        raise StatementImportError(
            f"row {row_index}: {field} {text!r} must be a finite number"
        )
    return parsed


def _parse_date(value: object, row_index: int) -> datetime:
    text = _clean(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise StatementImportError(
            f"row {row_index}: date {text!r} is not ISO 8601 (e.g. 2026-04-03)"
        ) from exc


def row_to_statement_line(row: Mapping[str, object], row_index: int = 0) -> StatementLine:
    """Map one import row to a `StatementLine`, coercing money/date at the boundary.

    Raises `StatementImportError` (naming the row) on a missing required field or
    an unparseable amount/date, so a bad import fails clearly rather than silently
    dropping or mis-typing a row.
    """
    missing = [f for f in _REQUIRED_FIELDS if not _clean(row.get(f))]
    if missing:
        raise StatementImportError(
            f"row {row_index}: missing required field(s): {', '.join(missing)}"
        )

    return StatementLine(
        statement_ref=_clean(row["statement_ref"]),
        date=_parse_date(row["date"], row_index),
        amount=_parse_decimal(row["amount"], "amount", row_index),
        description=_clean(row.get("description")),
    )


def _rows_to_statement_lines(rows: Sequence[Mapping[str, object]]) -> list[StatementLine]:
    """Map rows to `StatementLine`s in order, numbering each for error messages."""
    return [row_to_statement_line(row, index) for index, row in enumerate(rows, start=1)]


def _parse_csv(text: str) -> list[StatementLine]:
    """Parse CSV text (headers = the module's columns) → models, in file order.

    Read through ``io.StringIO(text, newline="")`` — the csv module's required
    newline handling — so a quoted field with an embedded newline parses intact,
    and the path and bytes import paths agree byte-for-byte on the same file (see
    `import_csv` / `import_bytes`). A ragged row (more values than headers) is a
    `StatementImportError` naming the row, not a silently mis-aligned line.
    """
    reader = csv.DictReader(io.StringIO(text, newline=""), restkey=_CSV_RESTKEY)
    lines: list[StatementLine] = []
    for index, row in enumerate(reader, start=1):
        if row.get(_CSV_RESTKEY):
            raise StatementImportError(
                f"row {index}: more values than headers — check for an unquoted "
                f"comma or a stray column"
            )
        lines.append(row_to_statement_line(row, index))
    return lines


def _parse_json(text: str) -> list[StatementLine]:
    """Parse JSON text → models. Accepts a top-level list of row objects or a
    ``{"lines": [...]}`` wrapper; keys match the CSV columns.

    Numbers are parsed with ``parse_float=Decimal`` so an *unquoted* amount
    (``"amount": 45.99``) is exact currency, never a lossy float. A malformed
    document, a wrapper missing its ``lines`` key, or a non-object row is a
    `StatementImportError` naming the problem — never a 500 or a silent drop.

    Note the wrapper key is ``lines`` (a statement is a list of lines), **not**
    ``transactions`` as the ledger importer uses — the two feeds stay distinct.
    """
    try:
        data = json.loads(text, parse_float=Decimal)
    except ValueError as exc:  # JSONDecodeError is a ValueError
        raise StatementImportError(f"not valid JSON — {exc}") from exc

    if isinstance(data, Mapping):
        if "lines" not in data:
            raise StatementImportError(
                "JSON object is missing a 'lines' key — expected a "
                '{"lines": [...]} wrapper or a top-level list of rows'
            )
        rows = data["lines"]
    else:
        rows = data

    if not isinstance(rows, list):
        raise StatementImportError(
            "expected a list of statement rows (a JSON array)"
        )
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise StatementImportError(
                f"row {index}: expected a JSON object, got {type(row).__name__}"
            )
    return _rows_to_statement_lines(rows)


def import_csv(path: str | Path) -> list[StatementLine]:
    """Read a CSV of statement lines (see module docstring for columns) → models."""
    # newline="" (not read_text) so csv sees raw line endings — matches the bytes
    # path in `import_bytes` and keeps embedded-newline quoted fields intact.
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return _parse_csv(handle.read())


def import_json(path: str | Path) -> list[StatementLine]:
    """Read a JSON of statement lines → models.

    Accepts either a top-level list of row objects, or a ``{"lines": [...]}``
    wrapper. Keys match the CSV columns (see module docstring).
    """
    return _parse_json(Path(path).read_text(encoding="utf-8"))


def import_bytes(data: bytes, filename: str) -> list[StatementLine]:
    """Import an uploaded CSV/JSON blob → models, dispatched by `filename` suffix.

    The upload counterpart to `import_file`: same columns, same boundary rules
    (exact `Decimal` money, blank description → "", ISO dates), but sourced from a
    request body rather than a path — so issue B's `POST /statements/import`
    reuses the one import boundary rather than re-deriving the coercions. Raises
    `StatementImportError` on an unsupported suffix, non-UTF-8 bytes, or any bad
    row (naming it).
    """
    suffix = Path(filename).suffix.lower()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StatementImportError(
            f"{filename!r} is not valid UTF-8 text — expected a CSV or JSON file"
        ) from exc
    if suffix == ".csv":
        return _parse_csv(text)
    if suffix == ".json":
        return _parse_json(text)
    raise StatementImportError(
        f"unsupported import format {suffix!r} — expected .csv or .json"
    )


def import_file(path: str | Path) -> list[StatementLine]:
    """Import `.csv` or `.json` by suffix → models."""
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return import_csv(path)
    if suffix == ".json":
        return import_json(path)
    raise StatementImportError(
        f"unsupported import format {suffix!r} — expected .csv or .json"
    )
