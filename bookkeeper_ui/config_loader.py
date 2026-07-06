"""Loading a `BookkeeperConfig` from a local config file.

The framework owns the config *schema* and its fail-fast validation
(`BookkeeperConfig.from_mapping`, which raises `ConfigError` listing every
missing §3 field at once). This module owns only the thin file boundary:
read a local JSON file → hand the mapping to `from_mapping`. No validation logic
is duplicated here — the framework is the single source of truth for what a
valid config is.

Config file format: a JSON object of the §3 fields (see `examples/config.json`).
**Money fields** (`materiality_floor`) should be JSON **strings** (``"1000.00"``)
so they coerce to an exact `Decimal`; the categorize→propose boundary that #2
relies on is ``confidence_thresholds.categorize`` (see
`BookkeeperConfig.categorize_threshold`).
"""

from __future__ import annotations

import json
from pathlib import Path

from bookkeeper.config import BookkeeperConfig


def load_config(path: str | Path) -> BookkeeperConfig:
    """Load and validate a `BookkeeperConfig` from a JSON config file.

    Propagates the framework's `ConfigError` (fail-fast, all missing fields at
    once) unchanged — a misconfigured instance fails clearly here at load rather
    than deep in a later run.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return BookkeeperConfig.from_mapping(data)
