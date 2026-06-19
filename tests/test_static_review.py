"""Tests for static review — especially cross-file duplicate detection."""

from __future__ import annotations

from iac_smith.models.validation import ValidationStatus
from iac_smith.nodes.static_review import (
    _find_cross_file_duplicates,
    _find_duplicate_named_resources,
    _find_redacted_placeholders,
    _find_singleton_resource_duplication,
    _find_terragrunt_dependency_output_mismatches,
    _find_terragrunt_missing_required_inputs,
    _find_terragrunt_orphaned_locals,
    _find_undeclared_variable_references,
    static_review_generated_files,
)


class TestCrossFileDuplicates:
    def test_no_duplicates_passes(self) -> None:
        files = {
            "modules/ecs-fargate/main.tf": (
                'resource "aws_vpc" "this" { cidr_block = "10.0.0.0/16" }'
            ),
            "modules/ecs-fargate/variables.tf": 'variable "vpc_id" { type = string }',
            "modules/ecs-fargate/outputs.tf": 'output "vpc_id" { value = aws_vpc.this.id }',
            "modules/ecs-fargate/versions.tf": (
                'terraform {\n  required_providers { aws = { source = "hashicorp/aws" } }\n}'
            ),
        }
        assert _find_cross_file_duplicates(files) == []

    def test_duplicate_variable_across_files(self) -> None:
        files = {
            "modules/ecs-fargate/main.tf": 'variable "vpc_id" { type = string }',
            "modules/ecs-fargate/variables.tf": 'variable "vpc_id" { type = string }',
        }
        errors = _find_cross_file_duplicates(files)
        assert len(errors) == 1
        assert 'Variable "vpc_id"' in errors[0]
        assert "modules/ecs-fargate" in errors[0]
        assert "Remove from" in errors[0]
        assert "keep in" in errors[0]

    def test_duplicate_output_across_files(self) -> None:
        files = {
            "modules/ecs-fargate/main.tf": (
                'output "cluster_arn" { value = aws_ecs_cluster.this.arn }'
            ),
            "modules/ecs-fargate/outputs.tf": (
                'output "cluster_arn" { value = aws_ecs_cluster.this.arn }'
            ),
        }
        errors = _find_cross_file_duplicates(files)
        assert len(errors) == 1
        assert 'Output "cluster_arn"' in errors[0]

    def test_duplicate_required_providers(self) -> None:
        provider_block = (
            'terraform {\n  required_providers { aws = { source = "hashicorp/aws" } }\n}'
        )
        files = {
            "modules/ecs-fargate/main.tf": provider_block,
            "modules/ecs-fargate/versions.tf": provider_block,
        }
        errors = _find_cross_file_duplicates(files)
        assert len(errors) == 1
        assert "required_providers" in errors[0]

    def test_multiple_modules_not_confused(self) -> None:
        """Two different modules can share the same variable names."""
        files = {
            "modules/foundation/main.tf": 'variable "vpc_id" { type = string }',
            "modules/foundation/variables.tf": "",
            "modules/ecs-fargate/variables.tf": 'variable "vpc_id" { type = string }',
            "modules/ecs-fargate/main.tf": "",
        }
        errors = _find_cross_file_duplicates(files)
        assert errors == []

    def test_non_module_files_ignored(self) -> None:
        """Files outside modules/ should not trigger false positives."""
        files = {
            "README.md": "just docs",
            "environments/non-prod/terragrunt.hcl": ('variable "region" { default = "us-west-2" }'),
        }
        errors = _find_cross_file_duplicates(files)
        assert errors == []


