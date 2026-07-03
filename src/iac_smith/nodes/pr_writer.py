import re

from iac_smith.legitimacy import resource_inventory
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


def _checks(items: list[str]) -> str:
    if not items:
        return "None."
    return "\n".join(f"- ✅ {item}" for item in items)


def _structure_only_warnings(structure_only: bool) -> list[str]:
    if not structure_only:
        return []
    return [
        "Structure-only PR: the spec renderer selected no provider resources, "
        "so generated module bodies are placeholders until generic "
        "registry/module or provider-schema composition is implemented."
    ]


def _split_inventory(
    generated_files: dict[str, str],
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]]]:
    inventory = resource_inventory(generated_files)
    workload = {p: r for p, r in inventory.items() if not p.startswith("bootstrap/")}
    backend = {p: r for p, r in inventory.items() if p.startswith("bootstrap/")}
    return workload, backend


def _inventory_summary(generated_files: dict[str, str]) -> list[str]:
    """Summary bullets computed from the rendered Terraform, never from intent."""
    workload, backend = _split_inventory(generated_files)
    workload_count = sum(len(records) for records in workload.values())
    backend_count = sum(len(records) for records in backend.values())
    lines: list[str] = []
    if workload_count:
        lines.append(
            f"{workload_count} provider resource(s) rendered across "
            f"{len(workload)} module/stack file(s)"
        )
    else:
        lines.append(
            "No provider resources were generated outside the backend bootstrap — "
            "module bodies are structural placeholders"
        )
    if backend_count:
        lines.append(f"{backend_count} backend bootstrap resource(s) for Terraform state")
    return lines


def _resource_listing(generated_files: dict[str, str]) -> str:
    inventory = resource_inventory(generated_files)
    if not inventory:
        return "None — no `resource` blocks exist in the rendered Terraform."
    return "\n".join(
        f"* `{path}`: " + ", ".join(f"`{rtype}.{rname}`" for rtype, rname in records)
        for path, records in inventory.items()
    )


def build_pr_body(
    issue_url: str,
    intent: InfrastructureIntent,
    change_plan: ChangePlan,
    validation: ValidationResult,
    runtime_checks: list[str] | None = None,
    structure_only: bool = False,
    generated_files: dict[str, str] | None = None,
) -> str:
    # Every claim below is derived from what was actually rendered whenever the
    # rendered files are available; the planned file list is only the fallback
    # for legacy callers. Summarizing parsed intent as if it were implemented is
    # exactly the failure mode the legitimacy gate exists to block.
    if generated_files is not None:
        summary = _inventory_summary(generated_files)
        changed_files = "\n".join(f"* `{path}`" for path in sorted(generated_files))
        resources_section = f"\n## Generated resources\n\n{_resource_listing(generated_files)}\n"
    else:
        summary = change_plan.summary
        changed_files = "\n".join(f"* `{path}`" for path in change_plan.files_to_generate)
        resources_section = ""
    backend_lines = "\n".join(
        f"* `{env}`: S3 `{resource.bucket}`, DynamoDB `{resource.lock_table}`"
        for env, resource in change_plan.backend_resources.items()
    )
    validation_block = f"**Security review**\n\n{_checks(validation.checks)}"
    if runtime_checks:
        validation_block += (
            "\n\n**Terraform / Terragrunt validation**\n\n"
            "IaC Smith ran these commands locally before opening this PR:\n\n"
            f"{_checks(runtime_checks)}"
        )
    if structure_only:
        validation_block += (
            "\n\n**Validation scope**: these checks ran against structural placeholders "
            "only — no workload provider resources were validated."
        )
    warnings = [
        *intent.warnings,
        *validation.warnings,
        *validation.structural,
        *_structure_only_warnings(structure_only),
    ]
    return f"""## Source issue

{issue_url}

## Generated infrastructure summary

{_bullets(summary)}

Target environments: {", ".join(change_plan.environments)}
Region: `{intent.region}`
Stack: `{change_plan.stack_name}`
{resources_section}
## Assumptions and defaults

{_bullets(intent.assumptions)}

## Files created or changed

{changed_files}

## Backend resources

{backend_lines}

## Validation results

{validation_block}

## Warnings and risks

{_bullets(warnings)}

## Iterating on this infrastructure

To add to or modify this infrastructure, create a new GitHub issue in the controller repository
labeled `iac-smith` describing the change. IaC Smith reads existing files in the target repo
before generating anything — follow-on PRs build on what was already merged.

## Before you apply

This infrastructure was generated by an LLM. Review every file in this PR — the
Terraform/Terragrunt source, the workflows, and the backend configuration — before merging.
IaC Smith did not apply anything; nothing reaches AWS until you merge and approve the
apply workflow's environment gate.
"""
