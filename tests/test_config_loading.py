"""Config loading: the sample config loads, and bad config fails fast."""

from __future__ import annotations

import json

import pytest

from bookkeeper.config import ConfigError

from bookkeeper_ui.config_loader import load_config


def test_loads_sample_config(examples_dir):
    """AC: BookkeeperConfig loads from the sample config file."""
    config = load_config(examples_dir / "config.json")
    assert "5000-office-supplies" in config.chart_of_accounts
    assert config.books_location == "local-file-store"
    assert config.attribution_targets == ("target-001", "target-002")


def test_categorize_threshold_available_for_slice2(examples_dir):
    """The categorize→propose boundary #2 relies on is present and live."""
    config = load_config(examples_dir / "config.json")
    assert config.categorize_threshold() == 0.6


def test_owner_category_rule_present(examples_dir):
    """The sample carries a namespaced owner category rule for the categorize skill."""
    config = load_config(examples_dir / "config.json")
    assert config.owner_policies["category:delta airlines"] == "5200-travel"


def test_missing_required_field_raises_config_error(tmp_path):
    """A config missing a required §3 field fails fast with ConfigError."""
    bad = {"chart_of_accounts": ["5000-office-supplies"]}  # missing the rest
    path = tmp_path / "bad_config.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
