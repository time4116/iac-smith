import re

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.validation import ValidationResult


def branch_name_for_issue(issue_number: int, issue_title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", issue_title.lower()).strip("-")[:48]
    return f"iac-smith/issue-{issue_number}-{slug}"


def _bullets(items: list[str]) -> str:
    if not items:
        return "None."
    return "\n".join(f"* {item}" for item in items)


def build_pr_body(
    issue_url: str,
    intent: InfrastructureIntent,
    change_plan: ChangePlan,
    validation: ValidationResult,
) -> str:
    changed_files = "\n".join(f"* `{path}`" for path in change_plan.files_to_generate)
    backend_lines = "\n".join(
        f"* `{env}`: S3 `{resource.bucket}`, DynamoDB `{resource.lock_table}`"
        for env, resource in change_plan.backend_resources.items()
    )
    return f"""## Source issue

{issue_url}

## Generated infrastructure summary

{_bullets(change_plan.summary)}

Target environments: {", ".join(change_plan.environments)}
Region: `{intent.region}`
Stack: `{change_plan.stack_name}`

## Assumptions and defaults

{_bullets(intent.assumptions)}

## Files created or changed

{changed_files}

## Backend resources

{backend_lines}

## Validation results

Status: `{validation.status.value}`

{_bullets(validation.checks)}

## Warnings and risks

{_bullets([*intent.warnings, *validation.warnings, *validation.structural])}

## Iterating on this infrastructure

To add to or modify this infrastructure, create a new GitHub issue in the controller repository
labeled `iac-smith` describing the change. IaC Smith reads existing files in the target repo
before generating anything — follow-on PRs build on what was already merged.

## Expected post-merge apply behavior

The target repository apply workflow is expected to run after merge to `main`, configure AWS
credentials with GitHub Actions OIDC, run validation and plan, then apply only the generated
or changed live path.

## No apply confirmation

IaC Smith did not apply infrastructure from the controller repository.
"""
