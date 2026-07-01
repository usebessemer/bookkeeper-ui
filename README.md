# bookkeeper-ui

Local, open-source thin UI for the Bessemer Bookkeeper agent: import
transactions → the agent proposes a chart-of-accounts category per transaction →
you confirm/correct → confirmations persist. Local-first, single-user,
file-based. Depends on the [`agent-classes`](https://github.com/usebessemer/agent-classes)
`bookkeeper` framework (the contract) and never modifies it.

## Slice 1 · issue #1 — Foundation (this PR)

The layer *under* the API (#2) and UI (#3): the local file store, transaction
import, and config loading. No API and no UI yet.

- **`FileLedgerStore`** — the file-based `booksLocation` adapter implementing the
  framework's `LedgerSink` (write, idempotent) + `LedgerSource` (read,
  deterministic order) ports over one JSONL file.
- **`FileConfirmationStore` / `Confirmation`** — the *separate* human
  confirm/correct resolution layer (kept distinct from the raw ledger).
- **`import_csv` / `import_json` / `import_and_store`** — CSV/JSON → framework
  `Transaction`s.
- **`load_config`** — a `BookkeeperConfig` from a local JSON file.

## Install & test (dev)

The framework is a sibling clone at `../agent-classes`. Install it editable
first, then this repo:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ../agent-classes      # the bookkeeper framework
pip install -e '.[test]'             # this package + pytest
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

Calling the framework's `categorize` over this store is issue #2.

## Scope & conventions

Categorize-and-confirm only; **no `agent-classes` changes**; single-user, local,
file-based. Branches: `feature/<slug>` off `develop`; PRs target `develop`.
`pytest` green before every commit.
