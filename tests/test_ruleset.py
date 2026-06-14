from pathlib import Path

import pytest

from iac_smith.models.rules import RuleSeverity
from iac_smith.nodes.ruleset_loader import load_ruleset


def test_load_ruleset_normalizes_severity_and_counts_rules():
    ruleset = load_ruleset(Path("rules"))

    assert ruleset.error_count >= 1
    assert ruleset.warning_count >= 1
    assert ruleset.preference_count >= 1
    assert {rule.severity for rule in ruleset.rules} >= {
        RuleSeverity.ERROR,
        RuleSeverity.WARNING,
        RuleSeverity.PREFERENCE,
    }


def test_load_ruleset_rejects_unknown_severity(tmp_path):
    rules_file = tmp_path / "bad.yaml"
    rules_file.write_text(
        "rules:\n  - id: bad-rule\n    severity: fatal\n    description: Invalid severity\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fatal"):
        load_ruleset(tmp_path)
