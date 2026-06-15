from typing import TypedDict

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.rules import Ruleset
from iac_smith.models.validation import ValidationResult


class IaCSmithState(TypedDict, total=False):
    issue_number: int
    issue_title: str
    issue_body: str
    issue_url: str
    labels: list[str]
    target_repo: str
    target_repo_path: str
    intent: InfrastructureIntent
    ruleset: Ruleset
    repo_patterns: RepoPatterns
    change_plan: ChangePlan
    generated_files: dict[str, str]
    validation: ValidationResult
    pr_body: str | None
    status: str
    block_reason: str | None
    repair_attempts: int
