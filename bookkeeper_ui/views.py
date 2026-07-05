"""The categorized-ledger projection — one place the API (#2) and the UI (#3) share.

Building the ledger view is the one piece of read logic both surfaces need: run
`categorize` (read-only) for the pending proposals/flags, then overlay the
confirmation store's latest human decision per transaction. It is non-trivial
(the confirmation overlay, the flagged fallback), so it lives here as a single
function rather than being re-derived once per surface — the JSON `/ledger` route
returns it as-is, and the UI derives both its confirm queue and its ledger table
from the same `LedgerOut`.

Returns the `LedgerOut` schema (the same wire shape #2 already serialized), not a
new domain type: the API returns it directly and a Jinja template reads its
fields (`entry.status`, `entry.transaction.vendor`, …) just as happily as JSON.
"""

from __future__ import annotations

from bookkeeper.config import BookkeeperConfig
from bookkeeper.skills.categorize import categorize

from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.schemas import LedgerEntryOut, LedgerOut, TransactionOut


async def build_ledger(
    *,
    config: BookkeeperConfig,
    ledger_store: FileLedgerStore,
    confirmation_store: FileConfirmationStore,
    period: str,
) -> LedgerOut:
    """The categorized ledger for `period`: every stored transaction (in the
    store's deterministic read order) annotated with its current standing —
    `confirmed` (resolved account), `proposed` (agent trust trail), or `flagged`
    (needs a human).

    Re-runs `categorize` for the pending proposals/flags (it writes nothing) and
    overlays the confirmation store's latest decision per transaction, so a
    resolved transaction shows `confirmed` even if it was first flagged.
    """
    report = await categorize(ledger_store, config, period)
    transactions = await ledger_store.fetch_for_period(period)
    proposals = {transaction_key(p.transaction): p for p in report.proposals}
    flags = {transaction_key(f.transaction): f for f in report.flagged}
    confirmed = await confirmation_store.latest_by_transaction()

    entries: list[LedgerEntryOut] = []
    for transaction in transactions:
        txn_id = transaction_key(transaction)
        out = TransactionOut.from_model(transaction)

        confirmation = confirmed.get(txn_id)
        if confirmation is not None:
            entries.append(
                LedgerEntryOut(
                    transaction=out,
                    status="confirmed",
                    account=confirmation.account,
                    source=confirmation.source,
                )
            )
            continue

        proposal = proposals.get(txn_id)
        if proposal is not None:
            entries.append(
                LedgerEntryOut(
                    transaction=out,
                    status="proposed",
                    account=proposal.proposed_account,
                    confidence=proposal.confidence,
                    source=proposal.source,
                )
            )
            continue

        # Every fetched transaction is partitioned into proposals ∪ flagged by
        # categorize, so this is the flagged branch; the fallback reason is a
        # defensive belt in case a hand-edited ledger drifts from the report.
        flag = flags.get(txn_id)
        entries.append(
            LedgerEntryOut(
                transaction=out,
                status="flagged",
                reason=flag.reason if flag is not None else "Uncategorized.",
            )
        )

    return LedgerOut(period=period, entries=entries)
