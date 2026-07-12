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

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bookkeeper.config import BookkeeperConfig

from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore
from bookkeeper_ui.closes import (
    FileCloseStore,
    closed_import_refusal,
    closed_periods,
    statement_line_in_closed_period,
    transaction_in_closed_period,
)
from bookkeeper_ui.confirmations import SOURCE_HUMAN, Confirmation, FileConfirmationStore
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
from bookkeeper_ui.statement_importer import StatementImportError
from bookkeeper_ui.statement_importer import import_bytes as import_statement_bytes
from bookkeeper_ui.statement_store import FileStatementStore
from bookkeeper_ui.views import build_ledger, build_reconciliation
from bookkeeper_ui.waivers import FileWaiverStore

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
    """
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse, summary="Import screen (home)")
    async def home(request: Request) -> HTMLResponse:
        """The import screen: upload a transactions and/or a statement file, choose
        the period to review."""
        return templates.TemplateResponse(
            request,
            "import.html",
            {"default_period": _DEFAULT_PERIOD},
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
            },
        )
