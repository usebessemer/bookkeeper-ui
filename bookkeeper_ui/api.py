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
  human resolutions), plus tax, anomalies, and the app gates. Read-only; the writes
  (anomaly review / waive / sign) are later Slice-3 issues.

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

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.categorize import categorize
from bookkeeper.skills.reconcile import reconcile_account
from bookkeeper.skills.track_tax import UnknownTaxRegime

from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore
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
from bookkeeper_ui.importer import TransactionImportError, import_bytes
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.periods import period_of
from bookkeeper_ui.reconciliations import (
    NOTE_REQUIRED_DECISIONS,
    PAIR_DECISIONS,
    VALID_DECISIONS,
    FileReconciliationStore,
    Reconciliation,
)
from bookkeeper_ui.schemas import (
    CategorizationReportOut,
    CloseReviewOut,
    ConfirmationOut,
    ImportResultOut,
    LedgerOut,
    ReconcileResolutionOut,
    ReconciliationReportOut,
    ReconciliationViewOut,
    ResolveReconcileRequest,
    ResolveRequest,
    StatementImportResultOut,
    StatementLineOut,
    StatementLinesOut,
    TransactionOut,
)
from bookkeeper_ui.statement_importer import StatementImportError
from bookkeeper_ui.statement_importer import import_bytes as import_statement_bytes
from bookkeeper_ui.statement_store import FileStatementStore
from bookkeeper_ui.views import build_close_review, build_ledger, build_reconciliation
from bookkeeper_ui.waivers import FileWaiverStore
from bookkeeper_ui.web import register_ui


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
    In this issue only `close_store` is read: it is the single source of
    closed-period truth the write-path guards below probe (an unset store means no
    period is closed, so the guards are inert and behaviour is exactly pre-Slice-3).
    The anomaly/waiver stores are threaded now for issues B–E.
    """
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

    The wiring is deliberately thin: #3 (the UI) owns the real run surface. This
    exists so the API is runnable on its own for local development and the tests
    exercise `create_app` directly with injected temp paths. The files
    (`ledger.jsonl` / `statements.jsonl` / `confirmations.jsonl` /
    `reconciliations.jsonl` / `closes.jsonl` / `anomaly_reviews.jsonl` /
    `reconciliation_waivers.jsonl`) stay distinct — a resolution or a close never
    touches the ledger or the statement it snapshots. This is the construction
    site for the three Slice-3 stores (not `create_app`, which takes them injected).
    """
    config_path = os.environ.get("BOOKKEEPER_UI_CONFIG", "examples/config.json")
    data_dir = Path(os.environ.get("BOOKKEEPER_UI_DATA_DIR", "data"))
    return create_app(
        config=load_config(config_path),
        ledger_store=FileLedgerStore(data_dir / "ledger.jsonl"),
        confirmation_store=FileConfirmationStore(data_dir / "confirmations.jsonl"),
        statement_store=FileStatementStore(data_dir / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(data_dir / "reconciliations.jsonl"),
        close_store=FileCloseStore(data_dir / "closes.jsonl"),
        anomaly_review_store=FileAnomalyReviewStore(data_dir / "anomaly_reviews.jsonl"),
        waiver_store=FileWaiverStore(data_dir / "reconciliation_waivers.jsonl"),
    )
