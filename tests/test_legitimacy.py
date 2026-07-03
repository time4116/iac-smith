"""Regression tests for the demo-infra PR #65 failure mode (issue #108).

A run whose composed workload was empty opened a PR containing only backend
bootstrap resources while the body claimed a full data platform. Every gate
here exists to make that impossible by default.
"""

from iac_smith.legitimacy import (
    backend_resource_addresses,
    check_pr_legitimacy,
    find_failure_banner,
    resource_inventory,
    workload_resource_addresses,
)
from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent

_BACKEND_MAIN = (
    'resource "aws_s3_bucket" "terraform_state" {\n  bucket = var.state_bucket_name\n}\n\n'
    'resource "aws_dynamodb_table" "terraform_locks" {\n  name = var.state_lock_table_name\n}\n'
)
_PLACEHOLDER_MAIN = (
    "# Deterministic skeleton generated from InfrastructureSpec.\n"
    "# No provider resources were selected for this component.\n"
)


def _intent(**overrides) -> InfrastructureIntent:
    values = {
        "raw_request": "Provision a non-prod Aurora PostgreSQL data platform",
        "resource_type": "aurora_postgresql_data_platform",
        "environment_scope": EnvironmentScope.NON_PROD_ONLY,
        "environments": ["non-prod"],
        "region": "us-west-2",
        "features": ["kms_encryption", "secrets_rotation"],
    }
    values.update(overrides)
    return InfrastructureIntent(**values)


def _plan(stack_name: str = "data-platform") -> ChangePlan:
    return ChangePlan(
        stack_name=stack_name,
        environments=["non-prod"],
        files_to_generate=[
            "README.md",
            "bootstrap/backend/non-prod/main.tf",
            "environments/non-prod/root.hcl",
            f"environments/non-prod/{stack_name}/terragrunt.hcl",
            f"modules/{stack_name}/main.tf",
            f"modules/{stack_name}/variables.tf",
        ],
        backend_resources={
            "non-prod": BackendResource(bucket="iac-smith-state", lock_table="iac-smith-lock")
        },
        summary=["Generate data-platform Terraform/Terragrunt structure"],
    )


def _legitimate_files() -> dict[str, str]:
    return {
        "bootstrap/backend/non-prod/main.tf": _BACKEND_MAIN,
        "modules/data-platform/main.tf": (
            'resource "aws_rds_cluster" "this" {\n'
            '  engine = "aurora-postgresql"\n'
            '  cluster_identifier = "data-platform"\n'
            "}\n\n"
            'resource "aws_kms_key" "this" {\n  enable_key_rotation = true\n}\n'
        ),
        "environments/non-prod/data-platform/terragrunt.hcl": (
            'include "root" {\n  path = find_in_parent_folders("root.hcl")\n}\n'
        ),
    }


def test_resource_inventory_parses_addresses_per_tf_file():
    files = _legitimate_files()

    inventory = resource_inventory(files)

    assert inventory["bootstrap/backend/non-prod/main.tf"] == [
        ("aws_s3_bucket", "terraform_state"),
        ("aws_dynamodb_table", "terraform_locks"),
    ]
    assert ("aws_rds_cluster", "this") in inventory["modules/data-platform/main.tf"]
    assert "environments/non-prod/data-platform/terragrunt.hcl" not in inventory


def test_workload_and_backend_addresses_split_on_bootstrap_prefix():
    files = _legitimate_files()

    assert backend_resource_addresses(files) == [
        "aws_s3_bucket.terraform_state",
        "aws_dynamodb_table.terraform_locks",
    ]
    assert "aws_rds_cluster.this" in workload_resource_addresses(files)
    assert "aws_s3_bucket.terraform_state" not in workload_resource_addresses(files)


def test_legitimate_output_passes_the_gate():
    errors = check_pr_legitimacy(
        generated_files=_legitimate_files(),
        change_plan=_plan(),
        intent=_intent(),
    )

    assert errors == []


def test_backend_only_resources_do_not_satisfy_workload_generation():
    files = {
        "bootstrap/backend/non-prod/main.tf": _BACKEND_MAIN,
        "modules/data-platform/main.tf": "# empty module\n",
    }

    errors = check_pr_legitimacy(generated_files=files, change_plan=_plan(), intent=_intent())

    assert any("Only backend bootstrap resources" in error for error in errors)
    assert any("aws_s3_bucket.terraform_state" in error for error in errors)


def test_structure_only_flag_blocks_by_default_and_passes_with_opt_in():
    files = {"modules/data-platform/main.tf": _PLACEHOLDER_MAIN}

    blocked = check_pr_legitimacy(
        generated_files=files,
        change_plan=_plan(),
        intent=_intent(),
        structure_only=True,
    )
    allowed = check_pr_legitimacy(
        generated_files=files,
        change_plan=_plan(),
        intent=_intent(),
        structure_only=True,
        allow_structure_only=True,
    )

    assert any("Structure-only output" in error for error in blocked)
    assert any("IAC_SMITH_ALLOW_STRUCTURE_ONLY" in error for error in blocked)
    assert allowed == []


def test_failure_banners_in_generated_files_and_pr_body_block():
    files = _legitimate_files()
    files["README.md"] = "Spec composition failed; rendered structure only: boom\n"

    errors = check_pr_legitimacy(
        generated_files=files,
        change_plan=_plan(),
        intent=_intent(),
        pr_body="Everything is fine.\n\nStructure-only PR: placeholders remain.",
    )

    assert any("README.md" in error and "composition failed" in error for error in errors)
    assert any("PR body contains failure banner" in error for error in errors)


def test_requested_resource_class_absent_from_inventory_blocks():
    files = {
        "bootstrap/backend/non-prod/main.tf": _BACKEND_MAIN,
        # Real resources, but nothing resembling the requested Aurora platform.
        "modules/data-platform/main.tf": (
            'resource "null_resource" "noop" {\n  triggers = {}\n}\n'
        ),
    }

    errors = check_pr_legitimacy(generated_files=files, change_plan=_plan(), intent=_intent())

    assert any("requested infrastructure classes" in error for error in errors)


def test_required_existing_dependency_must_be_wired_even_with_opt_in():
    files = _legitimate_files()
    intent = _intent(depends_on_existing=["foundation"])

    unwired = check_pr_legitimacy(
        generated_files=files,
        change_plan=_plan(),
        intent=intent,
        allow_structure_only=True,
    )

    files["environments/non-prod/data-platform/terragrunt.hcl"] += (
        '\ndependency "foundation" {\n  config_path = "../foundation"\n}\n'
    )
    wired = check_pr_legitimacy(generated_files=files, change_plan=_plan(), intent=intent)

    assert any('dependency "foundation"' in error for error in unwired)
    assert wired == []


def test_find_failure_banner_matches_case_insensitively():
    assert find_failure_banner("...Response Was Truncated at the cap...") == (
        "response was truncated"
    )
    assert find_failure_banner("healthy terraform output") is None
