from pathlib import Path

import pytest

from iac_smith.models.rules import RuleSeverity
from iac_smith.nodes.ruleset_loader import load_ruleset

_RULES_DIR = Path(__file__).resolve().parents[1] / "rules"


def test_load_ruleset_normalizes_severity_and_counts_rules():
    ruleset = load_ruleset(_RULES_DIR)

    assert ruleset.error_count >= 1
    assert ruleset.warning_count >= 1
    assert ruleset.preference_count >= 1
    assert {rule.severity for rule in ruleset.rules} >= {
        RuleSeverity.ERROR,
        RuleSeverity.WARNING,
        RuleSeverity.PREFERENCE,
    }


def test_foundation_scope_rules_are_machine_loadable():
    ruleset = load_ruleset(_RULES_DIR)
    rules_by_id = {rule.id: rule for rule in ruleset.rules}

    assert "foundation-module-scope" in rules_by_id
    assert "downstream stacks depend on" in rules_by_id["foundation-module-scope"].description
    assert "workload-modules-depend-on-foundation" in rules_by_id
    assert "module-name-scope-alignment" in rules_by_id


def test_load_ruleset_returns_empty_when_directory_does_not_exist(tmp_path):
    ruleset = load_ruleset(tmp_path / "nonexistent")
    assert ruleset.rules == []


def test_load_ruleset_rejects_unknown_severity(tmp_path):
    rules_file = tmp_path / "bad.yaml"
    rules_file.write_text(
        "rules:\n  - id: bad-rule\n    severity: fatal\n    description: Invalid severity\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fatal"):
        load_ruleset(tmp_path)
