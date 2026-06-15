from pathlib import Path

import yaml
from pydantic import ValidationError

from iac_smith.models.rules import Rule, Ruleset

# Default rules directory relative to the package root, used when no explicit path is given.
_DEFAULT_RULES_DIR = Path(__file__).resolve().parents[2] / "rules"


def load_ruleset(rules_dir: Path | None = None) -> Ruleset:
    """Load all *.yaml rule files from rules_dir.

    Falls back to the bundled rules/ directory at the repository root when
    rules_dir is None. Returns an empty Ruleset (not an error) when the
    directory does not exist, so the graph can proceed without rules on a
    fresh clone that hasn't committed any rule files yet.
    """
    path = rules_dir if rules_dir is not None else _DEFAULT_RULES_DIR
    if not path.exists():
        return Ruleset(rules=[])

    rules: list[Rule] = []
    for rule_file in sorted(path.glob("*.yaml")):
        data = yaml.safe_load(rule_file.read_text(encoding="utf-8")) or {}
        category = rule_file.stem
        for raw_rule in data.get("rules", []):
            try:
                rules.append(Rule(category=category, **raw_rule))
            except ValidationError as exc:
                raise ValueError(
                    f"Invalid rule in {rule_file}: {raw_rule.get('severity')}"
                ) from exc
    return Ruleset(rules=rules)
