"""The example-config regime fix: `track_tax` runs over the example config.

Slice 3 calls `track_tax` on every close review, and `select_regime` fail-fasts on
any regime but `HST`. Pins AC4: the committed example config uses a registered
regime (so `track_tax` does not raise), while a genuinely unknown regime still
surfaces the framework's `UnknownTaxRegime` — the rendered-error requirement is
never swallowed.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from bookkeeper.skills.track_tax import UnknownTaxRegime, select_regime, track_tax

from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.importer import import_file
from bookkeeper_ui.ledger_store import FileLedgerStore


def test_example_config_declares_hst(examples_dir):
    """AC4: the committed example config's `tax_regime` is the registered `HST`."""
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    assert data["tax_regime"] == "HST"


async def test_track_tax_over_example_config_does_not_raise(tmp_path, examples_dir):
    """AC4: `track_tax` over the example config + data selects HST and totals cleanly."""
    config = load_config(examples_dir / "config.json")
    store = FileLedgerStore(tmp_path / "ledger.jsonl")
    for txn in import_file(str(examples_dir / "transactions.csv")):
        await store.store(txn)

    summary = await track_tax(store, config, "2026-Q2")  # must not raise
    assert summary.regime == "HST"


def test_unknown_regime_still_fails_fast(examples_dir):
    """AC4: a genuinely unknown regime still raises `UnknownTaxRegime` (unswallowed)."""
    config = replace(load_config(examples_dir / "config.json"), tax_regime="standard")
    with pytest.raises(UnknownTaxRegime):
        select_regime(config)