class TestUndeclaredVariableReferences:
    def test_var_referenced_and_declared_passes(self) -> None:
        files = {
            "modules/ecs-fargate/main.tf": "locals { prefix = var.name_prefix }",
            "modules/ecs-fargate/variables.tf": ('variable "name_prefix" { type = string }'),
        }
        assert _find_undeclared_variable_references(files) == []

    def test_var_referenced_but_not_declared(self) -> None:
        main_tf = 'resource "aws_vpc" "this" { tags = { Name = var.name_prefix } }'
        files = {
            "modules/ecs-fargate/main.tf": main_tf,
            "modules/ecs-fargate/variables.tf": 'variable "vpc_id" { type = string }',
        }
        errors = _find_undeclared_variable_references(files)
        assert len(errors) == 1
        assert "name_prefix" in errors[0]
        assert "var.name_prefix" in errors[0]
        assert "is not declared" in errors[0]
        assert "Add variable" in errors[0]
        assert "variables.tf" in errors[0]

    def test_var_referenced_in_outputs(self) -> None:
        """var references in outputs.tf should also be checked."""
        files = {
            "modules/ecs-fargate/main.tf": 'resource "aws_vpc" "this" {}',
            "modules/ecs-fargate/outputs.tf": ('output "vpc_id" { value = var.custom_id }'),
            "modules/ecs-fargate/variables.tf": 'variable "vpc_id" { type = string }',
        }
        errors = _find_undeclared_variable_references(files)
        assert any("custom_id" in e for e in errors)

    def test_multiple_vars_missing(self) -> None:
        files = {
            "modules/ecs-fargate/main.tf": ('resource "x" "y" { a = var.foo; b = var.bar }'),
            "modules/ecs-fargate/variables.tf": 'variable "other" { type = string }',
        }
        errors = _find_undeclared_variable_references(files)
        assert len(errors) == 2
        assert any("foo" in e for e in errors)
        assert any("bar" in e for e in errors)

    def test_non_module_files_ignored(self) -> None:
        files = {
            "environments/non-prod/terragrunt.hcl": ("inputs = { name = var.region }"),
        }
        assert _find_undeclared_variable_references(files) == []

    def test_var_declared_only_in_main_tf_flagged_when_variables_tf_exists(self) -> None:
        """A var declared only in main.tf is treated as undeclared when variables.tf exists.

        The LLM sometimes puts variable blocks in main.tf that aren't in variables.tf.
        They pass the duplicate check (only one file has them) but when main.tf is
        repaired the variable blocks are removed, making the var truly undeclared.
        Catching this in the first review gives the repair prompt the right context.
        """
        files = {
            "modules/foundation/main.tf": (
                'variable "name_prefix" { type = string }\n'
                'resource "aws_vpc" "this" { tags = { Name = var.name_prefix } }\n'
            ),
            "modules/foundation/variables.tf": 'variable "vpc_cidr" { type = string }',
        }
        errors = _find_undeclared_variable_references(files)
        assert any("name_prefix" in e for e in errors)

    def test_var_declared_only_in_main_tf_valid_when_no_variables_tf(self) -> None:
        """Without a variables.tf, variable declarations in main.tf are valid."""
        files = {
            "modules/foundation/main.tf": (
                'variable "name_prefix" { type = string }\n'
                'resource "aws_vpc" "this" { tags = { Name = var.name_prefix } }\n'
            ),
        }
        errors = _find_undeclared_variable_references(files)
        assert errors == []

    def test_location_deduplication(self) -> None:
        """Each file is listed only once even if var is referenced multiple times."""
        files = {
            "modules/ecs-fargate/main.tf": (
                'resource "a" "b" { x = var.env; y = var.env; z = var.env }'
            ),
            "modules/ecs-fargate/variables.tf": "",
        }
        errors = _find_undeclared_variable_references(files)
        assert len(errors) == 1
        assert errors[0].count("modules/ecs-fargate/main.tf") == 1


