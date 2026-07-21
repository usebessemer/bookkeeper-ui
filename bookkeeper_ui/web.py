"""The thin UI (#3) — Jinja templates + htmx, mounted on the same FastAPI app.

Slice 1 (categorize) and Slice 2 (reconcile) screens over the #2 stores,
server-rendered, **no Node/build step**:

- ``GET  /``            — **import**: upload a transactions CSV/JSON *and* a
  statement CSV/JSON, pick the period to review.
- ``GET  /ui/queue``    — the **confirm queue** (Slice 1 core): one card per
  proposal rendering the full **trust trail** (proposed account · confidence ·
  the rule that fired), plus flagged transactions with their reason. Confirm /
  Pick-another post to ``/ui/resolve``; htmx swaps the resolved card out.
- ``GET  /ui/reconcile`` — the **reconcile queue** (Slice 2 core): the overlaid
  ``build_reconciliation`` projection rendered as to-confirm cards (both sides +
  vendor similarity + the report reason), gap cards (grouped by kind, with the
  signed delta on an amount mismatch), a read-only matched trail, and a resolved
  audit trail. Confirm / Reject / Acknowledge post to ``/ui/reconcile/resolve``;
  htmx swaps the resolved card out.
- ``GET  /ui/ledger``   — the **categorized ledger**: the confirmed transactions
  with their accounts and their Slice-2 reconciliation standing, plus the count
  still pending and a reconcile summary line.

The HTML surface is deliberately separate from the JSON API (which keeps its root
paths, `/import` … `/reconcile/view`, so #2's clients and tests are untouched):
the pages live at ``/`` and under ``/ui/*``, and both read through the *same*
stores and the *same* `views.build_ledger` / `views.build_reconciliation`
projections the JSON routes return. htmx is vendored under ``static/`` (no CDN
dependency — this runs local and offline).

The UI import/resolve handlers render an **error into the page** (a 200 partial
the user reads) rather than a JSON 4xx: on this surface the message is for a
human, and htmx swaps a 2xx body in place. The JSON API still returns the machine
4xx. The exceptions are the resolve states **unreachable** from the rendered
queue — an off-chart account / an unknown decision or id-shape (a defensive
**422**, mirroring the API's §5.2 / issue-B guards) and an unknown transaction /
statement id (a strict **404**, mirroring the API's N1 guard: a resolution must
never dangle against nothing). Those stay machine 4xx because a human at the
screen can reach none of them — the same convention #21 set for `/ui/resolve`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.flag_anomaly import flag_anomaly
from bookkeeper.skills.generate_package import PackageStatus
from bookkeeper.skills.track_tax import UnknownTaxRegime

from bookkeeper_ui.anomaly_reviews import (
    AnomalyReview,
    FileAnomalyReviewStore,
    derive_flag_id,
)
from bookkeeper_ui.candidates import (
    FileArtifactStore,
    FileCandidateDecisionStore,
    FileCandidateStore,
)
from bookkeeper_ui.closes import (
    FileCloseStore,
    closed_import_refusal,
    closed_periods,
    statement_line_in_closed_period,
    transaction_in_closed_period,
)
from bookkeeper_ui.confirmations import SOURCE_HUMAN, Confirmation, FileConfirmationStore
from bookkeeper_ui.exporter import MANIFEST_JSON, FileExportStore, export_package
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
    AnomalyOut,
    CloseRecordOut,
    CloseReviewOut,
    TransactionOut,
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

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

# The example dataset's busy quarter — the import form's default so the happy path
# (import examples/ → review) is one click. It is only a form pre-fill; the store
# still derives each transaction's real period from its own date (`period_of`).
_DEFAULT_PERIOD = "2026-Q2"


def register_ui(
    app: FastAPI,
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
    export_store: FileExportStore | None = None,
    candidate_store: FileCandidateStore | None = None,
    candidate_decision_store: FileCandidateDecisionStore | None = None,
    artifact_store: FileArtifactStore | None = None,
) -> None:
    """Mount the HTML UI on `app`, reading through the same injected stores as #2.

    Adds ``GET /``, the ``/ui/*`` routes (Slice 1 confirm + Slice 2 reconcile),
    and the ``/static`` mount. Kept a separate registration (not inlined in
    `create_app`) so `api.py` stays the JSON surface and this module owns the
    templates/htmx surface — one app, two seams. All four Slice-1/Slice-2 stores
    are injected so the reconcile queue, the resolve path, and the ledger fold all
    read/write through the *same* files the JSON API uses.

    The three Slice-3 stores (`close_store` / `anomaly_review_store` /
    `waiver_store`) are **optional** (default `None`), mirroring `create_app`, so
    the shipped tests that mount the UI with the five kwargs keep working. Here
    only `close_store` is read — it is the closed-period truth the UI write-path
    guards below probe; the other two are threaded for issues B–E.

    The Slice-4 export surface (`export_dir` / `export_store`) is threaded the same
    way (both **optional**, default `None`, mirroring `create_app`) so pre-Slice-4
    call sites keep working. `export_store` is the *same* append-only log
    (`exports.jsonl`) the JSON `GET /export`(s) reads — the UI exports listing +
    guarded download read through it, never re-reading the JSONL by hand; the
    export action reuses B's `export_package`. When unwired the exports listing
    renders its empty state and the download route 404s (there is nothing to serve).

    The three Slice-5 intake stores (`candidate_store` / `candidate_decision_store` /
    `artifact_store`) are threaded the same way (all **optional**, default `None`,
    mirroring `create_app`) so pre-Slice-5 call sites keep working. This issue (A)
    only threads them through so the sibling UI slice (issue B) can mount the
    HTML review queue over the *same* stores the JSON `/intake/*` surface writes;
    the HTML routes themselves are that later slice's.
    """
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse, summary="Import screen (home)")
    async def home(request: Request, period: str = _DEFAULT_PERIOD) -> HTMLResponse:
        """The import screen: upload a transactions and/or a statement file, choose
        the period to review.

        Surfaces a closed banner for **every** signed-closed period (an import
        touching a closed period is refused whole), read from the one closed-period
        truth `close_store.by_period()`. `period` only pre-fills the form / carries
        the nav context; the store still derives each row's real period from its date.
        """
        closed_banners = [
            {"period": p, "signed_at": r.signed_at.isoformat(), "signed_by": r.signed_by}
            for p, r in sorted((await close_store.by_period()).items())
        ] if close_store is not None else []
        return templates.TemplateResponse(
            request,
            "import.html",
            {"default_period": period, "closed_banners": closed_banners},
        )

    @app.post("/ui/import", response_class=HTMLResponse, summary="Handle a transactions upload (htmx)")
    async def ui_import(
        request: Request,
        file: UploadFile,
        period: str = Form(_DEFAULT_PERIOD),
    ) -> HTMLResponse:
        """Import the uploaded transactions file, persist each transaction, render
        the outcome.

        Renders the error *into the page* (a 200 partial htmx swaps in) on a bad
        file rather than a JSON 4xx — the message is for the human at the screen;
        the JSON `/import` still returns the machine 400. Nothing is persisted on a
        parse failure (`import_bytes` raises before any `store`).
        """
        data = await file.read()
        try:
            transactions = import_bytes(data, file.filename or "")
        except TransactionImportError as exc:
            return templates.TemplateResponse(
                request,
                "_import_result.html",
                {"error": str(exc)},
            )

        # Closed-period guard (the UI twin of the JSON `/import` 400): refuse the
        # whole upload — render the refusal into the page as a 200 error partial,
        # nothing persisted — if any row lands in a closed period.
        closed = await closed_periods(close_store)
        offending = [
            (f"{t.vendor} {t.amount} on {t.date.date().isoformat()}", period_of(t.date))
            for t in transactions
            if period_of(t.date) in closed
        ]
        if offending:
            return templates.TemplateResponse(
                request,
                "_import_result.html",
                {"error": closed_import_refusal(offending)},
            )

        for transaction in transactions:
            await ledger_store.store(transaction)

        # Which periods the file actually landed in (by each row's own date) — the
        # convenience links, so a multi-period file is navigable and a typo'd
        # period field never strands the user on an empty queue.
        counts: dict[str, int] = {}
        for transaction in transactions:
            key = period_of(transaction.date)
            counts[key] = counts.get(key, 0) + 1
        detected = sorted(counts.items())

        period = period.strip() or _DEFAULT_PERIOD
        return templates.TemplateResponse(
            request,
            "_import_result.html",
            {
                "imported": len(transactions),
                "period": period,
                "detected": detected,
            },
        )

    @app.post(
        "/ui/statements/import",
        response_class=HTMLResponse,
        summary="Handle a statement upload (htmx)",
    )
    async def ui_import_statement(
        request: Request,
        file: UploadFile,
        period: str = Form(_DEFAULT_PERIOD),
    ) -> HTMLResponse:
        """Import the uploaded statement file, persist each line, render the outcome.

        The reconcile counterpart to `ui_import`: same all-or-nothing discipline
        (a malformed file raises before any `store`, and renders the error into the
        page as a 200 partial), but the success partial links to the *reconcile*
        queue rather than the confirm queue. The store is idempotent, so a
        re-import adds no duplicate lines.
        """
        data = await file.read()
        try:
            lines = import_statement_bytes(data, file.filename or "")
        except StatementImportError as exc:
            return templates.TemplateResponse(
                request,
                "_statement_import_result.html",
                {"error": str(exc)},
            )

        # Closed-period guard (the UI twin of the JSON `/statements/import` 400):
        # refuse the whole upload into the page, nothing persisted, if any line
        # lands in a closed period.
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
            return templates.TemplateResponse(
                request,
                "_statement_import_result.html",
                {"error": closed_import_refusal(offending)},
            )

        for line in lines:
            await statement_store.store(line)

        counts: dict[str, int] = {}
        for line in lines:
            key = period_of(line.date)
            counts[key] = counts.get(key, 0) + 1
        detected = sorted(counts.items())

        period = period.strip() or _DEFAULT_PERIOD
        return templates.TemplateResponse(
            request,
            "_statement_import_result.html",
            {
                "imported": len(lines),
                "period": period,
                "detected": detected,
            },
        )

    @app.get("/ui/queue", response_class=HTMLResponse, summary="The confirm queue")
    async def ui_queue(request: Request, period: str = _DEFAULT_PERIOD) -> HTMLResponse:
        """The confirm queue for `period`: a card per un-resolved proposal/flag.

        Derived from the same `build_ledger` projection as the ledger — the queue
        is exactly the entries not yet `confirmed` (agent `proposed` ones carry the
        trust trail; `flagged` ones carry the reason and no proposal).
        """
        ledger = await build_ledger(
            config=config,
            ledger_store=ledger_store,
            confirmation_store=confirmation_store,
            period=period,
            close_store=close_store,
        )
        pending = [e for e in ledger.entries if e.status != "confirmed"]
        return templates.TemplateResponse(
            request,
            "queue.html",
            {
                "period": period,
                "entries": pending,
                "chart_of_accounts": config.chart_of_accounts,
                "total": len(ledger.entries),
                # The period-level close standing (issue B's LedgerOut fields) — the
                # banner + control suppression when the period is signed closed.
                "closed": ledger.closed,
                "signed_at": ledger.signed_at,
                "signed_by": ledger.signed_by,
            },
        )

    @app.post("/ui/resolve", response_class=HTMLResponse, summary="Confirm/correct (htmx)")
    async def ui_resolve(
        request: Request,
        transaction_id: str = Form(...),
        account: str = Form(...),
        period: str = Form(_DEFAULT_PERIOD),
    ) -> HTMLResponse:
        """Record one confirm/correct decision, then swap the resolved card out.

        Mirrors the JSON `/resolve` guards, both defensive here (unreachable from
        the rendered queue): a **422** for an account not in `chart_of_accounts`
        (§5.2: never invent a category), and a **404** for a transaction id the
        ledger does not hold (N1: a confirmation must never dangle against
        nothing). The account guard runs first, exactly as the API orders them. On
        success the response body is only an out-of-band counter update: the empty
        remainder swaps into the card target, so the card leaves the queue with no
        full-page reload.
        """
        if account not in config.chart_of_accounts:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"account {account!r} is not in chart_of_accounts — §5.2: never "
                    f"invent a category."
                ),
            )

        # Closed-period guard — unlike the account/id guards below (defensive,
        # unreachable from the queue → machine 4xx), a period can be signed while a
        # human has this queue open, so the refusal is *reachable* and rendered into
        # the page as a 200 partial (§5.7: a signed close is durable). The JSON
        # `/resolve` twin returns a machine 409 for the same state.
        closed_period = await transaction_in_closed_period(
            close_store, ledger_store, transaction_id
        )
        if closed_period is not None:
            return templates.TemplateResponse(
                request,
                "_closed_refusal.html",
                {"period": closed_period},
            )

        if not await ledger_store.contains(transaction_id):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"transaction {transaction_id!r} is not in the ledger — a "
                    f"confirmation must never dangle against nothing (N1: typo-safe)."
                ),
            )

        await confirmation_store.record(
            Confirmation(
                transaction_id=transaction_id,
                account=account,
                source=SOURCE_HUMAN,
                decided_at=datetime.now(timezone.utc),
            )
        )

        # Recompute how many still need a human, so the live counter and the
        # "all caught up" empty-state stay honest as the queue shrinks.
        ledger = await build_ledger(
            config=config,
            ledger_store=ledger_store,
            confirmation_store=confirmation_store,
            period=period,
        )
        pending = sum(1 for e in ledger.entries if e.status != "confirmed")
        return templates.TemplateResponse(
            request,
            "_resolved.html",
            {"period": period, "pending": pending},
        )

    @app.get("/ui/reconcile", response_class=HTMLResponse, summary="The reconcile queue")
    async def ui_reconcile(request: Request, period: str = _DEFAULT_PERIOD) -> HTMLResponse:
        """The reconcile queue for `period`: the overlaid `build_reconciliation`
        projection, never a second computation.

        The template partitions the one projection by status: open `to_confirm`
        pairs and open gaps are worked as cards; confident `matched` pairs are a
        read-only trail; resolved items (confirmed / rejected / acknowledged) are
        the audit trail. The header renders the config boundary honestly — the date
        window and the `reconcile_vendor` floor, or its inert truth when unset. A
        zero-statement view (the no-statement guard) renders the empty state, never
        a page of fake gap cards.
        """
        view = await build_reconciliation(
            config=config,
            ledger_store=ledger_store,
            statement_store=statement_store,
            reconciliation_store=reconciliation_store,
            period=period,
        )
        open_count = sum(1 for p in view.to_confirm if p.status == "to_confirm") + sum(
            1 for g in view.gaps if g.status == "gap_open"
        )
        # The period-level close standing — the reconcile view carries no `closed`
        # field, so read the one closed-period truth directly (the banner + the
        # interactive-queue suppression on a signed-closed period).
        record = (await close_store.by_period()).get(period) if close_store is not None else None
        return templates.TemplateResponse(
            request,
            "reconcile.html",
            {
                "period": period,
                "statement_lines": view.statement_lines,
                "matched": view.matched,
                "to_confirm": view.to_confirm,
                "gaps": view.gaps,
                "open_count": open_count,
                "date_window": config.reconcile_date_window(),
                "vendor_floor": config.reconcile_vendor_threshold(),
                "closed": record is not None,
                "signed_at": record.signed_at.isoformat() if record is not None else None,
                "signed_by": record.signed_by if record is not None else None,
            },
        )

    @app.post(
        "/ui/reconcile/resolve",
        response_class=HTMLResponse,
        summary="Confirm/reject/acknowledge a reconcile item (htmx)",
    )
    async def ui_reconcile_resolve(
        request: Request,
        decision: str = Form(...),
        transaction_id: str = Form(""),
        statement_line_id: str = Form(""),
        note: str = Form(""),
        period: str = Form(_DEFAULT_PERIOD),
    ) -> HTMLResponse:
        """Record one reconcile resolution, then swap the resolved card out.

        The form counterpart of JSON `/reconcile/resolve`, with the **identical**
        server-side guards in the identical order — every 422 shape check first
        (never `contains()` a null id), then the 404 existence checks. All are
        defensive here: the rendered queue's forms carry fixed hidden decision/id
        values and mark the note `required`, so a human at the screen can reach
        none of the bad states — the same machine-4xx convention #21 set for
        `/ui/resolve`. On success the response is only an out-of-band open-items
        counter update; the empty remainder swaps into the card target, so the card
        leaves the queue with no full-page reload.

        An empty hidden id posts as ``""`` (a one-sided gap card leaves the absent
        side blank); it is normalized to `None` here so the both-ids-null guard and
        the existence checks see a true absence, never the string ``""``.
        """
        txn_id = transaction_id.strip() or None
        stmt_id = statement_line_id.strip() or None

        # --- 422 shape guards (all before any existence check) ---
        if decision not in VALID_DECISIONS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown decision {decision!r} — must be one of "
                    f"{sorted(VALID_DECISIONS)}."
                ),
            )
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
        if decision in NOTE_REQUIRED_DECISIONS and not note.strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    f"decision {decision!r} requires a non-blank note recording the "
                    f"human's disposition."
                ),
            )

        # --- Closed-period guard (§5.7: a signed close is durable) ---
        # Reachable while a queue is open, so rendered into the page as a 200
        # partial (the JSON `/reconcile/resolve` twin returns a machine 409):
        # refuse if either resolved side lands in a closed period.
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
            return templates.TemplateResponse(
                request,
                "_closed_refusal.html",
                {"period": closed_txn or closed_stmt},
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

        await reconciliation_store.record(
            Reconciliation(
                transaction_id=txn_id,
                statement_line_id=stmt_id,
                decision=decision,
                note=note,
                source=SOURCE_HUMAN,
                decided_at=datetime.now(timezone.utc),
            )
        )

        # Recompute the open-items count off the same projection, so the live
        # counter and the "all reconciled" empty state stay honest as cards leave.
        view = await build_reconciliation(
            config=config,
            ledger_store=ledger_store,
            statement_store=statement_store,
            reconciliation_store=reconciliation_store,
            period=period,
        )
        open_count = sum(1 for p in view.to_confirm if p.status == "to_confirm") + sum(
            1 for g in view.gaps if g.status == "gap_open"
        )
        return templates.TemplateResponse(
            request,
            "_reconcile_resolved.html",
            {"period": period, "open_count": open_count},
        )

    @app.get("/ui/ledger", response_class=HTMLResponse, summary="The categorized ledger")
    async def ui_ledger(request: Request, period: str = _DEFAULT_PERIOD) -> HTMLResponse:
        """The categorized ledger for `period`: the confirmed rows (each with its
        reconciliation badge) + the pending count + a reconcile summary line.

        Passes the reconcile stores to `build_ledger`, so every entry carries its
        Slice-2 `reconciliation` fold (null when no statement was imported). The
        summary line reads the *same* `build_reconciliation` projection the queue
        and the JSON view read, so the three surfaces always agree (AC1).
        """
        ledger = await build_ledger(
            config=config,
            ledger_store=ledger_store,
            confirmation_store=confirmation_store,
            period=period,
            statement_store=statement_store,
            reconciliation_store=reconciliation_store,
            close_store=close_store,
        )
        confirmed = [e for e in ledger.entries if e.status == "confirmed"]
        pending = sum(1 for e in ledger.entries if e.status != "confirmed")

        # The reconcile summary — off the shared projection, so its counts match
        # the reconcile queue exactly. `statement_lines == 0` is the no-statement
        # guard: render "no statement imported" rather than an all-zero tally.
        view = await build_reconciliation(
            config=config,
            ledger_store=ledger_store,
            statement_store=statement_store,
            reconciliation_store=reconciliation_store,
            period=period,
        )
        reconcile_summary = {
            "has_statement": view.statement_lines > 0,
            "matched": len(view.matched),
            "awaiting": sum(1 for p in view.to_confirm if p.status == "to_confirm"),
            "gaps_open": sum(1 for g in view.gaps if g.status == "gap_open"),
        }
        return templates.TemplateResponse(
            request,
            "ledger.html",
            {
                "period": period,
                "confirmed": confirmed,
                "pending": pending,
                "total": len(ledger.entries),
                "reconcile_summary": reconcile_summary,
                # The period-level close standing (issue B's LedgerOut fields).
                "closed": ledger.closed,
                "signed_at": ledger.signed_at,
                "signed_by": ledger.signed_by,
            },
        )

    # --- Slice 3: the close-review screen + its htmx write twins (issue E). The
    # human surface over the composition (issue B) and the write endpoints (C: the
    # anomaly ack + the waiver; D: the sign). `GET /ui/close` renders the SAME
    # `views.build_close_review` projection the JSON `GET /close` serializes (one
    # projection, no second computation). Each write twin renders a 2xx partial with
    # a human-readable refusal in place of the control (the Slice-1 convention: the
    # JSON C/D twins keep the machine 4xx); the server re-verifies + guards exactly
    # as those JSON twins do — the disabled/absent control is a convenience, the
    # server stays the enforcer.

    @app.get("/ui/close", response_class=HTMLResponse, summary="The close-review screen")
    async def ui_close(request: Request, period: str = _DEFAULT_PERIOD) -> HTMLResponse:
        """The close-review screen for `period` — the live framework checklist, tax,
        anomalies, and app gates, or the stored signed snapshot for a closed period.

        Reads `views.build_close_review` and serializes it with the *same*
        `CloseReviewOut.from_review` the JSON `GET /close` uses, so HTML and JSON
        render identical state (AC2). An unregistered `tax_regime` makes `track_tax`
        fail fast (`UnknownTaxRegime`); it is rendered into the page as an error (the
        Slice-1 error-into-the-page rule), never a 500.
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
            return templates.TemplateResponse(
                request, "close.html", {"period": period, "error": str(exc)}
            )
        return templates.TemplateResponse(
            request,
            "close.html",
            {"period": period, "close": CloseReviewOut.from_review(review)},
        )

    @app.post(
        "/ui/anomalies/review",
        response_class=HTMLResponse,
        summary="Acknowledge one anomaly flag (htmx)",
    )
    async def ui_anomalies_review(
        request: Request,
        flag_id: str = Form(...),
        period: str = Form(_DEFAULT_PERIOD),
        note: str = Form(""),
    ) -> HTMLResponse:
        """Record one anomaly acknowledgment, then re-render the card as acknowledged.

        The form twin of JSON `POST /anomalies/review`, with the identical guards —
        a **closed** period (its dispositions are frozen) and a `flag_id` matching no
        **current** flag (a changed flag derives a new id) — each rendered as a 200
        refusal partial in place of the card, rather than the JSON 409/422. The flag
        id is derived with issue A's exact recipe (`derive_flag_id`), so the ack lands
        on the same gate-B linkage the JSON twin feeds.
        """
        if anomaly_review_store is None:
            return templates.TemplateResponse(
                request,
                "_close_refusal.html",
                {"message": "the anomaly-review store is not configured on this server."},
            )
        if period in await closed_periods(close_store):
            return templates.TemplateResponse(
                request,
                "_close_refusal.html",
                {"message": f"period {period} is closed — its anomaly dispositions are frozen (§5.7)."},
            )

        # The current flag set — `flag_anomaly` called as-is (read-only), keyed by the
        # app-derived id (A's exact recipe). A stale/unknown id is refused into the page.
        report = await flag_anomaly(ledger_store, config, period)
        flags_by_id = {derive_flag_id(flag): flag for flag in report.flags}
        flag = flags_by_id.get(flag_id)
        if flag is None:
            return templates.TemplateResponse(
                request,
                "_close_refusal.html",
                {"message": (
                    f"flag {flag_id} matches no current anomaly for {period} — it may "
                    f"have changed. Reload the close review and acknowledge again."
                )},
            )

        review = AnomalyReview(
            flag_id=flag_id,
            kind=flag.kind.value,
            reason=flag.reason,
            transaction_ids=tuple(transaction_key(t) for t in flag.transactions),
            note=note.strip() or None,
            acknowledged_at=datetime.now(timezone.utc),
            source=SOURCE_HUMAN,
        )
        await anomaly_review_store.record(review)

        acknowledged = AnomalyOut(
            id=flag_id,
            kind=flag.kind.value,
            reason=flag.reason,
            transactions=[TransactionOut.from_model(t) for t in flag.transactions],
            acknowledged=True,
            acknowledged_at=review.acknowledged_at.isoformat(),
            note=review.note,
        )
        return templates.TemplateResponse(
            request, "_anomaly_card.html", {"a": acknowledged, "period": period}
        )

    @app.post(
        "/ui/reconciliation/waive",
        response_class=HTMLResponse,
        summary="Waive reconciliation for a no-statement period (htmx)",
    )
    async def ui_reconciliation_waive(
        request: Request,
        period: str = Form(_DEFAULT_PERIOD),
        waived_by: str = Form("owner"),
        note: str = Form(""),
    ) -> HTMLResponse:
        """Record one reconciliation waiver, then re-render the gate block as waived.

        The form twin of JSON `POST /reconciliation/waive`, with the identical guards
        — a **closed** period and a period with a **statement on file** (never
        waivable: reconcile it) — each rendered as a 200 refusal partial rather than
        the JSON 409. After waiving, the gate renders as *waived* (never "reconciled").
        """
        if waiver_store is None:
            return templates.TemplateResponse(
                request,
                "_close_refusal.html",
                {"message": "the waiver store is not configured on this server."},
            )
        if period in await closed_periods(close_store):
            return templates.TemplateResponse(
                request,
                "_close_refusal.html",
                {"message": f"period {period} is closed — it cannot be waived (§5.7)."},
            )
        if await statement_store.fetch_statement(period):
            return templates.TemplateResponse(
                request,
                "_close_refusal.html",
                {"message": (
                    f"period {period} has a statement on file — a present statement is "
                    f"never waivable; reconcile it rather than waiving it."
                )},
            )

        waiver = Waiver(
            period=period,
            waived_at=datetime.now(timezone.utc),
            waived_by=waived_by.strip() or "owner",
            note=note.strip() or None,
        )
        await waiver_store.record(waiver)
        return templates.TemplateResponse(
            request,
            "_reconciliation_gate.html",
            {"reconciliation_source": "waived", "period": period},
        )

    @app.post("/ui/sign", response_class=HTMLResponse, summary="Sign the period closed (htmx)")
    async def ui_sign(
        request: Request,
        period: str = Form(_DEFAULT_PERIOD),
        signed_by: str = Form("owner"),
    ) -> HTMLResponse:
        """Sign `period` closed, then render the signed close (or the refusal).

        The form twin of JSON `POST /sign`, with the identical load-bearing order —
        the period precondition (a well-formed quarterly label with ≥1 ledger txn)
        and the closed-period guard **before** any composition, then in-handler
        re-verification via the *same* `build_close_review` and the three app gates.
        On a not-signable period the screen re-renders enumerating the failed gates
        (server-enforced); on pass it appends **exactly one** durable close record
        and renders the signed snapshot. Refusals are 200 partials; the JSON twin
        keeps the machine 4xx.
        """
        if close_store is None:
            return templates.TemplateResponse(
                request,
                "_close_refusal.html",
                {"message": "the close store is not configured on this server."},
            )

        # 1. Period precondition — before any composition (a garbage/empty label under
        # an unset prior would append a close the effective-prior read cannot order).
        if not is_quarterly_period(period):
            return templates.TemplateResponse(
                request,
                "_close_refusal.html",
                {"message": (
                    f"period {period!r} is not a well-formed quarterly label (YYYY-Qn) "
                    f"— a close is never signed under a label the prior-period guard "
                    f"cannot order."
                )},
            )
        if not await ledger_store.fetch_for_period(period):
            return templates.TemplateResponse(
                request,
                "_close_refusal.html",
                {"message": (
                    f"period {period} has no ledger transactions — there is nothing to "
                    f"close. Import and confirm the period's transactions before signing."
                )},
            )

        # 2. Closed-period guard — before trusting the composition. An already-closed
        # period renders its stored signed snapshot (never a second close row).
        if period in await closed_periods(close_store):
            record = (await close_store.by_period())[period]
            return templates.TemplateResponse(
                request,
                "_close_signed.html",
                {
                    "record": CloseRecordOut.from_record(record).model_dump(),
                    "period": period,
                    "already": True,
                },
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
                period=period,
            )
        except UnknownTaxRegime as exc:
            return templates.TemplateResponse(
                request, "_close_refusal.html", {"message": str(exc)}
            )

        if not review.signable:
            # Re-render the screen enumerating exactly which checks/gates are unmet.
            return templates.TemplateResponse(
                request,
                "_close_screen.html",
                {
                    "period": period,
                    "close": CloseReviewOut.from_review(review),
                    "sign_attempted": True,
                },
            )

        # 4. On pass — append exactly one self-contained close record and render it.
        record = await build_close_record(
            review=review,
            waiver_store=waiver_store,
            signed_by=signed_by.strip() or "owner",
            signed_at=datetime.now(timezone.utc),
        )
        await close_store.record(record)
        return templates.TemplateResponse(
            request,
            "_close_signed.html",
            {"record": CloseRecordOut.from_record(record).model_dump(), "period": period},
        )

    # --- Slice 4 · C: the accountant-package preview (read-only). The human surface
    # over the Contract A deliverable — the SAME `views.build_package` projection the
    # JSON `GET /package` (issue A) serializes (one projection, no second computation,
    # no direct `generate_accountant_package` call). It renders the honest two-state
    # picture: PROPOSED (assembled, never auto-published — the full trust trail + tax
    # + reconciliation + the app's confirmation overlay) or BLOCKED (the framework's
    # `unmet_close` reason verbatim, no deliverable, no export control). The Export
    # button + acknowledgment checkbox on a PROPOSED page are client convenience only
    # — the real §5.4 refusal gate lives server-side in issue B's `POST /export`, which
    # re-obtains the close and refuses a non-PROPOSED package regardless of this page.

    @app.get("/ui/package", response_class=HTMLResponse, summary="The accountant-package preview")
    async def ui_package(request: Request, period: str = _DEFAULT_PERIOD) -> HTMLResponse:
        """The accountant-package preview for `period` — proposed (assembled) or blocked.

        Reads `views.build_package` and renders `package.html` — the same projection
        the JSON `GET /package` serializes, so HTML and JSON render identical state.
        A PROPOSED package carries the full trail (summary, the costed/categorized/taxed
        entries with their trust trail + the additive confirmation overlay, the tax
        breakout, the reconciliation trail); a BLOCKED package renders `unmet_close`
        verbatim with no export control.

        An unregistered `tax_regime` makes `track_tax` fail fast (`UnknownTaxRegime`);
        it is rendered into the page as an error (the Slice-1 error-into-the-page
        rule), never a 500.
        """
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
            return templates.TemplateResponse(
                request, "package.html", {"period": period, "error": str(exc)}
            )
        return templates.TemplateResponse(
            request,
            "package.html",
            {"period": period, "package": package},
        )

    # --- Slice 4 · D: the exports listing + guarded download + the export action.
    # The read/serve surface over what B wrote — the SAME append-only `exports.jsonl`
    # log the JSON `GET /exports` reads (`export_store`), never a second reader. The
    # listing lets a human *see the log*; the download lets him *pull the local files*
    # (`FileResponse` from the local exports dir to the local browser — the entire
    # transport story; nothing leaves the machine). The export action (`POST /ui/export`)
    # is the human twin of B's JSON `POST /export`: it re-obtains the package from the
    # app's own stores and reuses B's `export_package` (no second write path).

    def _download_names(record) -> list[str]:
        """The exact filenames downloadable for one export — the log row's file-list
        (the three hashed Core files) plus the manifest, which the row deliberately
        excludes from its own hash set. This closed set is the download allow-list."""
        return [str(f["name"]) for f in record.files] + [MANIFEST_JSON]

    def _export_files(record) -> list[dict[str, str]]:
        """Per-file ``{name, url}`` for one export — one guarded-download link per Core
        file (path segments URL-encoded), for the listing rows + the export result."""
        return [
            {
                "name": name,
                "url": f"/ui/exports/{quote(record.export_id, safe='')}/{quote(name, safe='')}",
            }
            for name in _download_names(record)
        ]

    @app.get("/ui/exports", response_class=HTMLResponse, summary="The exports listing")
    async def ui_exports(request: Request, period: str = _DEFAULT_PERIOD) -> HTMLResponse:
        """The exports listing — the whole append-only log, newest-first.

        A read: reads the *same* `export_store` log (`exports.jsonl`) the JSON
        `GET /exports` serializes (never a second reader), and renders every export
        with its id/period/status/time/divergence plus one guarded-download link per
        Core file. `period` only carries the nav context (it never filters the log —
        the listing shows *all* exports); an unwired export surface renders the honest
        empty state, never a 500. Writes nothing.
        """
        records = list(await export_store.all()) if export_store is not None else []
        rows = [
            {
                "export_id": record.export_id,
                "period": record.period,
                "package_status": record.package_status,
                "exported_at": record.exported_at.isoformat(),
                "divergence_count": record.divergence_count,
                "files": _export_files(record),
            }
            for record in reversed(records)  # newest-first (reverse of insertion order)
        ]
        return templates.TemplateResponse(
            request, "exports.html", {"period": period, "exports": rows}
        )

    @app.get(
        "/ui/exports/{export_id}/{filename}",
        summary="Download one exported Core file (guarded)",
    )
    async def ui_export_download(export_id: str, filename: str) -> FileResponse:
        """Serve one exported Core file from the local exports dir — the guarded route.

        The guard is the whole point (Slice-4 AC-17): the file is served *only* when
        both `export_id` and `filename` are exact string members of the injected log —
        `export_id` a real log row, `filename` one of that row's Core files (the
        allow-list, never disk presence). Any name not literally in that closed set
        (a traversal `../`, an absolute path, an unlisted name) fails membership before
        any path is built → **404**. A second wall confirms the resolved real path is
        inside the exports root. A listed-but-missing file is a 404, never a 500.
        Nothing outside `exports/<export_id>/` is ever served.
        """
        if export_store is None or export_dir is None:
            raise HTTPException(status_code=404, detail="no export exists.")

        records = {r.export_id: r for r in await export_store.all()}
        record = records.get(export_id)
        if record is None:  # unknown export id — never a served file
            raise HTTPException(
                status_code=404, detail=f"export {export_id!r} is not in the export log."
            )
        if filename not in _download_names(record):  # exact-membership allow-list
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{filename!r} is not a file of export {export_id!r} — only the "
                    f"export's own Core files are served."
                ),
            )

        # Build the path from the validated, listed values only — then a defensive
        # second wall: the resolved real path must sit inside the exports root.
        root = Path(export_dir).resolve()
        candidate = (root / export_id / filename).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(status_code=404, detail="not found.")
        if not candidate.is_file():  # listed but missing on disk → 404, never a 500
            raise HTTPException(status_code=404, detail="not found.")
        return FileResponse(candidate, filename=filename)

    @app.post(
        "/ui/export",
        response_class=HTMLResponse,
        summary="Export the package to local files (htmx)",
    )
    async def ui_export(
        request: Request,
        period: str = Form(_DEFAULT_PERIOD),
        acknowledged: str = Form(""),
    ) -> HTMLResponse:
        """Export `period`'s package to local files — the human twin of B's `POST /export`.

        Does the **same** §5.4 server-side re-obtain B does: rebuilds the package from
        the app's own stores at request time (`build_package`), never trusting the
        previewed page or any client state (the `acknowledged` checkbox is UX-only —
        never a server gate). Then:

        - export surface unwired → an error partial (nothing written);
        - a **non-PROPOSED** rebuild → the refusal rendered into the partial quoting
          `unmet_close` verbatim, **writing nothing** (no folder, no log row) — the
          web convention (a human error is a 200 partial; B's machine route keeps 409);
        - PROPOSED → reuse B's `export_package` (the sole write path — a fresh folder +
          exactly one appended log row) and render the export id + one guarded-download
          link per Core file.

        An unregistered `tax_regime` makes the rebuild fail fast (`UnknownTaxRegime`);
        it is rendered into the partial as an error (the Slice-1 error-into-the-page
        rule), never a 500.
        """
        if export_dir is None or export_store is None:
            return templates.TemplateResponse(
                request,
                "_export_result.html",
                {"error": "the export directory is not configured on this server."},
            )

        # Re-obtain + rebuild the package server-side — never ride the previewed page.
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
            return templates.TemplateResponse(
                request, "_export_result.html", {"period": period, "error": str(exc)}
            )

        # A non-PROPOSED rebuild refuses into the partial, writing nothing.
        if package.status != PackageStatus.PROPOSED.value:
            return templates.TemplateResponse(
                request,
                "_export_result.html",
                {"period": period, "refusal": package.unmet_close},
            )

        # PROPOSED → reuse B's exporter (the sole write path); no second exporter here.
        from bookkeeper_ui import __version__  # local: avoids the __init__↔web cycle

        record = export_package(
            package=package,
            config=config,
            export_dir=Path(export_dir),
            exported_at=datetime.now(timezone.utc),
            app_version=__version__,
        )
        await export_store.record(record)
        return templates.TemplateResponse(
            request,
            "_export_result.html",
            {
                "period": period,
                "export_id": record.export_id,
                "files": _export_files(record),
                "divergence_count": record.divergence_count,
            },
        )
