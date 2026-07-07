"""The Tier-2 read/write API the thin UI talks to (FastAPI, async).

Four operations over the #1 foundation, plus the serialization boundary:

- ``POST /import``            — upload a CSV/JSON → persist via the `LedgerSink`.
- ``POST /categorize?period`` — call the framework's `categorize` **unmodified**
  → return the `CategorizationReport` (proposals = the trust trail; flagged).
- ``POST /resolve``           — record a confirm/correct decision (account
  validated against `chart_of_accounts`; transaction id must be one the ledger
  holds — a strict 404 otherwise, never an orphan) to the confirmation store.
- ``GET  /ledger?period``     — the categorized ledger: every transaction with
  its resolved account (if confirmed) or its pending status (proposed / flagged).

**Async on purpose** — it matches the framework's async ports/skills contract and
serves the #3 UI. **The framework stays pure**: this module and `schemas.py` own
all the web/pydantic surface; nothing here is pushed back into `../agent-classes`.
**Writes only through the #1 stores** — `categorize` writes nothing; the sole
write path is a human resolution into the confirmation store via `/resolve`.

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

from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import (
    SOURCE_HUMAN,
    Confirmation,
    FileConfirmationStore,
)
from bookkeeper_ui.importer import TransactionImportError, import_bytes
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.schemas import (
    CategorizationReportOut,
    ConfirmationOut,
    ImportResultOut,
    LedgerOut,
    ResolveRequest,
    TransactionOut,
)
from bookkeeper_ui.views import build_ledger
from bookkeeper_ui.web import register_ui


def create_app(
    *,
    config: BookkeeperConfig,
    ledger_store: FileLedgerStore,
    confirmation_store: FileConfirmationStore,
) -> FastAPI:
    """Build the API over an injected config + ledger/confirmation stores.

    Dependencies are passed in (not read from a global) so a test can drive the
    app over a temp-path ledger and a fresh confirmation trail, and #3 can wire
    the real local paths. The stores are the *only* things the routes write
    through — the framework `categorize` is called read-only.
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
        transaction is even looked up. Append-only: a correction is a new row the
        ledger view collapses to last-write-wins.
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
        so JSON and HTML render the same standing per transaction.
        """
        return await build_ledger(
            config=config,
            ledger_store=ledger_store,
            confirmation_store=confirmation_store,
            period=period,
        )

    # #3 — the thin UI (Jinja + htmx) over these same stores, mounted on this app
    # so `uvicorn bookkeeper_ui.api:build_app_from_env --factory` serves both the
    # JSON API (root paths) and the HTML surface (GET / and the /ui/* routes).
    register_ui(
        app,
        config=config,
        ledger_store=ledger_store,
        confirmation_store=confirmation_store,
    )

    return app


def build_app_from_env() -> FastAPI:
    """Build the app from env vars — the runnable default for local serving.

    ``uvicorn bookkeeper_ui.api:build_app_from_env --factory``

    - ``BOOKKEEPER_UI_CONFIG``   — path to the `BookkeeperConfig` JSON
                                   (default ``examples/config.json``).
    - ``BOOKKEEPER_UI_DATA_DIR`` — directory for the ledger + confirmation files
                                   (default ``data``); created on first write.

    The wiring is deliberately thin: #3 (the UI) owns the real run surface. This
    exists so the API is runnable on its own for local development and #2's tests
    exercise `create_app` directly with injected temp paths.
    """
    config_path = os.environ.get("BOOKKEEPER_UI_CONFIG", "examples/config.json")
    data_dir = Path(os.environ.get("BOOKKEEPER_UI_DATA_DIR", "data"))
    return create_app(
        config=load_config(config_path),
        ledger_store=FileLedgerStore(data_dir / "ledger.jsonl"),
        confirmation_store=FileConfirmationStore(data_dir / "confirmations.jsonl"),
    )
