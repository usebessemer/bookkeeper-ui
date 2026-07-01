"""bookkeeper-ui — the local thin UI's app layer over the Bookkeeper framework.

Slice 1 · issue #1 (Foundation) builds the layer *under* the API and UI:

- `FileLedgerStore` — the file-based `booksLocation` adapter implementing the
  framework's `LedgerSink` + `LedgerSource` ports.
- `FileConfirmationStore` / `Confirmation` — the separate human confirm/correct
  resolution layer.
- `import_csv` / `import_json` / `import_and_store` — CSV/JSON → `Transaction`.
- `load_config` — a `BookkeeperConfig` from a local JSON file.
- `period_of` / `transaction_key` — the store's period + dedupe conventions,
  shared so #2/#3 don't re-derive them.

The framework (`bookkeeper`, from `usebessemer/agent-classes`) is a dependency,
never modified here.
"""

from __future__ import annotations

from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import (
    SOURCE_HUMAN,
    Confirmation,
    FileConfirmationStore,
)
from bookkeeper_ui.importer import (
    TransactionImportError,
    import_and_store,
    import_csv,
    import_file,
    import_json,
    row_to_transaction,
)
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.periods import period_of

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # ledger store (booksLocation adapter — LedgerSink + LedgerSource)
    "FileLedgerStore",
    "transaction_key",
    "period_of",
    # confirmation store (the human resolution layer)
    "FileConfirmationStore",
    "Confirmation",
    "SOURCE_HUMAN",
    # import
    "import_csv",
    "import_json",
    "import_file",
    "import_and_store",
    "row_to_transaction",
    "TransactionImportError",
    # config
    "load_config",
]
