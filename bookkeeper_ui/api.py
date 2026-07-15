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

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.categorize import categorize
from bookkeeper.skills.flag_anomaly import flag_anomaly
from bookkeeper.skills.reconcile import reconcile_account
from bookkeeper.skills.track_tax import UnknownTaxRegime

from bookkeeper_ui.anomaly_reviews import (
    AnomalyReview,
    FileAnomalyReviewStore,
    derive_flag_id,
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
from bookkeeper_ui.importer import TransactionImportError, import_bytes
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
    CategorizationReportOut,
    CloseRecordOut,
    CloseReviewOut,
    ConfirmationOut,
    ImportResultOut,
    LedgerOut,
    PackageOut,
    ReconcileResolutionOut,
    ReconciliationReportOut,
    ReconciliationViewOut,
    ResolveReconcileRequest,
    ResolveRequest,
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
    build_ledger,
    build_package,
    build_reconciliation,
)
from bookkeeper_ui.waivers import FileWaiverStore, Waiver
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
    `close_store` is the single source of closed-period truth every write-path guard
    probes (an unset store means no period is closed, so the guards are inert and the
    behaviour is exactly pre-Slice-3). The `anomaly_review_store` / `waiver_store` are
    the append-only targets of the two Slice-3 write endpoints (`/anomalies/review` /
    `/reconciliation/waive`); each endpoint refuses a **503** if its store is unwired,
    so a Slice-1/2 app never silently no-ops a write.
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