class TestStaticReviewIntegration:
    def test_duplicate_variable_is_structural_not_blocking(self) -> None:
        """Cross-file duplicates are structural: surfaced and autofixed, not blocking.

        Real `terraform validate` catches a duplicate variable declaration, so it
        belongs in the structural tier (PARTIAL) rather than blocking PR creation.
        """
        main_tf = 'variable "vpc_id" { type = string }\nresource "null_resource" "x" {}'
        files = {
            "modules/ecs-fargate/main.tf": main_tf,
            "modules/ecs-fargate/variables.tf": 'variable "vpc_id" { type = string }',
        }
        result = static_review_generated_files(files)
        assert result.status == ValidationStatus.PARTIAL
        assert not result.errors
        assert any("vpc_id" in s for s in result.structural)
        assert any("Variable" in s for s in result.structural)

    def test_clean_module_passes(self) -> None:
        main_tf = 'resource "aws_vpc" "this" { cidr_block = "10.0.0.0/16" }'
        files = {
            "modules/ecs-fargate/main.tf": main_tf,
            "modules/ecs-fargate/variables.tf": 'variable "vpc_id" { type = string }',
            "modules/ecs-fargate/outputs.tf": 'output "vpc_id" { value = aws_vpc.this.id }',
            "modules/ecs-fargate/versions.tf": (
                'terraform {\n  required_providers { aws = { source = "hashicorp/aws" } }\n}'
            ),
        }
        result = static_review_generated_files(files)
        assert result.status == ValidationStatus.PASSED
        assert any("Static security review passed" in c for c in result.checks)

    def test_warnings_produce_partial_status_with_check_entry(self) -> None:
        files = {
            "modules/ecs-fargate/main.tf": (
                'resource "aws_vpc" "this" { cidr_block = "10.0.0.0/16" }'
            ),
            "modules/ecs-fargate/README.md": "No terraform-docs markers here.",
        }
        result = static_review_generated_files(files)
        assert result.status == ValidationStatus.PARTIAL
        assert result.warnings
        assert any("Static security review passed" in c for c in result.checks)


class TestRedactedPlaceholders:
    def test_redacted_placeholder_in_workflow(self) -> None:
        files = {
            ".github/workflows/terraform-apply.yml": (
                'run: curl -H "Authorization: Bearer *** https://api.github.com/repos/example'
            ),
        }
        errors = _find_redacted_placeholders(files)
        assert len(errors) == 1
        assert "***" in errors[0]
        assert "terraform-apply.yml" in errors[0]

    def test_clean_workflow_no_false_positive(self) -> None:
        files = {
            ".github/workflows/terraform-apply.yml": (
                'run: curl -H "Authorization: Bearer *** github.token }}"'
                " https://api.github.com/repos/example"
            ),
        }
        assert _find_redacted_placeholders(files) == []

    def test_non_yaml_file_ignored(self) -> None:
        files = {"modules/network/main.tf": "# *** just a comment"}
        assert _find_redacted_placeholders(files) == []


class TestTerragruntOrphanedLocals:
    def test_child_references_undeclared_local(self) -> None:
        files = {
            "environments/staging/rds-postgres/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders()\n}\n'
                "inputs = { env = local.environment }\n"
            ),
        }
        errors = _find_terragrunt_orphaned_locals(files)
        assert any("local.environment" in e for e in errors)
        assert any("environments/staging/rds-postgres/terragrunt.hcl" in e for e in errors)

    def test_root_config_no_include_no_error(self) -> None:
        files = {
            "environments/terragrunt.hcl": (
                'locals {\n  environment = "non-prod"\n}\n'
                "remote_state {\n"
                '  config = { key = "${local.environment}/terraform.tfstate" }\n'
                "}\n"
            ),
        }
        assert _find_terragrunt_orphaned_locals(files) == []

    def test_child_with_declared_local_ok(self) -> None:
        files = {
            "environments/prod/s3-backend/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders()\n}\n'
                'locals {\n  environment = "prod"\n}\n'
                "inputs = { env = local.environment }\n"
            ),
        }
        assert _find_terragrunt_orphaned_locals(files) == []

    def test_each_missing_local_reported_once(self) -> None:
        files = {
            "environments/dev/lambda-api/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders()\n}\n'
                "inputs = {\n"
                "  env    = local.environment\n"
                "  region = local.aws_region\n"
                "  env2   = local.environment\n"
                "}\n"
            ),
        }
        errors = _find_terragrunt_orphaned_locals(files)
        env_errors = [e for e in errors if "local.environment" in e]
        assert len(env_errors) == 1
        assert any("local.aws_region" in e for e in errors)

    def test_local_placeholders_in_comments_are_ignored(self) -> None:
        files = {
            "environments/non-prod/ecs-fargate-stack/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders()\n}\n'
                "# parent locals are not available as local.xxx here\n"
                "// repair hint: do not use local.aws_region unless declared\n"
                "/* block comment mentioning local.environment */\n"
                'inputs = { name = "ecs" }\n'
            ),
        }

        assert _find_terragrunt_orphaned_locals(files) == []

    def test_local_refs_inside_strings_still_checked(self) -> None:
        files = {
            "environments/non-prod/ecs-fargate-stack/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders()\n}\n'
                'inputs = { name = "${local.environment}-ecs" }\n'
            ),
        }

        errors = _find_terragrunt_orphaned_locals(files)
        assert any("local.environment" in e for e in errors)


