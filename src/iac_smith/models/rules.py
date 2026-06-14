from enum import StrEnum

from pydantic import BaseModel


class RuleSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    PREFERENCE = "preference"


class Rule(BaseModel):
    id: str
    severity: RuleSeverity
    description: str
    category: str


class Ruleset(BaseModel):
    rules: list[Rule]

    @property
    def error_count(self) -> int:
        return sum(rule.severity == RuleSeverity.ERROR for rule in self.rules)

    @property
    def warning_count(self) -> int:
        return sum(rule.severity == RuleSeverity.WARNING for rule in self.rules)

    @property
    def preference_count(self) -> int:
        return sum(rule.severity == RuleSeverity.PREFERENCE for rule in self.rules)
