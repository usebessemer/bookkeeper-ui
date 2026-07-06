"""The thin UI (#3) — Jinja templates + htmx, mounted on the same FastAPI app.

Three screens over the #2 stores, server-rendered, **no Node/build step**:

- ``GET  /``            — **import**: upload a CSV/JSON, pick the period to review.
- ``GET  /ui/queue``    — the **confirm queue** (the core): one card per proposal
  rendering the full **trust trail** (proposed account · confidence · the rule
  that fired), plus flagged transactions with their reason. Confirm / Pick-another
  post to ``/ui/resolve``; htmx swaps the resolved card out — no full-page reload.
- ``GET  /ui/ledger``   — the **categorized ledger**: the confirmed transactions
  with their accounts, plus the count still pending.

The HTML surface is deliberately separate from the JSON API (which keeps its root
paths, `/import` … `/ledger`, so #2's clients and tests are untouched): the pages
live at ``/`` and under ``/ui/*``, and both read through the *same* stores and the
*same* `views.build_ledger` projection the JSON `/ledger` returns. htmx is vendored
under ``static/`` (no CDN dependency — this runs local and offline).

The UI import/resolve handlers render an **error into the page** (a 200 partial the
user reads) rather than a JSON 4xx: on this surface the message is for a human, and
htmx swaps a 2xx body in place. The JSON API still returns the machine 4xx. The one
exception is `/ui/resolve` with an off-chart account (unreachable from the rendered
select) — a defensive 422, mirroring the API's §5.2 guard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bookkeeper.config import BookkeeperConfig

from bookkeeper_ui.confirmations import SOURCE_HUMAN, Confirmation, FileConfirmationStore
from bookkeeper_ui.importer import TransactionImportError, import_bytes
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.periods import period_of
from bookkeeper_ui.views import build_ledger

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
) -> None:
    """Mount the HTML UI on `app`, reading through the same injected stores as #2.

    Adds ``GET /``, the ``/ui/*`` routes, and the ``/static`` mount. Kept a
    separate registration (not inlined in `create_app`) so `api.py` stays the JSON
    surface and this module owns the templates/htmx surface — one app, two seams.
    """
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse, summary="Import screen (home)")
    async def home(request: Request) -> HTMLResponse:
        """The import screen: upload a CSV/JSON and choose the period to review."""
        return templates.TemplateResponse(
            request,
            "import.html",
            {"default_period": _DEFAULT_PERIOD},
        )

    @app.post("/ui/import", response_class=HTMLResponse, summary="Handle an upload (htmx)")
    async def ui_import(
        request: Request,
        file: UploadFile,
        period: str = Form(_DEFAULT_PERIOD),
    ) -> HTMLResponse:
        """Import the uploaded file, persist each transaction, render the outcome.

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

        Rejects (422) an account not in `chart_of_accounts` — §5.2 holds for a
        human decision through the UI too (unreachable from the rendered select; a
        defensive guard). On success the response body is only an out-of-band
        counter update: the empty remainder swaps into the card target, so the card
        leaves the queue with no full-page reload.
        """
        if account not in config.chart_of_accounts:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"account {account!r} is not in chart_of_accounts — §5.2: never "
                    f"invent a category."
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

    @app.get("/ui/ledger", response_class=HTMLResponse, summary="The categorized ledger")
    async def ui_ledger(request: Request, period: str = _DEFAULT_PERIOD) -> HTMLResponse:
        """The categorized ledger for `period`: the confirmed rows + the pending count."""
        ledger = await build_ledger(
            config=config,
            ledger_store=ledger_store,
            confirmation_store=confirmation_store,
            period=period,
        )
        confirmed = [e for e in ledger.entries if e.status == "confirmed"]
        pending = sum(1 for e in ledger.entries if e.status != "confirmed")
        return templates.TemplateResponse(
            request,
            "ledger.html",
            {
                "period": period,
                "confirmed": confirmed,
                "pending": pending,
                "total": len(ledger.entries),
            },
        )
