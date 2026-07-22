"""The Tier-2 read/write API the thin UI talks to (FastAPI, async).

Slice 1 (categorize) and Slice 2 (reconcile) operations over the #1 foundation,
plus the serialization boundary:

- ``POST /import``            — upload a CSV/JSON → persist via the `LedgerSink`.
- ``POST /categorize?period`` — call the framework's `categorize` **unmodified**
  → return the `CategorizationReport` (proposals = the trust trail; flagged).
- ``POST /resolve``           — record a confirm/correct decision (account
  validated against `chart_of_accounts`; transaction id must be one the ledger
  holds — a strict 404 otherwise, never an orphan) to the confirmation store.
- ``GET  /ledger?period``     — the categorized ledger: every transaction with
  its resolved account (if confirmed) or its pending status (proposed / flagged),
  plus its Slice 2 `reconciliation` fold.
- ``POST /statements/import`` — upload a CSV/JSON statement → persist its lines.
- ``GET  /statements?period`` — the stored statement lines (a truth surface).
- ``POST /reconcile?period``  — call `reconcile_account` **unmodified** → the raw
  report (matched / to_confirm / gaps). Detection-only; writes nothing.
- ``POST /reconcile/resolve`` — record a confirm/reject/acknowledge resolution
  (server-validated: 422 on a bad shape, 404 on an unknown id) — the *only*
  reconcile write path, into the reconciliation store alone.
- ``GET  /reconcile/view?period`` — the overlaid projection: the report annotated
  with each item's resolution status (the one truth the UI + ledger fold share).
- ``GET  /close?period``      — the close-review projection (Slice 3): the framework
  `close_period` checklist over the *effective* reports (raw skill output + persisted
  human resolutions), plus tax, anomalies, and the app gates. Read-only.
- ``POST /anomalies/review``  — acknowledge one *current* anomaly flag (422 if the
  `flag_id` matches no current flag; 409 if the period is closed) → append the ack row.
- ``POST /reconciliation/waive`` — waive reconciliation for a *no-statement* period
  (409 if a statement exists or the period is closed) → append the waiver row.
- ``POST /sign``               — the §5.7 sign-off (Slice 3): re-verify the whole
  close server-side (400 on a non-quarterly label, 409 on an empty/already-closed
  period or any unmet gate) → append **exactly one** durable, self-contained close
  record. The correctness core — the sole write on the pass path.

**Async on purpose** — it matches the framework's async ports/skills contract and
serves the #3 UI. **The framework stays pure**: this module and `schemas.py` own
all the web/pydantic surface; nothing here is pushed back into `../agent-classes`.
**Writes only through its own stores** — `categorize` and `reconcile_account`
write nothing; the sole write paths are a human confirmation via `/resolve` and a
human reconcile resolution via `/reconcile/resolve`.

The app is built by `create_app(...)` with its config + stores **injected**, so a
test drives it over a tmp-path ledger. `build_app_from_env()` is the runnable
default (see its docstring) for ``uvicorn bookkeeper_ui.api:build_app_from_env
--factory``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Response, UploadFile

from bookkeeper.config import BookkeeperConfig
from bookkeeper.model import Transaction
from bookkeeper.skills.categorize import categorize
from bookkeeper.skills.flag_anomaly import flag_anomaly
from bookkeeper.skills.generate_package import PackageStatus
from bookkeeper.skills.reconcile import reconcile_account
from bookkeeper.skills.track_tax import UnknownTaxRegime

from bookkeeper_ui.anomaly_reviews import (
    AnomalyReview,
    FileAnomalyReviewStore,
    derive_flag_id,
)
from bookkeeper_ui.candidates import (
    ACTION_CONFIRM,
    ACTION_REJECT,
    LEDGER_OUTCOME_ALREADY_PRESENT,
    LEDGER_OUTCOME_STORED,
    SOURCE_HUMAN as CANDIDATE_SOURCE_HUMAN,
    CandidateDecision,
    CandidateSubmission,
    FileArtifactStore,
    FileCandidateDecisionStore,
    FileCandidateStore,
    candidate_id as compute_candidate_id,
)
from bookkeeper_ui.closes import (
    FileCloseStore,
    closed_import_refusal,
    closed_periods,
    statement_line_in_closed_period,
    transaction_in_closed_period,
)
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import (
    SOURCE_HUMAN,
    Confirmation,
    FileConfirmationStore,
)
from bookkeeper_ui.exporter import FileExportStore, export_package
from bookkeeper_ui.importer import TransactionImportError, import_bytes
from bookkeeper_ui.intake_scan import scan_drop_dir
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.periods import is_quarterly_period, period_of
from bookkeeper_ui.reconciliations import (
    NOTE_REQUIRED_DECISIONS,
    PAIR_DECISIONS,
    VALID_DECISIONS,
    FileReconciliationStore,
    Reconciliation,
)
from bookkeeper_ui.schemas import (
    AnomalyReviewOut,
    AnomalyReviewRequest,
    CandidateOut,
    CandidateResolutionOut,
    CandidateSubmitOut,
    CandidateSubmitRequest,
    CategorizationReportOut,
    CloseRecordOut,
    CloseReviewOut,
    ConfirmationOut,
    IntakeQueueOut,
    ExportRecordOut,
    ExportResultOut,
    ImportResultOut,
    LedgerOut,
    PackageOut,
    ReconcileResolutionOut,
    ReconciliationReportOut,
    ReconciliationViewOut,
    ResolveCandidateRequest,
    ResolveReconcileRequest,
    ResolveRequest,
    ScanFileErrorOut,
    ScanResultOut,
    SignRequest,
    StatementImportResultOut,
    StatementLineOut,
    StatementLinesOut,
    TransactionOut,
    WaiveRequest,
    WaiverOut,
)
from bookkeeper_ui.statement_importer import StatementImportError
from bookkeeper_ui.statement_importer import import_bytes as import_statement_bytes
from bookkeeper_ui.statement_store import FileStatementStore
from bookkeeper_ui.views import (
    build_close_record,
    build_close_review,
    build_intake_queue,
    build_ledger,
    build_package,
    build_reconciliation,
)
from bookkeeper_ui.waivers import FileWaiverStore, Waiver
from bookkeeper_ui.web import register_ui

# The intake port's artifact allowlist (AC #3): the media types an extractor may
# submit. A candidate declaring anything else is a 422 — nothing is written.
ALLOWED_ARTIFACT_MEDIA_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "application/pdf",
        "text/plain",
    }
)

# The default cap on a decoded artifact's size. `BOOKKEEPER_UI_MAX_ARTIFACT_BYTES`
# overrides it at `build_app_from_env`; a test can pass `max_artifact_bytes` to
# `create_app` directly. A candidate whose decoded bytes exceed it is a 422.
DEFAULT_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024  # 10 MiB


def _require_nonblank(value: str | None, field: str) -> str:
    """Return `value` if it is a non-blank string, else raise a 422 naming the field.

    The intake port's field rules are an invariant, not courtesy (AC #3): a blank
    `source` / `submission_id` / `vendor` is a 422 with the field named, never a
    partial write.
    """
    if value is None or not value.strip():
        raise HTTPException(
            status_code=422, detail=f"{field} is required and must be a non-blank string."
        )
    return value


def _parse_money(raw: str, field: str) -> Decimal:
    """Parse a JSON-string money value to a finite `Decimal`, else raise a 422.

    Money crosses the intake wire as a **string** (a JSON number never reaches here —
    the request schema types the field `str`). `NaN` / `Infinity` parse as valid
    `Decimal`s but are not finite, so they are rejected too — no float bug on this
    new path (AC #3, guardrail 4).
    """
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field} {raw!r} is not a valid decimal amount (money is a string).",
        ) from exc
    if not value.is_finite():
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be a finite amount — {raw!r} (NaN/Infinity) is rejected.",
        )
    return value


def _parse_iso_datetime(raw: str, field: str) -> datetime:
    """Parse an ISO 8601 string to a `datetime`, else raise a 422 naming the field."""
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} {raw!r} is not an ISO 8601 date/datetime."
        ) from exc


def create_app(
    *,
    config: BookkeeperConfig,
    ledger_store: FileLedgerStore,
    confirmation_store: FileConfirmationStore,
    statement_store: FileStatementStore,
    reconciliation_store: FileReconciliationStore,
    close_store: FileCloseStore | None = None,
    anomaly_review_store: FileAnomalyReviewStore | None = None,
    waiver_store: FileWaiverStore | None = None,
    export_dir: str | Path | None = None,
    candidate_store: FileCandidateStore | None = None,
    candidate_decision_store: FileCandidateDecisionStore | None = None,
    artifact_store: FileArtifactStore | None = None,
    intake_drop_dir: str | Path | None = None,
    max_artifact_bytes: int | None = None,
    attribution_target_labels: dict[str, str] | None = None,
) -> FastAPI:
    """Build the API over an injected config + the four #1/Slice-2 stores.

    Dependencies are passed in (not read from a global) so a test can drive the
    app over a temp-path ledger, statement, confirmation, and reconciliation trail,
    and #3 can wire the real local paths. The stores are the *only* things the
    routes write through — `categorize` and `reconcile_account` are called
    read-only, and the sole reconcile write path is `/reconcile/resolve` (into the
    reconciliation store alone).

    The three Slice-3 stores (`close_store` / `anomaly_review_store` /
    `waiver_store`) are **optional** (default `None`) so the shipped Slice-1/Slice-2
    call sites — which pass exactly the five kwargs above — keep working unchanged.
    `close_store` is the single source of closed-period truth every write-path guard
    probes (an unset store means no period is closed, so the guards are inert and the
    behaviour is exactly pre-Slice-3). The `anomaly_review_store` / `waiver_store` are
    the append-only targets of the two Slice-3 write endpoints (`/anomalies/review` /
    `/reconciliation/waive`); each endpoint refuses a **503** if its store is unwired,
    so a Slice-1/2 app never silently no-ops a write.

    `export_dir` (Slice-4 · B) is **optional** (default `None`) on the same footing:
    the Slice-4 export write path (`POST /export`) writes the export folder under it
    and appends to its `exports.jsonl` log (built over the dir here). When unwired
    both export routes refuse a **503** — never a silent no-op — so every pre-Slice-4
    call site keeps constructing the app unchanged.

    The three Slice-5 intake stores (`candidate_store` / `candidate_decision_store` /
    `artifact_store`) are **optional** (default `None`) on the same footing: the JSON
    `/intake/*` surface (submit a candidate → the human confirm/reject that gates it
    into the ledger) reads/writes through them. When any is unwired the intake routes
    refuse a **503** — never a silent no-op — so every pre-Slice-5 call site keeps
    constructing the app unchanged. `max_artifact_bytes` caps a submitted artifact's
    decoded size (default `DEFAULT_MAX_ARTIFACT_BYTES`); `build_app_from_env` reads
    the `BOOKKEEPER_UI_MAX_ARTIFACT_BYTES` env override.

    `intake_drop_dir` (Slice-5 · A3) is the **optional** offline drop directory a scan
    ingests candidate `*.json` files from — the second front door onto the *same* A1
    validate/store path (`POST /intake/scan` + its UI twin). The feature is **enabled iff
    `intake_drop_dir is not None`**: when unset the scan routes refuse a **503** (never a
    silent no-op — the export-route precedent) and the UI hides the scan button + the
    win-state "scan drop folder" prompt, so the MUST capture flow never depends on this
    SHOULD feature. `build_app_from_env` always passes a default path (so the running app
    has it enabled); a test constructs `create_app` without it to exercise the disabled path.

    `attribution_target_labels` (Slice-5 · B) is the **optional** app-side sidecar map
    (id string → human label) the intake-review `<select>` renders through. It is *not*
    a framework `BookkeeperConfig` field (`from_mapping` silently drops unknown keys),
    so it is read app-side (`build_app_from_env` pulls it from the config JSON) and
    threaded straight through to `register_ui` — default `None` → an empty map, so the
    `<select>` falls back to the raw ids and pre-Slice-5 call sites are unchanged.
    """
    # The append-only export log lives beside the per-export folders, under the
    # injected export dir. Unwired → no export surface (the routes 503).
    export_store: FileExportStore | None = (
        FileExportStore(Path(export_dir) / "exports.jsonl")
        if export_dir is not None
        else None
    )
    # The intake artifact-size cap: the injected value, else the module default.
    artifact_cap = (
        max_artifact_bytes if max_artifact_bytes is not None else DEFAULT_MAX_ARTIFACT_BYTES
    )
    app = FastAPI(
        title="bookkeeper-ui API",
        description=(
            "Local, single-user read/write API for the Bessemer Bookkeeper: "
            "import → categorize → confirm/correct → read the categorized ledger."
        ),
    )

    @app.get("/health", summary="Liveness check")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/import", response_model=ImportResultOut, summary="Import & persist transactions")
    async def import_transactions(file: UploadFile = File(...)) -> ImportResultOut:
        """Upload a CSV/JSON of transactions → persist each via the `LedgerSink`.

        Format is dispatched by the upload's filename suffix (`.csv` / `.json`);
        the store is idempotent, so re-importing the same file adds no duplicate
        rows. A malformed file (bad suffix, non-UTF-8, bad row) is a 400 naming
        the problem, not a partial-silent import.
        """
        data = await file.read()
        try:
            transactions = import_bytes(data, file.filename or "")
        except TransactionImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Closed-period guard: refuse the *whole* upload if any parsed row lands in
        # a closed period (nothing persisted — validate before the store loop,
        # mirroring the nothing-on-failure import rule). Each row carries its own
        # date, so `period_of` is directly available; closed truth is the close store.
        closed = await closed_periods(close_store)
        offending = [
            (f"{t.vendor} {t.amount} on {t.date.date().isoformat()}", period_of(t.date))
            for t in transactions
            if period_of(t.date) in closed
        ]
        if offending:
            raise HTTPException(status_code=400, detail=closed_import_refusal(offending))

        for transaction in transactions:
            await ledger_store.store(transaction)

        return ImportResultOut(
            imported=len(transactions),
            transactions=[TransactionOut.from_model(t) for t in transactions],
        )

    @app.post(
        "/categorize",
        response_model=CategorizationReportOut,
        summary="Run categorize → proposals + flagged (the trust trail)",
    )
    async def categorize_period(period: str) -> CategorizationReportOut:
        """Call the framework's `categorize(source, config, period)` **as-is** and
        serialize the report. Proposals carry `proposed_account` / `confidence` /
        `source` (the rule that fired); flagged carry `reason`. Writes nothing —
        proposals-only (§5.4); the confirm/correct step is `/resolve`.
        """
        report = await categorize(ledger_store, config, period)
        return CategorizationReportOut.from_model(report)

    @app.post("/resolve", response_model=ConfirmationOut, summary="Confirm/correct a category")
    async def resolve(request: ResolveRequest) -> ConfirmationOut:
        """Record one human confirm/correct decision into the confirmation store.

        Two guards, both write nothing on rejection:

        - **422** an `account` not in `config.chart_of_accounts` — §5.2 holds even
          for a human-through-the-API decision: never file an invented category.
        - **404** a `transaction_id` no stored transaction carries — §5-conservative
          (N1, decided 2026-07-06): a confirmation must never dangle against
          nothing, so a typo'd id is refused rather than persisted as an orphan.

        The account guard runs first: an invented category is rejected before the
        transaction is even looked up. Then the closed-period guard (§5.7: a signed
        close is durable) refuses a **409** for a known transaction in a closed
        period, before the existence check — an *unknown* id (in no closed period)
        falls through to the unchanged N1 404. Append-only: a correction is a new
        row the ledger view collapses to last-write-wins.
        """
        if request.account not in config.chart_of_accounts:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"account {request.account!r} is not in chart_of_accounts — "
                    f"choose one of the configured accounts (§5.2: never invent a "
                    f"category)."
                ),
            )

        closed_period = await transaction_in_closed_period(
            close_store, ledger_store, request.transaction_id
        )
        if closed_period is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"transaction {request.transaction_id!r} is in closed period "
                    f"{closed_period!r} — its books are write-guarded and cannot be "
                    f"re-resolved (§5.7: a signed close is durable)."
                ),
            )

        if not await ledger_store.contains(request.transaction_id):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"transaction {request.transaction_id!r} is not in the ledger — "
                    f"a confirmation must never dangle against nothing (N1, "
                    f"§5-conservative: typo-safe)."
                ),
            )

        confirmation = Confirmation(
            transaction_id=request.transaction_id,
            account=request.account,
            source=SOURCE_HUMAN,
            decided_at=datetime.now(timezone.utc),
        )
        await confirmation_store.record(confirmation)
        return ConfirmationOut.from_model(confirmation)

    @app.get("/ledger", response_model=LedgerOut, summary="The categorized ledger")
    async def ledger(period: str) -> LedgerOut:
        """The categorized ledger for `period`: every stored transaction (in the
        store's deterministic read order) annotated with its current standing —
        `confirmed` (resolved account), `proposed` (agent trust trail), or
        `flagged` (needs a human).

        Delegates to `views.build_ledger` — the single projection the #3 UI shares,
        so JSON and HTML render the same standing per transaction. The reconcile
        stores are passed too, so every entry also carries its `reconciliation`
        fold (null when no statement was imported for the period) — the same
        `build_reconciliation` result `GET /reconcile/view` serializes, so the two
        surfaces always agree.
        """
        return await build_ledger(
            config=config,
            ledger_store=ledger_store,
            confirmation_store=confirmation_store,
            period=period,
            statement_store=statement_store,
            reconciliation_store=reconciliation_store,
        )

    # --- Slice 2: reconcile — statement import + the detection-only skill + the
    # sole human write path (a resolution) + the overlaid view. `reconcile_account`
    # is called as-is; the only reconcile write is `/reconcile/resolve`.

    @app.post(
        "/statements/import",
        response_model=StatementImportResultOut,
        summary="Import & persist statement lines",
    )
    async def import_statement(file: UploadFile = File(...)) -> StatementImportResultOut:
        """Upload a CSV/JSON bank/card statement → persist each line via the store.

        The reconcile counterpart to `/import`: format dispatched by the filename
        suffix, the store idempotent (a re-import adds no duplicate rows), a
        malformed file a 400 naming the problem — nothing partially imported.
        """
        data = await file.read()
        try:
            lines = import_statement_bytes(data, file.filename or "")
        except StatementImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Closed-period guard, the statement-side twin of `/import`: refuse the
        # whole upload if any line lands in a closed period (nothing persisted).
        closed = await closed_periods(close_store)
        offending = [
            (
                f"{line.statement_ref} {line.amount} on {line.date.date().isoformat()}",
                period_of(line.date),
            )
            for line in lines
            if period_of(line.date) in closed
        ]
        if offending:
            raise HTTPException(status_code=400, detail=closed_import_refusal(offending))

        for line in lines:
            await statement_store.store(line)

        return StatementImportResultOut(
            imported=len(lines),
            lines=[StatementLineOut.from_model(line) for line in lines],
        )

    @app.get("/statements", response_model=StatementLinesOut, summary="The stored statement lines")
    async def statements(period: str) -> StatementLinesOut:
        """The period's stored statement lines, in read order — a truth surface for
        tests/inspection (the statement side of the reconcile input)."""
        lines = await statement_store.fetch_statement(period)
        return StatementLinesOut(
            period=period,
            lines=[StatementLineOut.from_model(line) for line in lines],
        )

    @app.post(
        "/reconcile",
        response_model=ReconciliationReportOut,
        summary="Run reconcile_account → the raw report (matched/to_confirm/gaps)",
    )
    async def reconcile(period: str) -> ReconciliationReportOut:
        """Call the framework's `reconcile_account(ledger, statement, config, period)`
        **as-is** and serialize the raw report (the analog of `POST /categorize`).

        Writes nothing — detection-only (§5.5). Unlike `/reconcile/view` it does
        **not** short-circuit on an empty statement: it returns whatever the skill
        truthfully reports (an empty statement → every transaction surfaces as an
        `unmatched_on_statement` gap). The no-statement product guard lives on the
        view surface, not here.
        """
        report = await reconcile_account(ledger_store, statement_store, config, period)
        return ReconciliationReportOut.from_model(report)

    @app.post(
        "/reconcile/resolve",
        response_model=ReconcileResolutionOut,
        summary="Record one validated reconcile resolution",
    )
    async def reconcile_resolve(request: ResolveReconcileRequest) -> ReconcileResolutionOut:
        """Record one human confirm/reject/acknowledge into the reconciliation store.

        Guards, ordered per the #21 review — every **422** shape check first, and a
        both-ids-null request refused before any existence check (never `contains()`
        a null id), then the **404** existence checks:

        - **422** an unknown `decision`; both ids null (resolves nothing); a
          `confirm`/`reject` (a pair decision) missing either id; a `reject`/
          `acknowledge` with a blank required `note`.
        - **404** a supplied `transaction_id` absent from the ledger store, or a
          supplied `statement_line_id` absent from the statement store — N1
          (decided): a resolution must never dangle against nothing, so a typo'd id
          is refused rather than persisted as an orphan. Mirrors `/resolve`'s rule.

        Append-only: a correction is a new row the view collapses to last-write-wins.
        """
        decision = request.decision
        txn_id = request.transaction_id
        stmt_id = request.statement_line_id

        # --- 422 shape guards (all before any existence check) ---
        if decision not in VALID_DECISIONS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown decision {decision!r} — must be one of "
                    f"{sorted(VALID_DECISIONS)}."
                ),
            )
        # Both-ids-null first, so no `contains()` is ever called on a null id.
        if txn_id is None and stmt_id is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "a resolution must target at least one id (transaction_id "
                    "and/or statement_line_id) — both null resolves nothing."
                ),
            )
        if decision in PAIR_DECISIONS and (txn_id is None or stmt_id is None):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"decision {decision!r} resolves a to_confirm pair — both "
                    f"transaction_id and statement_line_id are required."
                ),
            )
        if decision in NOTE_REQUIRED_DECISIONS and not request.note.strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    f"decision {decision!r} requires a non-blank note recording the "
                    f"human's disposition."
                ),
            )

        # --- 409 closed-period guard (§5.7: a signed close is durable) ---
        # Refuse if *either* resolved side lands in a closed period, before the
        # existence checks. An unknown id is in no closed period, so it falls
        # through to the unchanged N1 404 below.
        closed_txn = (
            await transaction_in_closed_period(close_store, ledger_store, txn_id)
            if txn_id is not None
            else None
        )
        closed_stmt = (
            await statement_line_in_closed_period(close_store, statement_store, stmt_id)
            if stmt_id is not None
            else None
        )
        if closed_txn is not None or closed_stmt is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"the resolution targets closed period "
                    f"{(closed_txn or closed_stmt)!r} — its books are write-guarded "
                    f"and cannot be re-resolved (§5.7: a signed close is durable)."
                ),
            )

        # --- 404 existence guards (N1: never dangle against nothing) ---
        if txn_id is not None and not await ledger_store.contains(txn_id):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"transaction {txn_id!r} is not in the ledger — a resolution "
                    f"must never dangle against nothing (N1, §5-conservative)."
                ),
            )
        if stmt_id is not None and not await statement_store.contains(stmt_id):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"statement line {stmt_id!r} is not in the statement store — a "
                    f"resolution must never dangle against nothing (N1, §5-conservative)."
                ),
            )

        reconciliation = Reconciliation(
            transaction_id=txn_id,
            statement_line_id=stmt_id,
            decision=decision,
            note=request.note,
            source=SOURCE_HUMAN,
            decided_at=datetime.now(timezone.utc),
        )
        await reconciliation_store.record(reconciliation)
        return ReconcileResolutionOut.from_model(reconciliation)

    @app.get(
        "/reconcile/view",
        response_model=ReconciliationViewOut,
        summary="The overlaid reconcile view (report + resolution status per item)",
    )
    async def reconcile_view(period: str) -> ReconciliationViewOut:
        """The overlaid reconcile projection for `period` — `reconcile_account`
        overlaid with the latest human resolution per item, each carrying a status.

        Delegates to `views.build_reconciliation` — the single projection the queue
        UI (issue C) and the ledger fold also read, so all three agree. Honours the
        no-statement guard (zero lines → the explicit empty view, never all-gaps).
        """
        return await build_reconciliation(
            config=config,
            ledger_store=ledger_store,
            statement_store=statement_store,
            reconciliation_store=reconciliation_store,
            period=period,
        )

    # --- Slice 3: close review (read-only). The one shared close projection —
    # the framework `close_period` checklist over the *effective* reports, plus tax,
    # anomalies, and the app gates. Read-only: the writes (anomaly review / waive /
    # sign) are issues C + D. `build_close_review` is the single source of truth.

    @app.get(
        "/close",
        response_model=CloseReviewOut,
        response_model_exclude_none=False,
        summary="The close-review projection (framework checklist + gates + signable)",
    )
    async def close_review(period: str) -> CloseReviewOut:
        """The composed close review for `period` — read-only (`GET /close`).

        Delegates to `views.build_close_review`, the single projection `GET /ui/close`
        (issue E) also reads. Renders the framework's five-check `close_period`
        checklist verbatim over the effective reports, the period's tax + anomalies,
        the three app gates, and `signable`. An already-closed period renders its
        stored signed snapshot (not a recomputation).

        A `tax_regime` the framework does not register makes `track_tax` fail fast
        with `UnknownTaxRegime`; it is surfaced as a **400** (never swallowed into a
        200 with an empty tax) — the example config is `HST`, which registers.
        """
        try:
            review = await build_close_review(
                config=config,
                ledger_store=ledger_store,
                confirmation_store=confirmation_store,
                statement_store=statement_store,
                reconciliation_store=reconciliation_store,
                close_store=close_store,
                anomaly_review_store=anomaly_review_store,
                waiver_store=waiver_store,
                period=period,
            )
        except UnknownTaxRegime as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CloseReviewOut.from_review(review)

    # --- Slice 4 · A: the accountant-package preview (read-only). The read side of
    # the Contract A deliverable — `generate_accountant_package` over the effective
    # close (`build_package` delegates to the same `build_close_review` GET /close
    # uses), plus the app's confirmation overlay. No exporter / write path here.

    @app.get(
        "/package",
        response_model=PackageOut,
        response_model_exclude_none=False,
        summary="The accountant-package preview projection (proposed | blocked)",
    )
    async def package(period: str) -> PackageOut:
        """The accountant-package preview for `period` — proposed (assembled) or blocked.

        Delegates to `views.build_package`, the single projection `GET /ui/package`
        (a later issue) will also read. A PROPOSED package (the effective close was
        READY) is a **200** carrying the full trail; a BLOCKED package (the effective
        close was not READY, or the period is already closed) is also a **200** with a
        non-null `unmet_close` — a blocked package is the honest answer, not an error.

        A `tax_regime` the framework does not register makes `track_tax` fail fast with
        `UnknownTaxRegime`; it is surfaced as a **400** (mirrors `GET /close`), never
        swallowed — the example config is `HST`, which registers.
        """
        try:
            return await build_package(
                config=config,
                ledger_store=ledger_store,
                confirmation_store=confirmation_store,
                statement_store=statement_store,
                reconciliation_store=reconciliation_store,
                close_store=close_store,
                anomaly_review_store=anomaly_review_store,
                waiver_store=waiver_store,
                period=period,
            )
        except UnknownTaxRegime as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- Slice 4 · B: the export write path. `POST /export` re-obtains the close
    # server-side through the **same** `build_package` projection `GET /package`
    # reads (never trusting client-supplied or previously-previewed state), gates on
    # the freshly-rebuilt status, and only on PROPOSED writes a fresh export folder +
    # one append-only log row. A BLOCKED rebuild is a 409 quoting `unmet_close` with
    # nothing written. `GET /exports` lists the log in order. No transmission of any
    # kind — local files + the local-browser download (a later UI issue) only.

    @app.post(
        "/export",
        response_model=ExportResultOut,
        summary="Export the accountant package to local files (§5.4)",
    )
    async def export(period: str) -> ExportResultOut:
        """Export `period`'s accountant package — the §5.4 write path (`POST /export`).

        The server **re-obtains the close and rebuilds the package from its own
        stores** at request time (via `build_package`, the same projection
        `GET /package` reads): it never trusts client-supplied state or a previously
        previewed package. Then:

        - The export directory unwired → **503** (never a silent no-op).
        - The rebuilt package is **not PROPOSED** (`status != "proposed"` — a BLOCKED
          close, or an already-closed period) → **409** quoting `unmet_close`
          verbatim, and **nothing is written** (no folder, no log row). Because
          `build_package` only reads its stores and runs the pure skills, the refusal
          path is read-only.
        - PROPOSED → write the fresh `exports/<export_id>/` folder (the four Core
          files), append **exactly one** log row, and echo the `ExportResultOut`.

        A `tax_regime` the framework does not register makes the rebuild fail fast with
        `UnknownTaxRegime`, surfaced as a **400** (mirrors `GET /package`).
        """
        if export_dir is None or export_store is None:
            raise HTTPException(
                status_code=503,
                detail="the export directory is not configured on this server.",
            )

        # Re-obtain + rebuild the package server-side — never trust client state.
        try:
            package = await build_package(
                config=config,
                ledger_store=ledger_store,
                confirmation_store=confirmation_store,
                statement_store=statement_store,
                reconciliation_store=reconciliation_store,
                close_store=close_store,
                anomaly_review_store=anomaly_review_store,
                waiver_store=waiver_store,
                period=period,
            )
        except UnknownTaxRegime as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # A non-PROPOSED rebuild refuses, writing nothing (no folder, no log row).
        if package.status != PackageStatus.PROPOSED.value:
            raise HTTPException(status_code=409, detail=package.unmet_close)

        # PROPOSED → the sole write path: a fresh folder + one appended log row.
        from bookkeeper_ui import __version__  # local: avoids the __init__↔api cycle

        record = export_package(
            package=package,
            config=config,
            export_dir=Path(export_dir),
            exported_at=datetime.now(timezone.utc),
            app_version=__version__,
        )
        await export_store.record(record)
        return ExportResultOut.from_record(record)

    @app.get(
        "/exports",
        response_model=list[ExportRecordOut],
        summary="The export log (append-only, in export order)",
    )
    async def exports() -> list[ExportRecordOut]:
        """The export log for this app — every export in export (insertion) order.

        Reads `exports.jsonl` through the store's `all()` (no writes). The export
        directory unwired → **503** (mirrors `POST /export`).
        """
        if export_store is None:
            raise HTTPException(
                status_code=503,
                detail="the export directory is not configured on this server.",
            )
        return [ExportRecordOut.from_record(record) for record in await export_store.all()]

    # --- Slice 3: the thin write endpoints (issue C). Acknowledge an anomaly flag
    # and waive a no-statement period — the two small writes that feed close review's
    # gate B (anomalies-reviewed) and gate C (statement-or-waiver). Each writes only
    # its own Slice-3 store's row (never the ledger / statement it is dispositioning),
    # append-only. These are machine 4xx (422/409); the human-reachable UI twins that
    # render 2xx partials are issue E. `flag_anomaly` is called as-is (read-only).

    @app.post(
        "/anomalies/review",
        response_model=AnomalyReviewOut,
        summary="Acknowledge one current anomaly flag",
    )
    async def anomalies_review(request: AnomalyReviewRequest) -> AnomalyReviewOut:
        """Record one human acknowledgment of a `flag_anomaly` flag into the store.

        `flag_anomaly` is advisory — it surfaces mechanical anomalies but gates
        nothing and writes nothing; this is the human acknowledgment layered on top,
        which close review's gate B ("all anomalies reviewed") reads. Two guards, both
        write nothing on rejection:

        - **409** the period is closed (`close_store` truth) — a signed close is
          write-guarded (§5.7), so its anomaly dispositions are frozen too. Checked
          first, the period-level write guard (mirrors `/resolve`'s closed guard
          preceding its existence check).
        - **422** `flag_id` matches no **current** flag for the period: re-run
          `flag_anomaly` (read-only, as-is) and derive each flag's id with A's exact
          recipe (`derive_flag_id`, imported — never re-implemented), then reject an id
          not in that set. Never acknowledge a flag that does not exist / fabricate
          one. A `flag_id` derived from a *stale* flag (e.g. one whose over-materiality
          reason changed after a `materiality_floor` change) derives a new id, is not
          current, and so is refused here.

        On success append exactly one row (append-only: a re-ack is a new row the
        store collapses to last-write-wins) and echo it back.
        """
        if anomaly_review_store is None:
            raise HTTPException(
                status_code=503,
                detail="the anomaly-review store is not configured on this server.",
            )

        if request.period in await closed_periods(close_store):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"period {request.period!r} is closed — its books are "
                    f"write-guarded and its anomaly dispositions cannot be changed "
                    f"(§5.7: a signed close is durable)."
                ),
            )

        # The current flag set for the period — `flag_anomaly` called as-is
        # (read-only), keyed by the app-derived id (A's exact recipe, imported).
        report = await flag_anomaly(ledger_store, config, request.period)
        flags_by_id = {derive_flag_id(flag): flag for flag in report.flags}
        flag = flags_by_id.get(request.flag_id)
        if flag is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"flag_id {request.flag_id!r} matches no current anomaly flag for "
                    f"period {request.period!r} — an acknowledgment must never dangle "
                    f"against a flag that does not exist (a changed flag derives a new "
                    f"id, so a stale acknowledgment is never inherited)."
                ),
            )

        review = AnomalyReview(
            flag_id=request.flag_id,
            kind=flag.kind.value,
            reason=flag.reason,
            transaction_ids=tuple(transaction_key(t) for t in flag.transactions),
            note=request.note,
            acknowledged_at=datetime.now(timezone.utc),
            source=SOURCE_HUMAN,
        )
        await anomaly_review_store.record(review)
        return AnomalyReviewOut.from_model(review)

    @app.post(
        "/reconciliation/waive",
        response_model=WaiverOut,
        summary="Waive reconciliation for a no-statement period",
    )
    async def reconciliation_waive(request: WaiveRequest) -> WaiverOut:
        """Record one human waiver of a period's reconciliation precondition.

        Close review's gate C is met by a statement to reconcile against **or** a
        recorded waiver; this is that waiver — a dated, attributable decision to sign
        the close despite no feed. Two guards, both write nothing on rejection:

        - **409** the period is closed (`close_store` truth) — a signed close is
          write-guarded (§5.7). Checked first, the period-level write guard.
        - **409** statement lines exist for the period
          (`statement_store.fetch_statement` non-empty) — a present statement is never
          waivable: reconcile it, do not waive it.

        On success append exactly one row (append-only: a re-waiver is a new row the
        store collapses to last-write-wins) and echo it back. `waived_by` defaults to
        ``"owner"`` (single-user local, a label not an identity).
        """
        if waiver_store is None:
            raise HTTPException(
                status_code=503,
                detail="the waiver store is not configured on this server.",
            )

        if request.period in await closed_periods(close_store):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"period {request.period!r} is closed — its books are "
                    f"write-guarded and cannot be waived (§5.7: a signed close is "
                    f"durable)."
                ),
            )

        if await statement_store.fetch_statement(request.period):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"period {request.period!r} has a statement on file — a present "
                    f"statement is never waivable; reconcile it rather than waiving it."
                ),
            )

        waiver = Waiver(
            period=request.period,
            waived_at=datetime.now(timezone.utc),
            waived_by=request.waived_by or "owner",
            note=request.note,
        )
        await waiver_store.record(waiver)
        return WaiverOut.from_model(waiver)

    # --- Slice 3: the SIGN action (issue D). The §5.7 human sign-off — re-verify the
    # whole close server-side, then append exactly one durable, self-contained close
    # record. The correctness core: the #14 immutability lesson (capture, never
    # re-derive) + the sign gates live here. JSON route (machine 4xx); the UI twin is
    # issue E. The sole write on the pass path is one `CloseRecord`; on every refusal
    # nothing is written.

    @app.post(
        "/sign",
        response_model=CloseRecordOut,
        summary="Sign a period closed — re-verify + write the durable close record",
    )
    async def sign(request: SignRequest) -> CloseRecordOut:
        """Sign `period` closed (§5.7) — the durable, append-only close record.

        The handler order is load-bearing (issue D):

        1. **Period precondition (before any composition).** `period` is
           client-supplied free text: it MUST be a well-formed quarterly ``YYYY-Qn``
           label (the `period_of` convention — else **400**) **and** carry ≥1 ledger
           transaction (else **409**). This runs first because with
           `prior_period_state` unset the framework calls any label READY — so signing
           a garbage/empty label would append a close under a label the D4
           effective-prior read cannot order, fail-safe-BLOCKing every future close.
        2. **Closed-period guard (before trusting the composition).** An already-closed
           period → **409** (never re-signed, never a second close row). Checked before
           the composition because `build_close_review` returns the *stored snapshot*
           for a closed period, which would otherwise look like a signable READY.
        3. **In-handler re-verification (trust no client state).** Recompute the whole
           close via the **same** `build_close_review` the review screen uses and
           re-check every gate server-side — framework READY **and** all three app
           gates (all-confirmed / anomalies-reviewed / statement-or-waiver). Any gate
           unmet → **409** whose body is the `CloseReviewOut` enumerating what failed.
           Signing at/before the effective prior close is refused by the framework's
           own `period_closeable` (via the D4 effective prior) — never re-implemented.
        4. **On pass — the sole write.** Append **exactly one** `CloseRecord` and echo
           it. Nothing else is written (the D4 in-memory `dataclasses.replace` is not a
           file write); an unregistered `tax_regime` surfaces the framework error 400.
        """
        if close_store is None:
            raise HTTPException(
                status_code=503,
                detail="the close store is not configured on this server.",
            )

        # 1. Period precondition — before any composition or write.
        if not is_quarterly_period(request.period):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"period {request.period!r} is not a well-formed quarterly label "
                    f"(YYYY-Qn, n 1–4 — the period_of convention the books are filed "
                    f"by). A close is never signed under a label the prior-period "
                    f"guard cannot order."
                ),
            )
        if not await ledger_store.fetch_for_period(request.period):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"period {request.period!r} has no ledger transactions — there is "
                    f"nothing to close. Import and confirm the period's transactions "
                    f"before signing."
                ),
            )

        # 2. Closed-period guard — before trusting the composition (a closed period's
        # review returns the stored snapshot, not a fresh gate evaluation).
        if request.period in await closed_periods(close_store):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"period {request.period!r} is already closed — a signed close is "
                    f"durable and is never re-signed (§5.7). No second close record is "
                    f"written."
                ),
            )

        # 3. In-handler re-verification — the same projection the review screen reads.
        try:
            review = await build_close_review(
                config=config,
                ledger_store=ledger_store,
                confirmation_store=confirmation_store,
                statement_store=statement_store,
                reconciliation_store=reconciliation_store,
                close_store=close_store,
                anomaly_review_store=anomaly_review_store,
                waiver_store=waiver_store,
                period=request.period,
            )
        except UnknownTaxRegime as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not review.signable:
            # 409 enumerating exactly which gates failed — the same CloseReviewOut the
            # review renders (money already exact strings; mode="json" keeps them so).
            raise HTTPException(
                status_code=409,
                detail=CloseReviewOut.from_review(review).model_dump(mode="json"),
            )

        # 4. On pass — append exactly one self-contained close record and echo it.
        signed_by = (request.signed_by or "").strip() or "owner"
        record = await build_close_record(
            review=review,
            waiver_store=waiver_store,
            signed_by=signed_by,
            signed_at=datetime.now(timezone.utc),
        )
        await close_store.record(record)
        return CloseRecordOut.from_record(record)

    # --- Slice 5 · A: the intake port — the machine-facing half of receipt capture.
    # An extractor POSTs a candidate (its extracted fields + its source artifact); a
    # candidate can never touch the ledger — only a human confirm constructs a
    # `Transaction`. The stores are the *only* thing these routes write through; when
    # any is unwired the surface refuses a 503 rather than silently no-op.

    def _require_intake() -> tuple[
        FileCandidateStore, FileCandidateDecisionStore, FileArtifactStore
    ]:
        """The three intake stores, or a 503 if the port is not configured."""
        if (
            candidate_store is None
            or candidate_decision_store is None
            or artifact_store is None
        ):
            raise HTTPException(
                status_code=503,
                detail=(
                    "the intake port is not configured on this app — no candidate / "
                    "decision / artifact store is wired."
                ),
            )
        return candidate_store, candidate_decision_store, artifact_store

    @app.post(
        "/intake/candidates",
        response_model=CandidateSubmitOut,
        status_code=201,
        summary="Submit a candidate transaction (with its source artifact)",
    )
    async def submit_candidate(
        request: CandidateSubmitRequest, response: Response
    ) -> CandidateSubmitOut:
        """Accept one candidate document → persist the submission row + its raw artifact.

        Server-validates every field (an invariant, not courtesy): non-blank
        `source`/`submission_id`/`vendor`, finite-`Decimal` money as strings (a JSON
        number is a 422; `NaN`/`Infinity` rejected), an ISO date, a base64 artifact
        that decodes non-empty and ≤ the size cap, an allowlisted media type — any
        failure a 422 naming the field, nothing written. **Idempotent** on the
        `(source, submission_id)` identity: a re-submission is a no-op returning 200
        with the *existing* candidate and `duplicate: true` (first write wins; a
        differing re-POST never mutates the stored row); a first write is a 201. A
        submission is a machine write on the **proposal** side — it never touches the
        ledger.
        """
        candidates, _decisions, artifacts = _require_intake()

        source = _require_nonblank(request.source, "source")
        submission_id = _require_nonblank(request.submission_id, "submission_id")
        vendor = _require_nonblank(request.vendor, "vendor")
        amount = _parse_money(request.amount, "amount")
        tax = (
            _parse_money(request.tax, "tax")
            if request.tax not in (None, "")
            else Decimal("0")
        )
        date = _parse_iso_datetime(request.date, "date")
        received_at = (
            _parse_iso_datetime(request.received_at, "received_at")
            if request.received_at not in (None, "")
            else None
        )

        if request.artifact_media_type not in ALLOWED_ARTIFACT_MEDIA_TYPES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"artifact_media_type {request.artifact_media_type!r} is not "
                    f"allowed — one of {sorted(ALLOWED_ARTIFACT_MEDIA_TYPES)}."
                ),
            )
        try:
            artifact_bytes = base64.b64decode(request.artifact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="artifact is not valid base64."
            ) from exc
        if not artifact_bytes:
            raise HTTPException(
                status_code=422, detail="artifact decoded to empty bytes — an artifact is required."
            )
        if len(artifact_bytes) > artifact_cap:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"artifact is {len(artifact_bytes)} bytes — over the "
                    f"{artifact_cap}-byte cap."
                ),
            )

        cid = compute_candidate_id(source, submission_id)
        existing = await candidates.get(cid)
        if existing is not None:
            # Idempotent no-op: the identity is already on record (first write wins).
            response.status_code = 200
            return CandidateSubmitOut(
                duplicate=True, candidate=CandidateOut.from_submission(existing)
            )

        submission = CandidateSubmission(
            candidate_id=cid,
            source=source,
            submission_id=submission_id,
            vendor=vendor,
            amount=amount,
            tax=tax,
            date=date,
            description=request.description or "",
            attribution_target_id=request.attribution_target_id,
            source_hint=request.source_hint or "",
            received_at=received_at,
            artifact_media_type=request.artifact_media_type,
            artifact_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
            submitted_at=datetime.now(timezone.utc),
        )
        # Artifact first (its sha256 is already on the row), then the submission row —
        # the row is the index, so a crash between the two leaves an orphan blob, never
        # a row pointing at missing bytes.
        await artifacts.put(cid, artifact_bytes)
        await candidates.add(submission)
        response.status_code = 201
        return CandidateSubmitOut(
            duplicate=False, candidate=CandidateOut.from_submission(submission)
        )

    @app.get(
        "/intake/candidates",
        response_model=IntakeQueueOut,
        summary="List candidates (the shared intake queue)",
    )
    async def list_candidates(status: str | None = None) -> IntakeQueueOut:
        """The intake queue via the shared `build_intake_queue` projection.

        `?status=pending|confirmed|rejected` filters to that standing; no filter
        returns **all** statuses (the one projection both this route and the later UI
        queue read, so JSON and HTML never disagree). An unknown `status` value is a
        422. Money is echoed as exact strings.
        """
        candidates, decisions, _artifacts = _require_intake()
        if status is not None and status not in ("pending", "confirmed", "rejected"):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"status {status!r} must be one of pending|confirmed|rejected "
                    f"(omit it for all statuses)."
                ),
            )
        return await build_intake_queue(
            candidate_store=candidates,
            candidate_decision_store=decisions,
            status=status,  # type: ignore[arg-type]
        )

    @app.get(
        "/intake/artifact/{candidate_id}",
        summary="Fetch a candidate's raw source artifact",
    )
    async def get_artifact(candidate_id: str) -> Response:
        """Serve a candidate's stored raw artifact bytes with its declared media type.

        The `artifact_sha256` on the candidate row is the integrity/trace link; this
        route hands back the exact bytes submitted. An unknown candidate id, or a
        candidate whose bytes are missing on disk, is a 404.
        """
        candidates, _decisions, artifacts = _require_intake()
        submission = await candidates.get(candidate_id)
        if submission is None:
            raise HTTPException(
                status_code=404, detail=f"no candidate {candidate_id!r} on record."
            )
        data = await artifacts.get(candidate_id)
        if data is None:
            raise HTTPException(
                status_code=404,
                detail=f"no artifact on disk for candidate {candidate_id!r}.",
            )
        return Response(content=data, media_type=submission.artifact_media_type)

    @app.post(
        "/intake/resolve",
        response_model=CandidateResolutionOut,
        summary="Confirm/reject a candidate (the review gate before the ledger)",
    )
    async def resolve_candidate(request: ResolveCandidateRequest) -> CandidateResolutionOut:
        """Gate a candidate: a human **confirm** constructs and files a ledger
        `Transaction`; a **reject** records the decision and leaves the ledger untouched.

        Errors: **404** an unknown `candidate_id`; **409** an already-decided candidate
        (its recorded outcome returned — re-opening is out of scope); **422** a bad
        confirmed field. On confirm the human's edits go through the same gate the
        submission did (finite-Decimal money, ISO date, non-blank vendor), the
        `attribution_target_id` must be one of `config.attribution_targets` (§ the
        resolver never invents a target), and the **edited** date is checked against the
        closed periods (`closed_periods` + `period_of`) — a date in a signed-closed
        period is a **409** with no ledger write and no decision row. Honest dedupe: the
        ledger `store()` is idempotent and silent, so this probes `contains()` first and
        records `ledger_outcome` — a duplicate confirm no-ops the ledger but says so
        (`already-present`), never a silent lost filing.
        """
        candidates, decisions, artifacts = _require_intake()

        cid = request.candidate_id
        submission = await candidates.get(cid)
        if submission is None:
            raise HTTPException(
                status_code=404, detail=f"no candidate {cid!r} — nothing to resolve."
            )

        prior = (await decisions.latest_by_candidate()).get(cid)
        if prior is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": (
                        f"candidate {cid!r} is already {prior.action}ed — re-opening a "
                        f"decided candidate is out of scope."
                    ),
                    "candidate_id": cid,
                    "action": prior.action,
                    "ledger_outcome": prior.ledger_outcome,
                    "reject_reason": prior.reject_reason,
                },
            )

        now = datetime.now(timezone.utc)

        if request.action == ACTION_REJECT:
            decision = CandidateDecision(
                candidate_id=cid,
                action=ACTION_REJECT,
                source=CANDIDATE_SOURCE_HUMAN,
                decided_at=now,
                reject_reason=request.reject_reason,
            )
            await decisions.record(decision)
            return CandidateResolutionOut(
                candidate_id=cid,
                action=ACTION_REJECT,
                standing="rejected",
                reject_reason=request.reject_reason,
                decided_at=now.isoformat(),
                message=(
                    "Candidate rejected — the ledger is untouched; the submission row "
                    "and its artifact remain on disk (append-only audit trail)."
                ),
            )

        # CONFIRM — every effective field value (the human's edit, else the
        # candidate's own value) re-validated through the submission's gate.
        vendor = _require_nonblank(
            request.vendor if request.vendor is not None else submission.vendor, "vendor"
        )
        amount = (
            _parse_money(request.amount, "amount")
            if request.amount is not None
            else submission.amount
        )
        tax = (
            _parse_money(request.tax, "tax") if request.tax is not None else submission.tax
        )
        date = (
            _parse_iso_datetime(request.date, "date")
            if request.date is not None
            else submission.date
        )
        description = (
            request.description if request.description is not None else submission.description
        )
        target = (
            request.attribution_target_id
            if request.attribution_target_id is not None
            else submission.attribution_target_id
        )
        if target is None or target not in config.attribution_targets:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"attribution_target_id {target!r} must be one of "
                    f"config.attribution_targets — the resolver never invents a target."
                ),
            )

        # C1 closed-guard — on the **human-edited** date being written (the date can
        # change in the form). `closed_periods` + `period_of`, never
        # `transaction_in_closed_period` (which silently passes here — the txn is not
        # in the ledger yet). Refused → 409, no ledger write, no decision row.
        if period_of(date) in await closed_periods(close_store):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"the edited date {date.date().isoformat()} falls in closed period "
                    f"{period_of(date)!r} — its books are write-guarded (§5.7: a signed "
                    f"close is durable). No transaction was filed."
                ),
            )

        artifact_bytes = await artifacts.get(cid) or b""
        transaction = Transaction(
            attribution_target_id=target,
            vendor=vendor,
            amount=amount,
            tax=tax,
            date=date,
            description=description,
            artifact_bytes=artifact_bytes,
        )
        key = transaction_key(transaction)
        already = await ledger_store.contains(key)  # probe BEFORE the idempotent store
        await ledger_store.store(transaction)
        outcome = LEDGER_OUTCOME_ALREADY_PRESENT if already else LEDGER_OUTCOME_STORED

        decision = CandidateDecision(
            candidate_id=cid,
            action=ACTION_CONFIRM,
            source=CANDIDATE_SOURCE_HUMAN,
            decided_at=now,
            vendor=vendor,
            amount=amount,
            tax=tax,
            date=date,
            description=description,
            attribution_target_id=target,
            transaction_key=key,
            ledger_outcome=outcome,
        )
        await decisions.record(decision)
        message = (
            "Confirmed — this transaction is already in the ledger (a duplicate of an "
            "existing filing); no new row was written."
            if already
            else "Confirmed and filed to the ledger."
        )
        return CandidateResolutionOut(
            candidate_id=cid,
            action=ACTION_CONFIRM,
            standing="confirmed",
            ledger_outcome=outcome,
            transaction_key=key,
            decided_at=now.isoformat(),
            message=message,
        )

    # --- Slice 5 · A3: the offline drop-directory intake — a second front door onto the
    # A1 validate/store path. An extractor that cannot POST drops candidate `*.json` files
    # into the drop dir; this on-demand scan ingests them (no watcher, no poller). Enabled
    # iff `intake_drop_dir is not None` — unwired the route 503s (the export-route
    # precedent), never a silent no-op.

    @app.post(
        "/intake/scan",
        response_model=ScanResultOut,
        summary="Scan the drop directory for candidate documents (offline intake)",
    )
    async def scan_intake_drop() -> ScanResultOut:
        """Scan the drop dir → ingest each valid candidate document through the A1 path.

        The offline / push-can't-reach intake mode (Slice 5 · A3): a script or scanner
        that cannot POST drops candidate `*.json` files into the drop dir; this on-demand
        scan ingests each through the *same* validate + store path `POST /intake/candidates`
        uses. **Idempotent** — ingest rides the store's `candidate_id` dedupe, so a second
        scan over the unchanged dir writes nothing new (every file a `duplicate`). One
        malformed file is reported in `errors` **without** aborting the scan or blocking the
        valid files (AC 11). The drop feature is enabled iff a drop dir is wired — unwired →
        a **503** (never a silent no-op), mirroring the export routes.
        """
        if intake_drop_dir is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "the intake drop directory is not configured on this app — the "
                    "offline drop-scan mode is disabled."
                ),
            )
        candidates, _decisions, artifacts = _require_intake()
        summary = await scan_drop_dir(
            drop_dir=intake_drop_dir,
            candidate_store=candidates,
            artifact_store=artifacts,
            max_artifact_bytes=artifact_cap,
        )
        return ScanResultOut(
            scanned=summary.scanned,
            ingested=summary.ingested,
            duplicates=summary.duplicates,
            errors=[ScanFileErrorOut(file=e.file, error=e.error) for e in summary.errors],
        )

    # #3 — the thin UI (Jinja + htmx) over these same stores, mounted on this app
    # so `uvicorn bookkeeper_ui.api:build_app_from_env --factory` serves both the
    # JSON API (root paths) and the HTML surface (GET / and the /ui/* routes).
    register_ui(
        app,
        config=config,
        ledger_store=ledger_store,
        confirmation_store=confirmation_store,
        statement_store=statement_store,
        reconciliation_store=reconciliation_store,
        close_store=close_store,
        anomaly_review_store=anomaly_review_store,
        waiver_store=waiver_store,
        export_dir=export_dir,
        export_store=export_store,
        candidate_store=candidate_store,
        candidate_decision_store=candidate_decision_store,
        artifact_store=artifact_store,
        intake_drop_dir=intake_drop_dir,
        max_artifact_bytes=artifact_cap,
        attribution_target_labels=attribution_target_labels,
    )

    return app


def build_app_from_env() -> FastAPI:
    """Build the app from env vars — the runnable default for local serving.

    ``uvicorn bookkeeper_ui.api:build_app_from_env --factory``

    - ``BOOKKEEPER_UI_CONFIG``   — path to the `BookkeeperConfig` JSON
                                   (default ``examples/config.json``).
    - ``BOOKKEEPER_UI_DATA_DIR`` — directory for the ledger + statement +
                                   confirmation + reconciliation + close + anomaly
                                   review + waiver files (default ``data``); each
                                   created on first write.
    - ``BOOKKEEPER_UI_EXPORT_DIR`` — directory the export write path writes to (the
                                   per-export folders + the `exports.jsonl` log);
                                   default ``<data_dir>/exports``.
    - ``BOOKKEEPER_UI_MAX_ARTIFACT_BYTES`` — cap on a submitted intake artifact's
                                   decoded size (default ``DEFAULT_MAX_ARTIFACT_BYTES``,
                                   10 MiB); a larger artifact is a 422.
    - ``BOOKKEEPER_UI_INTAKE_DROP_DIR`` — the offline drop directory `POST /intake/scan`
                                   ingests candidate `*.json` files from; default
                                   ``<data_dir>/intake_drop`` (so the running app always
                                   has the drop-scan mode enabled).

    The wiring is deliberately thin: #3 (the UI) owns the real run surface. This
    exists so the API is runnable on its own for local development and the tests
    exercise `create_app` directly with injected temp paths. The files
    (`ledger.jsonl` / `statements.jsonl` / `confirmations.jsonl` /
    `reconciliations.jsonl` / `closes.jsonl` / `anomaly_reviews.jsonl` /
    `reconciliation_waivers.jsonl` / `candidates.jsonl` / `candidate_decisions.jsonl`
    + the `artifacts/` blob dir) stay distinct — a resolution, a close, or a
    candidate decision never touches the ledger or the statement it snapshots. This
    is the construction site for the Slice-3 **and** Slice-5 stores (not
    `create_app`, which takes them injected).
    """
    config_path = os.environ.get("BOOKKEEPER_UI_CONFIG", "examples/config.json")
    data_dir = Path(os.environ.get("BOOKKEEPER_UI_DATA_DIR", "data"))
    export_dir = Path(os.environ.get("BOOKKEEPER_UI_EXPORT_DIR", str(data_dir / "exports")))
    intake_drop_dir = Path(
        os.environ.get("BOOKKEEPER_UI_INTAKE_DROP_DIR", str(data_dir / "intake_drop"))
    )
    max_artifact_bytes = int(
        os.environ.get("BOOKKEEPER_UI_MAX_ARTIFACT_BYTES", str(DEFAULT_MAX_ARTIFACT_BYTES))
    )
    # The intake `<select>` label map — read app-side from the SAME config JSON
    # `load_config` reads (a new read *beside* it, not a change to it), since the
    # framework `BookkeeperConfig` has no such field and `from_mapping` would drop it.
    # Absent → an empty map, so the `<select>` renders the raw attribution ids.
    raw_config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    attribution_target_labels = raw_config.get("attribution_target_labels") or {}
    return create_app(
        config=load_config(config_path),
        ledger_store=FileLedgerStore(data_dir / "ledger.jsonl"),
        confirmation_store=FileConfirmationStore(data_dir / "confirmations.jsonl"),
        statement_store=FileStatementStore(data_dir / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(data_dir / "reconciliations.jsonl"),
        close_store=FileCloseStore(data_dir / "closes.jsonl"),
        anomaly_review_store=FileAnomalyReviewStore(data_dir / "anomaly_reviews.jsonl"),
        waiver_store=FileWaiverStore(data_dir / "reconciliation_waivers.jsonl"),
        export_dir=export_dir,
        candidate_store=FileCandidateStore(data_dir / "candidates.jsonl"),
        candidate_decision_store=FileCandidateDecisionStore(
            data_dir / "candidate_decisions.jsonl"
        ),
        artifact_store=FileArtifactStore(data_dir / "artifacts"),
        intake_drop_dir=intake_drop_dir,
        max_artifact_bytes=max_artifact_bytes,
        attribution_target_labels=attribution_target_labels,
    )
