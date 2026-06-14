from pathlib import Path

import yaml
from pydantic import ValidationError

from iac_smith.models.rules import Rule, Ruleset


def load_ruleset(rules_dir: Path) -> Ruleset:
    rules: list[Rule] = []
    for path in sorted(rules_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        category = path.stem
        for raw_rule in data.get("rules", []):
            try:
                rules.append(Rule(category=category, **raw_rule))
            except ValidationError as exc:
                raise ValueError(f"Invalid rule in {path}: {raw_rule.get('severity')}") from exc
    return Ruleset(rules=rules)
