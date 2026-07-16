"""The accountant-package exporter — Slice 4 · B's local write path.

The §5.4 export write path: given an already-built, PROPOSED `PackageOut` (from
`views.build_package`), write a fresh, self-contained export folder to disk and
append one row to the append-only export log. **Local files only** — this module
opens no socket, no HTTP client, no mail: the deliverable is *for* the accountant,
but the human sends it by his own hand, outside the app (Slice-4 guardrail #1: no
transmission of any kind).

**framework-vNext.** The framework skill's docstring reserves a `PackageWriter`
port as a future instance piece (`generate_package.py` — "the instance provides a
PackageWriter"). v0.1.0's `ports.py` defines none, so this exporter is plain app
code playing that role. When a framework vNext defines a `PackageWriter` port,
this module is its natural adapter — but the port is a framework change and is
**never** added to `agent-classes` in this slice.

An export is a **fresh** folder `exports/<export_id>/` (never a rewrite of an
existing one) plus one appended log row. `export_id = "<period>--<UTC timestamp>"`
in a filesystem-safe basic form (no colons/spaces), so it is a legal path segment
on every OS and a re-export of the same period is always a new, distinct folder.

Folder contents — exactly four Core files:

- ``package.json``   — the `PackageOut` serialization verbatim (money strings, the
  per-entry trust trail, tax breakout, reconciliation trail, summary, basis).
- ``entries.csv``    — one row per entry (both the framework proposal and the human
  confirmation columns).
- ``tax_summary.csv``— per-target reclaimable rows + a ``PERIOD_TOTAL`` row.
- ``manifest.json``  — the export id/period/status/time, the basis (verbatim from
  config), and a per-file sha256 + byte count for the **other three** files (the
  manifest excludes itself from hashing).

**Money discipline.** Every money value already arrives from `PackageOut` as an
exact-`Decimal` string (the `schemas.py` boundary rule), so the exporter writes it
through verbatim and **never** coerces a money value through `float` — there is no
`float(` on any money path in this module (mirrors `closes.py`'s refuse-a-float
posture at the write boundary).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bookkeeper.config import BookkeeperConfig

from bookkeeper_ui.confirmations import SOURCE_HUMAN
from bookkeeper_ui.schemas import PackageOut

# The four Core file names. `manifest.json` indexes and hashes the other three; it
# is deliberately excluded from its own hash set (a manifest cannot hash itself).
PACKAGE_JSON = "package.json"
ENTRIES_CSV = "entries.csv"
TAX_SUMMARY_CSV = "tax_summary.csv"
MANIFEST_JSON = "manifest.json"

# The three hashed Core files, in the order they are written and recorded.
_HASHED_FILES = (PACKAGE_JSON, ENTRIES_CSV, TAX_SUMMARY_CSV)

ENTRIES_HEADER = [
    "date",
    "vendor",
    "description",
    "attribution_target_id",
    "amount",
    "tax",
    "proposed_account",
    "confidence",
    "source_rule",
    "confirmed_account",
    "confirmed_at",
    "transaction_id",
]

TAX_SUMMARY_HEADER = ["attribution_target_id", "transaction_count", "reclaimable"]

# The `source_rule` written for a human-confirmed line (a Slice-3 confirmed flag,
# whose `PackageEntryOut.source` is the app's `"human"` convention). Its synthetic
# `confidence=1.0` is never written as an agent confidence — the cell is left blank.
HUMAN_SOURCE_RULE = "human-confirmed"


@dataclass(frozen=True)
class ExportRecord:
    """One append-only export-log row — a self-describing record of one export.

    `export_id` / `period` / `package_status` identify the export; `exported_at` is
    its UTC timestamp; `files` is the per-file `{name, sha256, bytes}` for the three
    hashed Core files (the same hashes the manifest carries); `divergence_count` is
    the package's divergence count. Held with `files` as a tuple of JSON-native maps
    so a read-back (which arrives as JSON lists/dicts) compares equal to the
    constructed record.
    """

    export_id: str
    period: str
    package_status: str
    exported_at: datetime
    files: tuple[Mapping[str, object], ...]
    divergence_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))


def _export_id(period: str, exported_at: datetime) -> str:
    """`<period>--<UTC basic-form timestamp>` — a legal, unique path segment.

    Basic ISO form (no colons/spaces), microsecond-resolution so two exports of the
    same period in the same second never collide onto one folder.
    """
    stamp = exported_at.strftime("%Y%m%dT%H%M%S%fZ")
    return f"{period}--{stamp}"


def _entries_csv_bytes(package: PackageOut) -> bytes:
    """The `entries.csv` bytes — one row per entry, both account columns present.

    A `human`-source line writes ``human-confirmed`` in `source_rule` and leaves
    `confidence` blank (the synthetic `1.0` is never written as an agent
    confidence). Money cells (`amount` / `tax`) are the exact `str(Decimal)` from the
    package — never through `float`.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(ENTRIES_HEADER)
    for entry in package.entries:
        is_human = entry.source == SOURCE_HUMAN
        writer.writerow(
            [
                entry.transaction.date,
                entry.transaction.vendor,
                entry.transaction.description,
                entry.attribution_target_id,
                entry.transaction.amount,  # exact str(Decimal)
                entry.tax,  # exact str(Decimal)
                entry.proposed_account,
                "" if is_human else entry.confidence,
                HUMAN_SOURCE_RULE if is_human else entry.source,
                entry.confirmed_account or "",
                entry.confirmed_at or "",
                entry.transaction.id,
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _tax_summary_csv_bytes(package: PackageOut) -> bytes:
    """The `tax_summary.csv` bytes — per-target rows in skill order + a total row.

    Per-target rows follow the deterministic order the skill produced. The final
    ``PERIOD_TOTAL`` row carries the `regime` name (in the `transaction_count`
    column) and the `period_total` (in the `reclaimable` column) — both exact
    strings, never through `float`.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(TAX_SUMMARY_HEADER)
    tax = package.tax_breakout
    if tax is not None:
        for target in tax.per_target:
            writer.writerow(
                [
                    target.attribution_target_id,
                    target.transaction_count,
                    target.reclaimable,  # exact str(Decimal)
                ]
            )
        writer.writerow(["PERIOD_TOTAL", tax.regime, tax.period_total])
    return buffer.getvalue().encode("utf-8")


def _manifest_bytes(
    *,
    export_id: str,
    package: PackageOut,
    config: BookkeeperConfig,
    exported_at: datetime,
    app_version: str,
    files: list[Mapping[str, object]],
) -> bytes:
    """The `manifest.json` bytes — id/period/status/time, basis, and the file hashes.

    The basis is recorded **verbatim from config** (`accounting_method` /
    `jurisdiction` / `tax_regime` / `accountant_format`) — v0.1.0 has no format
    registry, so `accountant_format` is recorded and nothing more. `files` hashes the
    other three Core files; the manifest excludes itself.
    """
    manifest = {
        "export_id": export_id,
        "period": package.period,
        "package_status": package.status,
        "exported_at": exported_at.isoformat(),
        "app_version": app_version,
        "basis": {
            "accounting_method": config.accounting_method,
            "jurisdiction": config.jurisdiction,
            "tax_regime": config.tax_regime,
            "accountant_format": config.accountant_format,
        },
        "divergence_count": package.divergence_count,
        "files": list(files),
    }
    return json.dumps(manifest, indent=2).encode("utf-8")


def _file_record(name: str, data: bytes) -> dict[str, object]:
    """A `{name, sha256, bytes}` record for one written file's exact bytes."""
    return {
        "name": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def export_package(
    *,
    package: PackageOut,
    config: BookkeeperConfig,
    export_dir: Path,
    exported_at: datetime,
    app_version: str,
) -> ExportRecord:
    """Write a fresh export folder for a **PROPOSED** package; return its log record.

    Writes ``exports/<export_id>/`` with the four Core files and returns an
    `ExportRecord` the caller both appends to the log store and echoes as
    `ExportResultOut`. Never modifies or deletes an existing export folder: the leaf
    folder is created with ``exist_ok=False``, so a colliding id is a hard error, not
    a silent overwrite. Writes nothing outside the new folder.

    The caller gates on `package.status == "proposed"` before calling — a BLOCKED
    package never reaches here (nothing is written on the refusal path).
    """
    export_id = _export_id(package.period, exported_at)
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    folder = export_dir / export_id
    folder.mkdir(exist_ok=False)  # never overwrite an existing export

    # Write the three hashed Core files, hashing the exact bytes as written.
    package_bytes = json.dumps(package.model_dump(mode="json"), indent=2).encode("utf-8")
    entries_bytes = _entries_csv_bytes(package)
    tax_bytes = _tax_summary_csv_bytes(package)

    (folder / PACKAGE_JSON).write_bytes(package_bytes)
    (folder / ENTRIES_CSV).write_bytes(entries_bytes)
    (folder / TAX_SUMMARY_CSV).write_bytes(tax_bytes)

    files = [
        _file_record(PACKAGE_JSON, package_bytes),
        _file_record(ENTRIES_CSV, entries_bytes),
        _file_record(TAX_SUMMARY_CSV, tax_bytes),
    ]

    # The manifest indexes and hashes the other three; it excludes itself.
    manifest_bytes = _manifest_bytes(
        export_id=export_id,
        package=package,
        config=config,
        exported_at=exported_at,
        app_version=app_version,
        files=files,
    )
    (folder / MANIFEST_JSON).write_bytes(manifest_bytes)

    return ExportRecord(
        export_id=export_id,
        period=package.period,
        package_status=package.status,
        exported_at=exported_at,
        files=tuple(files),
        divergence_count=package.divergence_count,
    )


def _to_row(record: ExportRecord) -> dict[str, object]:
    """Flatten an `ExportRecord` to its JSONL row (ISO datetime, files as-is)."""
    return {
        "export_id": record.export_id,
        "period": record.period,
        "package_status": record.package_status,
        "exported_at": record.exported_at.isoformat(),
        "files": [dict(f) for f in record.files],
        "divergence_count": record.divergence_count,
    }


def _from_row(row: Mapping[str, object]) -> ExportRecord:
    """Reconstruct an `ExportRecord` from a JSONL row (files verbatim, ISO datetime)."""
    return ExportRecord(
        export_id=str(row["export_id"]),
        period=str(row["period"]),
        package_status=str(row["package_status"]),
        exported_at=datetime.fromisoformat(str(row["exported_at"])),
        files=tuple(row.get("files") or ()),  # type: ignore[arg-type]
        divergence_count=int(row["divergence_count"]),  # type: ignore[call-overload]
    )


class FileExportStore:
    """A JSONL-backed, append-only store of export-log rows.

    Construct with the path to the export log (`<export_dir>/exports.jsonl`, created
    on first write, parents included). Mirrors the other stores' discipline — async
    methods, one JSON object per line, whole-file reads. **Append-only**: a re-export
    is a new row (and a new folder); the store never rewrites or truncates, and the
    per-export folders it references are likewise never touched.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    async def record(self, record: ExportRecord) -> None:
        """Append one export to the log (never rewrites/truncates)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_row(record)) + "\n")

    async def all(self) -> list[ExportRecord]:
        """Every recorded export, in export (insertion) order — the full trail."""
        results: list[ExportRecord] = []
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                results.append(_from_row(json.loads(line)))
        return results


__all__ = ["ExportRecord", "FileExportStore", "export_package"]