class TestTerragruntNestedLocalReferences:
    def test_nested_local_references_do_not_require_nested_local_names(self) -> None:
        # A local referenced inside a nested input map (tags) is satisfied by the
        # top-level locals block; it must not be flagged as an orphaned local.
        files = {
            "environments/non-prod/ecs-fargate-stack/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders()\n}\n'
                'locals {\n  environment = "non-prod"\n}\n'
                "inputs = {\n"
                "  tags = {\n"
                "    Environment = local.environment\n"
                "  }\n"
                "}\n"
            ),
        }

        assert _find_terragrunt_orphaned_locals(files) == []


class TestTerragruntMissingRequiredInputs:
    def test_required_module_variable_not_passed_by_stack(self) -> None:
        files = {
            "environments/non-prod/ecs-fargate-stack/terragrunt.hcl": (
                'terraform {\n  source = "../../../modules//ecs-fargate-stack"\n}\n'
                "inputs = {\n  environment = local.environment\n}\n"
            ),
            "modules/ecs-fargate-stack/variables.tf": (
                'variable "environment" { type = string }\n'
                'variable "aws_region" { type = string }\n'
                'variable "container_image" { type = string }\n'
            ),
        }

        errors = _find_terragrunt_missing_required_inputs(files)

        assert any("required input `aws_region`" in e for e in errors)
        assert any("required input `container_image`" in e for e in errors)
        assert all("required input `environment`" not in e for e in errors)

    def test_variable_with_default_not_required_from_stack(self) -> None:
        files = {
            "environments/non-prod/ecs-fargate-stack/terragrunt.hcl": (
                'terraform {\n  source = "../../../modules//ecs-fargate-stack"\n}\n'
                "inputs = { environment = local.environment }\n"
            ),
            "modules/ecs-fargate-stack/variables.tf": (
                'variable "environment" { type = string }\n'
                'variable "container_image" {\n'
                "  type    = string\n"
                '  default = "nginx:latest"\n'
                "}\n"
            ),
        }

        assert _find_terragrunt_missing_required_inputs(files) == []


class TestTerragruntDependencyOutputMismatches:
    def test_dependency_output_reference_missing_from_module_outputs(self) -> None:
        files = {
            "environments/non-prod/rds-postgres/terragrunt.hcl": (
                'dependency "foundation" {\n  config_path = "../foundation"\n}\n'
                "inputs = {\n"
                "  vpc_id = dependency.foundation.outputs.vpc_id\n"
                "  database_subnet_ids = dependency.foundation.outputs.database_subnet_ids\n"
                "}\n"
            ),
            "modules/foundation/outputs.tf": (
                'output "vpc_id" { value = aws_vpc.main.id }\n'
                'output "private_subnet_ids" { value = aws_subnet.private[*].id }\n'
            ),
        }

        errors = _find_terragrunt_dependency_output_mismatches(files)

        assert any("database_subnet_ids" in e for e in errors)
        assert any("modules/foundation/outputs.tf" in e for e in errors)
        assert all("vpc_id" not in e for e in errors)

    def test_dependency_output_reference_declared_ok(self) -> None:
        files = {
            "environments/non-prod/ecs-fargate-stack/terragrunt.hcl": (
                'dependency "foundation" {\n  config_path = "../foundation"\n}\n'
                "inputs = { vpc_id = dependency.foundation.outputs.vpc_id }\n"
            ),
            "modules/foundation/outputs.tf": 'output "vpc_id" { value = aws_vpc.main.id }',
        }

        assert _find_terragrunt_dependency_output_mismatches(files) == []


