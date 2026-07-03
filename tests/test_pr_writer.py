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


def test_pr_body_claims_are_derived_from_rendered_inventory():
    intent = InfrastructureIntent(
        raw_request="Create an Aurora data platform",
        resource_type="aurora_postgresql",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
    )
    plan = ChangePlan(
        stack_name="data-platform",
        environments=["non-prod"],
        files_to_generate=["modules/data-platform/main.tf"],
        backend_resources={
            "non-prod": BackendResource(bucket="iac-smith-state", lock_table="iac-smith-lock")
        },
        summary=["Planned intent that must NOT be echoed as implemented."],
    )
    generated_files = {
        "modules/data-platform/main.tf": (
            'resource "aws_rds_cluster" "this" {\n  engine = "aurora-postgresql"\n}\n'
        ),
        "bootstrap/backend/non-prod/main.tf": (
            'resource "aws_s3_bucket" "terraform_state" {\n  bucket = "b"\n}\n'
        ),
        "environments/non-prod/data-platform/terragrunt.hcl": "include {}\n",
    }

    body = build_pr_body(
        issue_url="https://github.com/time4116/iac-smith/issues/59",
        intent=intent,
        change_plan=plan,
        validation=ValidationResult(status=ValidationStatus.PASSED),
        generated_files=generated_files,
    )

    assert "## Generated resources" in body
    assert "`aws_rds_cluster.this`" in body
    assert "`aws_s3_bucket.terraform_state`" in body
    assert "1 provider resource(s) rendered" in body
    assert "Planned intent that must NOT be echoed" not in body
    # File claims come from what was actually rendered, not the plan.
    assert "`environments/non-prod/data-platform/terragrunt.hcl`" in body
    # Scope monitoring is computed from the rendered files, not planned intent.
    assert "## Scope Monitoring" in body
    assert "Files created or changed: 3" in body
    assert "Workload provider resources: 1 across 1 file(s)" in body
    assert "Backend bootstrap resources: 1" in body
    assert "Module directories: `modules/data-platform`" in body
    assert "Environment stacks: `environments/non-prod/data-platform`" in body


def test_pr_body_says_structure_only_validation_scope_when_placeholder():
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
        backend_resources={"non-prod": BackendResource(bucket="b", lock_table="l")},
        summary=[],
    )

    body = build_pr_body(
        issue_url="https://github.com/time4116/iac-smith/issues/1",
        intent=intent,
        change_plan=plan,
        validation=ValidationResult(status=ValidationStatus.PASSED),
        runtime_checks=["terragrunt validate passed."],
        structure_only=True,
        generated_files={"modules/example/main.tf": "# placeholder\n"},
    )

    assert "structural placeholders" in body
    assert "No provider resources were generated outside the backend bootstrap" in body
