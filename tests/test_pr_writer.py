from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.validation import ValidationResult, ValidationStatus
from iac_smith.nodes.pr_writer import build_pr_body


def test_pr_body_uses_planned_environment_names_when_repo_patterns_override_intent():
    intent = InfrastructureIntent(
        raw_request="Create a VPC foundation",
        resource_type="vpc_foundation",
        environment_scope=EnvironmentScope.BOTH,
        environments=["non-prod", "prod"],
        region="us-west-2",
    )
    plan = ChangePlan(
        stack_name="vpc",
        environments=["dev", "staging", "prod"],
        files_to_generate=["environments/dev/vpc/terragrunt.hcl"],
        backend_resources={
            "dev": BackendResource(bucket="iac-smith-dev-tfstate", lock_table="iac-smith-dev-lock")
        },
        summary=["Generated VPC foundation."],
    )
    validation = ValidationResult(status=ValidationStatus.PASSED)

    body = build_pr_body(
        issue_url="https://github.com/time4116/iac-smith/issues/1",
        intent=intent,
        change_plan=plan,
        validation=validation,
    )

    assert "Target environments: dev, staging, prod" in body
    assert "Target environments: non-prod, prod" not in body
    # Without runtime checks, only the security review group renders.
    assert "**Security review**" in body
    assert "**Terraform / Terragrunt validation**" not in body


def test_pr_body_surfaces_structure_only_spec_renderer_warning():
    intent = InfrastructureIntent(
        raw_request="Create infrastructure",
        resource_type="example",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
    )
    plan = ChangePlan(
        stack_name="example",
        environments=["non-prod"],
        files_to_generate=["modules/example/main.tf"],
        backend_resources={
            "non-prod": BackendResource(
                bucket="iac-smith-dev-tfstate", lock_table="iac-smith-dev-lock"
            )
        },
        summary=["Generated deterministic structure."],
    )

    body = build_pr_body(
        issue_url="https://github.com/time4116/iac-smith/issues/1",
        intent=intent,
        change_plan=plan,
        validation=ValidationResult(status=ValidationStatus.PASSED),
        structure_only=True,
    )

    assert "Structure-only PR" in body
    assert "selected no provider resources" in body