class TestDuplicateNamedResources:
    def test_duplicate_provider_names_across_arbitrary_modules(self) -> None:
        examples = [
            ("aws_security_group", "api", "${var.environment}-shared-api"),
            ("aws_iam_role", "processor", "${var.environment}-worker-role"),
            ("aws_cloudwatch_log_group", "logs", "/aws/app/shared"),
            ("aws_lb_target_group", "http", "${var.environment}-http-tg"),
        ]

        for resource_type, resource_name, provider_name in examples:
            files = {
                "modules/service-a/main.tf": (
                    f'resource "{resource_type}" "{resource_name}" {{\n'
                    f'  name = "{provider_name}"\n'
                    "}\n"
                ),
                "modules/service-b/main.tf": (
                    f'resource "{resource_type}" "{resource_name}" {{\n'
                    f'  name = "{provider_name}"\n'
                    "}\n"
                ),
            }

            errors = _find_duplicate_named_resources(files)

            assert len(errors) == 1
            assert resource_type in errors[0]
            assert provider_name in errors[0]
            assert "modules/service-a/main.tf" in errors[0]
            assert "modules/service-b/main.tf" in errors[0]

    def test_distinct_provider_names_allowed_across_arbitrary_modules(self) -> None:
        files = {
            "modules/service-a/main.tf": (
                'resource "aws_security_group" "api" {\n  name = "${var.environment}-api-sg"\n}\n'
            ),
            "modules/service-b/main.tf": (
                'resource "aws_security_group" "worker" {\n'
                '  name = "${var.environment}-worker-sg"\n'
                "}\n"
            ),
        }

        assert _find_duplicate_named_resources(files) == []

    def test_duplicate_named_resources_block_review_for_any_stack_shape(self) -> None:
        files = {
            "modules/api/main.tf": (
                'resource "aws_iam_role" "runtime" {\n'
                '  name = "${var.environment}-runtime-role"\n'
                "}\n"
            ),
            "modules/batch/main.tf": (
                'resource "aws_iam_role" "runtime" {\n'
                '  name = "${var.environment}-runtime-role"\n'
                "}\n"
            ),
            "modules/api/variables.tf": 'variable "environment" { type = string }',
            "modules/batch/variables.tf": 'variable "environment" { type = string }',
        }

        result = static_review_generated_files(files)

        assert result.status == ValidationStatus.FAILED
        assert any("duplicate provider name" in e for e in result.errors)
        assert not result.structural


class TestSingletonResourceDuplication:
    def test_vpc_in_multiple_modules_fails_review(self) -> None:
        """aws_vpc in two unrelated modules signals a broken foundation boundary."""
        files = {
            "modules/network/main.tf": ('resource "aws_vpc" "this" { cidr_block = "10.0.0.0/16" }'),
            "modules/rds-postgres/main.tf": (
                'resource "aws_vpc" "this" { cidr_block = "10.1.0.0/16" }'
            ),
        }
        errors = _find_singleton_resource_duplication(files)
        assert any("aws_vpc" in e for e in errors)
        assert any("`modules/network`" in e for e in errors)
        assert any("`modules/rds-postgres`" in e for e in errors)

    def test_static_review_warns_on_vpc_in_foundation_and_workload(self) -> None:
        # Two modules each declaring aws_vpc is a broken foundation boundary, but
        # both validate fine in isolation — Terraform won't catch it. It is an
        # advisory warning for the reviewer, not a blocking error.
        files = {
            "modules/foundation/main.tf": (
                'resource "aws_vpc" "this" { cidr_block = "10.0.0.0/16" }'
            ),
            "modules/ecs-fargate-stack/main.tf": (
                'resource "aws_vpc" "this" { cidr_block = "10.1.0.0/16" }'
            ),
        }

        result = static_review_generated_files(files)

        assert result.status == ValidationStatus.PARTIAL
        assert not result.errors
        assert any("aws_vpc" in w for w in result.warnings)

    def test_vpc_in_one_module_ok(self) -> None:
        files = {
            "modules/network/main.tf": ('resource "aws_vpc" "this" { cidr_block = "10.0.0.0/16" }'),
            "modules/lambda-api/main.tf": (
                'resource "aws_lambda_function" "this" { function_name = "api" }'
            ),
        }
        assert _find_singleton_resource_duplication(files) == []

    def test_non_singleton_type_not_flagged(self) -> None:
        """aws_security_group legitimately appears in many modules."""
        files = {
            "modules/network/main.tf": ('resource "aws_security_group" "this" { name = "sg-a" }'),
            "modules/rds-postgres/main.tf": (
                'resource "aws_security_group" "this" { name = "sg-b" }'
            ),
        }
        assert _find_singleton_resource_duplication(files) == []

