# bookkeeper-ui

Local, open-source thin UI for the Bessemer Bookkeeper agent: import
transactions → the agent proposes a chart-of-accounts category per transaction →
you confirm/correct → confirmations persist. Local-first, single-user,
file-based. Depends on the [`agent-classes`](https://github.com/usebessemer/agent-classes)
`bookkeeper` framework (the contract) and never modifies it.

## What's built (Slice 1 — complete)

- **#1 Foundation** — the layer *under* the API: the local file store, transaction
  import, and config loading.
  - **`FileLedgerStore`** — the file-based `booksLocation` adapter implementing
    the framework's `LedgerSink` (write, idempotent) + `LedgerSource` (read,
    deterministic order) ports over one JSONL file.
  - **`FileConfirmationStore` / `Confirmation`** — the *separate* human
    confirm/correct resolution layer (kept distinct from the raw ledger).
  - **`import_csv` / `import_json` / `import_bytes` / `import_and_store`** —
    CSV/JSON → framework `Transaction`s.
  - **`load_config`** — a `BookkeeperConfig` from a local JSON file.
- **#2 API** — the FastAPI (async) read/write API the thin UI talks to: run the
  agent, read the trust trail, submit resolutions, read the categorized ledger.
  See [The API](#the-api-2) below.
- **#3 Thin UI** — the visible surface: import, the confirm queue rendering the
  full trust trail, and the categorized-ledger view. Server-rendered Jinja + htmx
  (no Node/build step), served by the same FastAPI app. See
  [The UI](#the-ui-3) below.

Slice 1 (standalone categorize-and-confirm) is complete.

## Install & test (dev)

The `bookkeeper` framework has no PyPI release, so `pyproject.toml` pins it as a
git direct reference (`bookkeeper @ git+…/agent-classes.git@v0.1.0`). A plain
install pulls the framework from git at that tag — no PyPI (a squatted, unrelated
`bookkeeper` package lives there; see #10) and no sibling clone required:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'             # this package + framework (from git) + pytest
pytest
```

**Dev override — editable framework from a sibling clone.** To work against a
live `../agent-classes` checkout, install the editable sibling **last** so it
wins. A direct-URL requirement is *not* satisfied by an already-installed
editable, so doing it the other way round (sibling first, then this repo) would
re-fetch the pinned tag and clobber your editable checkout:

```bash
pip install -e '.[test]'             # this package (pulls the pinned framework)
pip install -e ../agent-classes      # override: editable framework — must be last
pytest
```

## Import format

CSV headers and JSON object keys share the same names:

| field                   | required | maps to                             |
|-------------------------|----------|-------------------------------------|
| `date`                  | yes      | `Transaction.date` — ISO 8601 (`2026-04-03`) |
| `vendor`                | yes      | `Transaction.vendor`                |
| `amount`                | yes      | `Transaction.amount` — exact `Decimal` |
| `attribution_target_id` | yes      | `Transaction.attribution_target_id` |
| `tax`                   | no       | `Transaction.tax` — `Decimal`; blank → `0` |
| `description`           | no       | `Transaction.description` — `""` if absent |

Money is parsed as exact `Decimal` (never float); absent/blank `tax` coalesces
to `Decimal("0")` (the framework never holds None-money). Each row's own JSON
serialization is kept as the transaction's `artifact_bytes`, so a stored figure
stays linked to its source (charter §1: traceable). JSON import accepts a
top-level list of row objects or a `{"transactions": [...]}` wrapper.

**Period.** The framework reads transactions back by an opaque period string
(e.g. `2026-Q2`). The store places each transaction into a **calendar quarter**
from its date (`period_of`), and `fetch_for_period` answers consistently.

## Examples

Runnable sample dataset under [`examples/`](examples/):

- `config.json` — a full `BookkeeperConfig` (chart of accounts, a live
  `categorize` threshold, owner category rules).
- `transactions.csv` / `transactions.json` — the same six transactions in both
  import formats (one in `2026-Q1`, five in `2026-Q2`).

```python
import asyncio
from bookkeeper_ui import FileLedgerStore, import_csv, load_config

async def main():
    store = FileLedgerStore("data/ledger.jsonl")
    for txn in import_csv("examples/transactions.csv"):
        await store.store(txn)
    config = load_config("examples/config.json")
    q2 = await store.fetch_for_period("2026-Q2")
    print(len(q2), "transactions in 2026-Q2")

asyncio.run(main())
```

## The API (#2)

FastAPI (async, no Node build step). The framework dataclasses stay pure —
pydantic serialization lives only at this boundary (`bookkeeper_ui/schemas.py`),
money crosses the wire as an exact string, and every transaction carries its
stable `id` (the ledger `transaction_key`) so the UI can post it back to
`/resolve`.

| method & path              | does                                                                   |
|----------------------------|------------------------------------------------------------------------|
| `POST /import`             | upload a `.csv`/`.json` of transactions → persist via the file store   |
| `POST /categorize?period=` | run the framework's `categorize` **as-is** → `proposals[]` (the trust trail: `proposed_account` + `confidence` + `source`) and `flagged[]` (`reason`) |
| `POST /resolve`            | record a confirm/correct decision (`{transaction_id, account}`); rejects an account not in `chart_of_accounts` |
| `GET  /ledger?period=`     | the categorized ledger: each transaction with its `confirmed` account, or its pending `proposed` / `flagged` status |
| `GET  /health`             | liveness check                                                         |

`categorize` writes nothing (proposals-only); the one write path is a human
resolution into the confirmation store via `/resolve`. Interactive docs are at
`/docs` when the server is running.

### Run it

```bash
uvicorn bookkeeper_ui.api:build_app_from_env --factory --reload
```

Configured by env vars (both optional):

- `BOOKKEEPER_UI_CONFIG` — path to the config JSON (default `examples/config.json`).
- `BOOKKEEPER_UI_DATA_DIR` — dir for the ledger + confirmation files (default `data`).

A quick end-to-end pass with the sample data:

```bash
curl -F file=@examples/transactions.csv localhost:8000/import
curl -X POST 'localhost:8000/categorize?period=2026-Q2'
curl -X POST localhost:8000/resolve \
  -H 'content-type: application/json' \
  -d '{"transaction_id": "<id from /categorize>", "account": "5200-travel"}'
curl 'localhost:8000/ledger?period=2026-Q2'
```

Embedding the API in a process instead? `create_app(config=…, ledger_store=…,
confirmation_store=…)` builds it over injected stores (this is what the tests and
the UI use).

## The UI (#3)

The visible surface — **import → confirm queue → categorized ledger** — rendered
server-side with **Jinja templates + htmx** and served by the *same* FastAPI app
as the JSON API (the pages live at `/` and under `/ui/*`; the JSON API keeps its
root paths). **No Node, no build step**; htmx is vendored under
`bookkeeper_ui/static/` so it runs fully local and offline.

| page                   | what it does                                                            |
|------------------------|------------------------------------------------------------------------|
| `GET /`                | **Import** — upload a CSV/JSON and choose the period to review          |
| `GET /ui/queue?period=`| **Confirm queue** — a card per proposal showing the full **trust trail** (proposed account · confidence · the rule that fired), plus flagged items with their reason. Confirm / Pick-another → `/ui/resolve`; htmx swaps the resolved card out (no full-page reload) |
| `GET /ui/ledger?period=`| **Categorized ledger** — the confirmed transactions with their accounts, plus the count still pending |

### Run it

Install (see [Install & test](#install--test-dev) — the UI's `jinja2` / `uvicorn`
deps come with `pip install -e '.[test]'`), then start the server:

```bash
uvicorn bookkeeper_ui.api:build_app_from_env --factory --reload
```

Open **http://localhost:8000** and:

1. **Import** the sample data — pick `examples/transactions.csv` (or
   `examples/transactions.json`), leave the period as `2026-Q2`, and submit.
2. Follow **“Review the confirm queue”** — each card shows the agent's proposed
   account, how confident it is, and which rule fired (`owner-rule` vs
   `chart-match`); flagged rows show why they need a human. **Confirm** in one tap
   or **Pick another** account; the card leaves the queue as you go.
3. Open the **Categorized ledger** to see the confirmed transactions and how many
   are still pending.

Configured by the same env vars as the API (`BOOKKEEPER_UI_CONFIG`,
`BOOKKEEPER_UI_DATA_DIR`; both optional). The API and its interactive docs
(`/docs`) are served alongside the UI on the same port.

## Scope & conventions

Categorize-and-confirm only; **no `agent-classes` changes**; single-user, local,
file-based. Branches: `feature/<slug>` off `develop`; PRs target `develop`.
`pytest` green before every commit.
