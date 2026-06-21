"""Tests for static review — especially cross-file duplicate detection."""

from __future__ import annotations

import pytest

from iac_smith.models.validation import ValidationStatus
from iac_smith.nodes.static_review import (
    _DANGEROUS_PORTS,
    _NAMED_RESOURCE_TYPES,
    _SECURITY_CHECKS_PERFORMED,
    _SINGLETON_RESOURCE_TYPES,
    _apply_workflow_errors,
    _contains_dangerous_public_ingress,
    _find_apprunner_non_ecr_image,
    _find_cloudwatch_logs_kms_without_grant,
    _find_cross_file_duplicates,
    _find_duplicate_named_resources,
    _find_hardcoded_secret_values,
    _find_malformed_terraform_declarations,
    _find_redacted_placeholders,
    _find_singleton_resource_duplication,
    _find_terragrunt_dangling_dependencies,
    _find_terragrunt_dependency_output_mismatches,
    _find_terragrunt_include_cycles,
    _find_terragrunt_missing_required_inputs,
    _find_terragrunt_orphaned_locals,
    _find_terragrunt_required_providers,
    _find_undeclared_variable_references,
    existing_stack_dirs,
    static_review_generated_files,
)


class TestMalformedTerraformDeclarations:
    def test_flags_var_block_typo_in_tf_file(self) -> None:
        files = {
            "modules/dynamodb-table/variables.tf": (
                'variable "table_name" { type = string }\nvar "aws_region" { type = string }\n'
            )
        }

        errors = _find_malformed_terraform_declarations(files)

        assert len(errors) == 1
        assert 'var "aws_region"' in errors[0]
        assert 'variable "aws_region"' in errors[0]

    def test_valid_variable_block_not_flagged(self) -> None:
        files = {"modules/dynamodb-table/variables.tf": 'variable "aws_region" { type = string }\n'}

        assert _find_malformed_terraform_declarations(files) == []


class TestTerragruntIncludeCycles:
    def test_environment_root_must_not_include_itself_with_find_in_parent_folders(self) -> None:
        files = {
            "environments/non-prod/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders()\n}\n'
            ),
            "environments/non-prod/dynamodb-table/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders()\n}\n'
            ),
        }

        errors = _find_terragrunt_include_cycles(files)

        assert len(errors) == 1
        assert "environments/non-prod/terragrunt.hcl" in errors[0]
        assert "includes itself" in errors[0]

    def test_stack_include_parent_with_find_in_parent_folders_is_allowed(self) -> None:
        files = {
            "environments/terragrunt.hcl": "locals { root = true }\n",
            "environments/non-prod/terragrunt.hcl": 'locals { environment = "non-prod" }\n',
            "environments/non-prod/dynamodb-table/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders()\n}\n'
            ),
        }

        assert _find_terragrunt_include_cycles(files) == []

    def test_environment_root_self_include_via_find_in_parent_folders_argument(self) -> None:
        files = {
            "environments/non-prod/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders("terragrunt.hcl")\n}\n'
            ),
        }

        errors = _find_terragrunt_include_cycles(files)

        assert len(errors) == 1
        assert "environments/non-prod/terragrunt.hcl" in errors[0]
        assert "includes itself" in errors[0]

    def test_environment_root_parent_root_hcl_include_is_allowed(self) -> None:
        # Walking up for a *different* file (a real repo root.hcl) is valid.
        files = {
            "environments/non-prod/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders("root.hcl")\n}\n'
            ),
        }

        assert _find_terragrunt_include_cycles(files) == []

    def test_self_include_via_get_terragrunt_dir_is_flagged_in_any_config(self) -> None:
        files = {
            "environments/non-prod/dynamodb-table/terragrunt.hcl": (
                'include "root" {\n  path = "${get_terragrunt_dir()}/terragrunt.hcl"\n}\n'
            ),
        }

        errors = _find_terragrunt_include_cycles(files)

        assert len(errors) == 1
        assert "includes itself" in errors[0]

    def test_relative_parent_include_path_is_allowed(self) -> None:
        # `../terragrunt.hcl` points at a real parent, not the config itself.
        files = {
            "environments/non-prod/dynamodb-table/terragrunt.hcl": (
                'include "root" {\n  path = "../terragrunt.hcl"\n}\n'
            ),
        }

        assert _find_terragrunt_include_cycles(files) == []


class TestAppRunnerImageSource:
    def _service(self, image_line: str) -> str:
        return (
            'resource "aws_apprunner_service" "this" {\n'
            '  service_name = "open-webui"\n'
            "  source_configuration {\n"
            "    image_repository {\n"
            f"      {image_line}\n"
            '      image_repository_type = "ECR_PUBLIC"\n'
            "    }\n"
            "  }\n"
            "}\n"
        )

    def test_ghcr_literal_image_is_blocked(self) -> None:
        files = {
            "modules/app-runner-open-webui/main.tf": self._service(
                'image_identifier = "ghcr.io/open-webui/open-webui:latest"'
            )
        }

        errors = _find_apprunner_non_ecr_image(files)

        assert len(errors) == 1
        assert "ghcr.io/open-webui/open-webui:latest" in errors[0]
        assert "ECR" in errors[0]

    def test_ecr_public_literal_image_is_allowed(self) -> None:
        files = {
            "modules/app-runner-open-webui/main.tf": self._service(
                'image_identifier = "public.ecr.aws/aws-containers/hello:latest"'
            )
        }
        assert _find_apprunner_non_ecr_image(files) == []

    def test_private_ecr_literal_image_is_allowed(self) -> None:
        files = {
            "modules/app-runner-open-webui/main.tf": self._service(
                "image_identifier = "
                '"123456789012.dkr.ecr.us-west-2.amazonaws.com/open-webui:latest"'
            )
        }
        assert _find_apprunner_non_ecr_image(files) == []

    def test_image_from_variable_default_is_resolved_and_blocked(self) -> None:
        files = {
            "modules/app-runner-open-webui/main.tf": self._service(
                "image_identifier = var.image_uri"
            ),
            "modules/app-runner-open-webui/variables.tf": (
                'variable "image_uri" {\n'
                "  type    = string\n"
                '  default = "docker.io/open-webui/open-webui:latest"\n'
                "}\n"
            ),
        }

        errors = _find_apprunner_non_ecr_image(files)

        assert len(errors) == 1
        assert "docker.io/open-webui/open-webui:latest" in errors[0]

    def test_image_from_stack_input_is_resolved_and_blocked(self) -> None:
        files = {
            "modules/app-runner-open-webui/main.tf": self._service(
                "image_identifier = var.image_uri"
            ),
            "modules/app-runner-open-webui/variables.tf": (
                'variable "image_uri" { type = string }\n'
            ),
            "environments/non-prod/app-runner-open-webui/terragrunt.hcl": (
                'terraform {\n  source = "../../../modules/app-runner-open-webui"\n}\n'
                'inputs = {\n  image_uri = "ghcr.io/open-webui/open-webui:latest"\n}\n'
            ),
        }

        errors = _find_apprunner_non_ecr_image(files)

        assert len(errors) == 1
        assert "ghcr.io/open-webui/open-webui:latest" in errors[0]

    def test_interpolated_image_value_is_not_flagged(self) -> None:
        files = {
            "modules/app-runner-open-webui/main.tf": self._service(
                'image_identifier = "${var.registry}/open-webui:latest"'
            )
        }
        assert _find_apprunner_non_ecr_image(files) == []

    def test_non_apprunner_file_with_image_string_is_ignored(self) -> None:
        files = {
            "modules/ecs-fargate/main.tf": (
                'resource "aws_ecs_task_definition" "this" {\n'
                '  container_definitions = "image_identifier = \\"ghcr.io/x/y:latest\\""\n'
                "}\n"
            )
        }
        assert _find_apprunner_non_ecr_image(files) == []

    def test_blocks_pr_via_static_review(self) -> None:
        files = {
            "modules/app-runner-open-webui/main.tf": self._service(
                'image_identifier = "ghcr.io/open-webui/open-webui:latest"'
            )
        }

        result = static_review_generated_files(files)

        assert result.status == ValidationStatus.FAILED
        assert any("App Runner service" in e for e in result.errors)


class TestCloudwatchLogsKmsGrant:
    _LOG_GROUP = (
        'resource "aws_cloudwatch_log_group" "firehose_delivery" {\n'
        '  name       = "/aws/kinesisfirehose/x"\n'
        "  kms_key_id = aws_kms_key.platform_events.arn\n"
        "}\n"
    )

    def test_default_policy_key_is_flagged(self) -> None:
        # The exact PR #51 shape: a CMK with no policy block at all.
        files = {
            "modules/platform-event-ingestion/main.tf": (
                'resource "aws_kms_key" "platform_events" {\n'
                '  description = "platform events"\n'
                "}\n" + self._LOG_GROUP
            )
        }

        findings = _find_cloudwatch_logs_kms_without_grant(files)

        assert len(findings) == 1
        assert "aws_cloudwatch_log_group.firehose_delivery" in findings[0]
        assert "logs.<region>.amazonaws.com" in findings[0]

    def test_inline_policy_granting_logs_is_allowed(self) -> None:
        files = {
            "modules/platform-event-ingestion/main.tf": (
                'resource "aws_kms_key" "platform_events" {\n'
                "  policy = jsonencode({\n"
                "    Statement = [{\n"
                '      Principal = { Service = "logs.us-west-2.amazonaws.com" }\n'
                '      Action    = ["kms:Encrypt", "kms:Decrypt"]\n'
                "    }]\n"
                "  })\n"
                "}\n" + self._LOG_GROUP
            )
        }
        assert _find_cloudwatch_logs_kms_without_grant(files) == []

    def test_separate_key_policy_resource_granting_logs_is_allowed(self) -> None:
        files = {
            "modules/platform-event-ingestion/main.tf": (
                'resource "aws_kms_key" "platform_events" {\n  description = "x"\n}\n'
                + self._LOG_GROUP
            ),
            "modules/platform-event-ingestion/kms.tf": (
                'resource "aws_kms_key_policy" "platform_events" {\n'
                "  key_id = aws_kms_key.platform_events.id\n"
                "  policy = jsonencode({\n"
                '    Statement = [{ Principal = { Service = "logs.amazonaws.com" } }]\n'
                "  })\n"
                "}\n"
            ),
        }
        assert _find_cloudwatch_logs_kms_without_grant(files) == []

    def test_policy_via_data_document_granting_logs_is_allowed(self) -> None:
        files = {
            "modules/platform-event-ingestion/main.tf": (
                'resource "aws_kms_key" "platform_events" {\n'
                "  policy = data.aws_iam_policy_document.kms.json\n"
                "}\n"
                'data "aws_iam_policy_document" "kms" {\n'
                "  statement {\n"
                "    principals {\n"
                '      type        = "Service"\n'
                '      identifiers = ["logs.${data.aws_region.current.name}.amazonaws.com"]\n'
                "    }\n"
                "  }\n"
                "}\n" + self._LOG_GROUP
            )
        }
        assert _find_cloudwatch_logs_kms_without_grant(files) == []

    def test_log_group_without_cmk_is_ignored(self) -> None:
        files = {
            "modules/platform-event-ingestion/main.tf": (
                'resource "aws_kms_key" "platform_events" {\n  description = "x"\n}\n'
                'resource "aws_cloudwatch_log_group" "plain" {\n  name = "/aws/x"\n}\n'
            )
        }
        assert _find_cloudwatch_logs_kms_without_grant(files) == []

    def test_blocks_are_surfaced_as_structural_not_error(self) -> None:
        files = {
            "modules/platform-event-ingestion/main.tf": (
                'resource "aws_kms_key" "platform_events" {\n  description = "x"\n}\n'
                + self._LOG_GROUP
            )
        }

        result = static_review_generated_files(files)

        assert any("CloudWatch Logs service principal" in s for s in result.structural)
        assert not any("CloudWatch Logs service principal" in e for e in result.errors)


class TestTerragruntRequiredProviders:
    def test_required_providers_in_terragrunt_hcl_flagged(self) -> None:
        files = {
            "environments/non-prod/terragrunt.hcl": (
                'generate "provider" {\n'
                '  path     = "provider.tf"\n'
                "  contents = <<EOF\n"
                "terraform {\n  required_providers {\n"
                '    aws = { source = "hashicorp/aws", version = "~> 5.0" }\n'
                "  }\n}\n"
                'provider "aws" { region = "us-east-1" }\n'
                "EOF\n"
                "}\n"
            ),
        }
        errors = _find_terragrunt_required_providers(files)
        assert len(errors) == 1
        assert "Duplicate required providers" in errors[0]

    def test_required_providers_block_makes_review_fail(self) -> None:
        files = {
            "environments/non-prod/terragrunt.hcl": (
                'generate "provider" {\n  contents = <<EOF\nterraform {\n'
                "  required_providers { aws = {} }\n}\nEOF\n}\n"
            ),
        }
        result = static_review_generated_files(files)
        assert result.status == ValidationStatus.FAILED
        assert any("required_providers" in e for e in result.errors)

    def test_provider_only_generate_block_is_clean(self) -> None:
        files = {
            "environments/non-prod/terragrunt.hcl": (
                'generate "provider" {\n  path = "provider.tf"\n  contents = <<EOF\n'
                'provider "aws" {\n  region = "${local.aws_region}"\n}\nEOF\n}\n'
            ),
        }
        assert _find_terragrunt_required_providers(files) == []

    def test_required_providers_in_module_versions_not_flagged(self) -> None:
        # versions.tf is the correct home for required_providers.
        files = {
            "modules/foundation/versions.tf": (
                "terraform {\n  required_providers { aws = {} }\n}\n"
            ),
        }
        assert _find_terragrunt_required_providers(files) == []


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
        # The check list enumerates the actual security checks performed, not a
        # single opaque "passed" line.
        assert result.checks == list(_SECURITY_CHECKS_PERFORMED)
        assert any("hardcoded secrets" in c for c in result.checks)
        assert any("manual-approval" in c for c in result.checks)

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
        assert result.checks == list(_SECURITY_CHECKS_PERFORMED)


class TestRedactedPlaceholders:
    def test_redacted_placeholder_in_workflow(self) -> None:
        files = {
            ".github/workflows/terraform-apply.yml": (
                'run: curl -H "Authorization: Bearer ***" https://api.github.com/repos/example'
            ),
        }
        errors = _find_redacted_placeholders(files)
        assert len(errors) == 1
        assert "***" in errors[0]
        assert "terraform-apply.yml" in errors[0]

    def test_clean_workflow_no_false_positive(self) -> None:
        files = {
            ".github/workflows/terraform-apply.yml": (
                'run: curl -H "Authorization: Bearer ${{ github.token }}"'
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


class TestTerragruntDanglingDependencies:
    def _workload_stack(self) -> str:
        return (
            'dependency "foundation" {\n'
            '  config_path = "../foundation"\n'
            '  mock_outputs = { vpc_id = "vpc-123" }\n'
            "}\n"
            "inputs = {\n"
            "  vpc_id = dependency.foundation.outputs.vpc_id\n"
            "}\n"
        )

    def test_flags_dependency_on_stack_not_in_change(self) -> None:
        # The EB failure: a workload stack depends on a foundation stack that was
        # never generated and does not exist in the repo.
        files = {
            "environments/non-prod/eb-dotnet/terragrunt.hcl": self._workload_stack(),
            "modules/eb-dotnet/main.tf": 'resource "aws_vpc" "x" {}\n',
        }

        errors = _find_terragrunt_dangling_dependencies(files, set())

        assert len(errors) == 1
        assert "environments/non-prod/foundation" in errors[0]
        assert "data sources" in errors[0]

    def test_flags_reference_without_dependency_block(self) -> None:
        files = {
            "environments/non-prod/eb-dotnet/terragrunt.hcl": (
                "inputs = { vpc_id = dependency.foundation.outputs.vpc_id }\n"
            ),
        }

        errors = _find_terragrunt_dangling_dependencies(files, set())

        assert len(errors) == 1
        assert 'declares no `dependency "foundation"` block' in errors[0]

    def test_ok_when_foundation_stack_generated_in_same_change(self) -> None:
        files = {
            "environments/non-prod/eb-dotnet/terragrunt.hcl": self._workload_stack(),
            "environments/non-prod/foundation/terragrunt.hcl": (
                'terraform { source = "../../../modules/foundation" }\n'
            ),
        }

        assert _find_terragrunt_dangling_dependencies(files, set()) == []

    def test_ok_when_foundation_stack_pre_exists_in_repo(self) -> None:
        files = {"environments/non-prod/eb-dotnet/terragrunt.hcl": self._workload_stack()}

        # The foundation stack already lives in the target repo (not regenerated).
        known = {"environments/non-prod/foundation"}

        assert _find_terragrunt_dangling_dependencies(files, known) == []

    def test_remote_config_path_is_not_flagged(self) -> None:
        files = {
            "environments/non-prod/eb-dotnet/terragrunt.hcl": (
                'dependency "shared" {\n'
                '  config_path = "git::https://example.com/infra.git//foundation"\n'
                "}\n"
                "inputs = { vpc_id = dependency.shared.outputs.vpc_id }\n"
            )
        }

        assert _find_terragrunt_dangling_dependencies(files, set()) == []

    def test_surfaced_as_structural_in_full_review(self) -> None:
        files = {
            "environments/non-prod/eb-dotnet/terragrunt.hcl": self._workload_stack(),
        }

        result = static_review_generated_files(files)

        assert any("environments/non-prod/foundation" in s for s in result.structural)

    def test_existing_stack_dirs_reads_repo(self, tmp_path) -> None:
        env = tmp_path / "environments" / "non-prod"
        (env / "foundation").mkdir(parents=True)
        (env / "foundation" / "terragrunt.hcl").write_text("# foundation\n")
        (env.parent / "non-prod" / "terragrunt.hcl").write_text("# env root, not a stack\n")
        (env / ".terragrunt-cache").mkdir()
        (env / ".terragrunt-cache" / "terragrunt.hcl").write_text("# cached copy\n")

        dirs = existing_stack_dirs(tmp_path)

        assert dirs == {"environments/non-prod/foundation"}
        assert existing_stack_dirs(None) == set()


class TestHardcodedSecretValues:
    def test_flags_named_secret_literal(self) -> None:
        files = {
            "environments/non-prod/app/terragrunt.hcl": (
                "inputs = {\n"
                "  environment_variables = {\n"
                '    WEBUI_SECRET_KEY = "change-me-in-production"\n'
                "  }\n"
                "}\n"
            )
        }

        warnings = _find_hardcoded_secret_values(files)

        assert len(warnings) == 1
        assert "WEBUI_SECRET_KEY" in warnings[0]
        assert "random_password" in warnings[0]

    def test_flags_plain_password_literal(self) -> None:
        files = {
            "modules/db/main.tf": 'resource "x" "y" {\n  master_password = "hunter2value"\n}\n'
        }
        assert len(_find_hardcoded_secret_values(files)) == 1

    def test_ignores_secret_reference_identifiers(self) -> None:
        # `*_arn` / `*_name` / `*_id` are references to a secret, not the secret.
        files = {
            "modules/app/main.tf": (
                'resource "x" "y" {\n'
                '  secret_arn  = "arn:aws:secretsmanager:us-west-2:1234:secret:foo"\n'
                '  secret_name = "myapp/db-credentials"\n'
                "}\n"
            )
        }
        assert _find_hardcoded_secret_values(files) == []

    def test_ignores_secret_sourced_from_reference(self) -> None:
        # A non-literal value (var/data reference) is fine.
        files = {"modules/app/main.tf": "locals {\n  api_token = var.api_token\n}\n"}
        assert _find_hardcoded_secret_values(files) == []

    def test_ignores_markdown(self) -> None:
        files = {"README.md": 'Set `WEBUI_SECRET_KEY = "change-me-in-production"` before deploy.'}
        assert _find_hardcoded_secret_values(files) == []


class TestDuplicateNamedResources:
    # Drive the check from the code's own set so every named resource type — and
    # any type added to it later — is covered, not just a hand-picked subset.
    @pytest.mark.parametrize("resource_type", sorted(_NAMED_RESOURCE_TYPES))
    def test_duplicate_provider_names_flagged_for_every_named_type(
        self, resource_type: str
    ) -> None:
        provider_name = "${var.environment}-shared"
        files = {
            "modules/service-a/main.tf": (
                f'resource "{resource_type}" "this" {{\n  name = "{provider_name}"\n}}\n'
            ),
            "modules/service-b/main.tf": (
                f'resource "{resource_type}" "this" {{\n  name = "{provider_name}"\n}}\n'
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
    # Drive from the code's own set: every singleton-owned resource type — and any
    # added later — must be flagged when declared in more than one module.
    @pytest.mark.parametrize("resource_type", sorted(_SINGLETON_RESOURCE_TYPES))
    def test_singleton_in_multiple_modules_flagged(self, resource_type: str) -> None:
        """A singleton-owned resource in two unrelated modules breaks the foundation boundary."""
        files = {
            "modules/network/main.tf": f'resource "{resource_type}" "this" {{}}',
            "modules/rds-postgres/main.tf": f'resource "{resource_type}" "this" {{}}',
        }
        errors = _find_singleton_resource_duplication(files)
        assert any(resource_type in e for e in errors)
        assert any("`modules/network`" in e for e in errors)
        assert any("`modules/rds-postgres`" in e for e in errors)

    @pytest.mark.parametrize("resource_type", sorted(_SINGLETON_RESOURCE_TYPES))
    def test_static_review_warns_on_singleton_in_foundation_and_workload(
        self, resource_type: str
    ) -> None:
        # A singleton resource in two modules is a broken foundation boundary, but
        # both validate fine in isolation — Terraform won't catch it. It is an
        # advisory warning for the reviewer, not a blocking error.
        files = {
            "modules/foundation/main.tf": f'resource "{resource_type}" "this" {{}}',
            "modules/ecs-fargate-stack/main.tf": f'resource "{resource_type}" "this" {{}}',
        }

        result = static_review_generated_files(files)

        assert result.status == ValidationStatus.PARTIAL
        assert not result.errors
        assert any(resource_type in w for w in result.warnings)

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


_APPLY_PATH = ".github/workflows/terraform-apply.yml"

# A trimmed but structurally complete apply workflow: scoped by a `detect` job,
# gated behind an `environment:`, triggered only on push to main.
_GOOD_APPLY_WORKFLOW = (
    "on:\n"
    "  push:\n"
    "    branches:\n"
    "      - main\n"
    "jobs:\n"
    "  detect:\n"
    "    runs-on: ubuntu-latest\n"
    "    outputs:\n"
    "      any: ${{ steps.scan.outputs.any }}\n"
    "  gate:\n"
    "    needs: detect\n"
    "    environment: non-prod\n"
    "    runs-on: ubuntu-latest\n"
    "  apply-foundation:\n"
    "    needs: [detect, gate]\n"
    "    if: ${{ needs.detect.outputs.foundation == 'true' }}\n"
    "    runs-on: ubuntu-latest\n"
)


class TestApplyWorkflowGuards:
    def test_good_workflow_has_no_errors(self) -> None:
        assert _apply_workflow_errors(_APPLY_PATH, _GOOD_APPLY_WORKFLOW) == []

    def test_pull_request_trigger_flagged(self) -> None:
        content = _GOOD_APPLY_WORKFLOW + "  pull_request:\n    branches: [main]\n"
        errors = _apply_workflow_errors(_APPLY_PATH, content)
        assert any("must not run on pull requests" in e for e in errors)

    def test_missing_main_branch_filter_flagged(self) -> None:
        content = _GOOD_APPLY_WORKFLOW.replace("      - main\n", "      - release\n")
        errors = _apply_workflow_errors(_APPLY_PATH, content)
        assert any("limited to main or master" in e for e in errors)

    def test_missing_environment_gate_flagged(self) -> None:
        content = _GOOD_APPLY_WORKFLOW.replace("    environment: non-prod\n", "")
        errors = _apply_workflow_errors(_APPLY_PATH, content)
        assert any("manual approval" in e and "environment:" in e for e in errors)

    def test_missing_change_scoping_flagged(self) -> None:
        # Strip the detect-output guard: the run no longer scopes to changed components.
        content = _GOOD_APPLY_WORKFLOW.replace(
            "    if: ${{ needs.detect.outputs.foundation == 'true' }}\n", ""
        ).replace("      any: ${{ steps.scan.outputs.any }}\n", "")
        errors = _apply_workflow_errors(_APPLY_PATH, content)
        assert any("scope the run to changed components" in e for e in errors)

    def test_non_apply_workflow_ignored(self) -> None:
        pr_check = ".github/workflows/terraform-pr-check.yml"
        assert _apply_workflow_errors(pr_check, "on: pull_request") == []


def _ingress_rule(port: int, cidr_attr: str) -> str:
    return (
        'resource "aws_security_group" "x" {\n'
        "  ingress {\n"
        f"    from_port = {port}\n"
        f"    to_port   = {port}\n"
        '    protocol  = "tcp"\n'
        f"    {cidr_attr}\n"
        "  }\n"
        "}\n"
    )


# Every public-internet CIDR form the regexes recognize (both legacy bracket
# blocks and the newer single-value rule attributes, IPv4 and IPv6).
_PUBLIC_CIDR_FORMS = [
    'cidr_blocks = ["0.0.0.0/0"]',
    'cidr_ipv4 = "0.0.0.0/0"',
    'ipv6_cidr_blocks = ["::/0"]',
    'cidr_ipv6 = "::/0"',
]


class TestDangerousPublicIngress:
    # Drive from the code's own port set so every sensitive port — and any added
    # later — is covered, not just SSH/22.
    @pytest.mark.parametrize("port", sorted(_DANGEROUS_PORTS))
    def test_every_dangerous_port_open_to_public_is_flagged(self, port: int) -> None:
        assert _contains_dangerous_public_ingress(_ingress_rule(port, _PUBLIC_CIDR_FORMS[0]))

    @pytest.mark.parametrize("cidr_attr", _PUBLIC_CIDR_FORMS)
    def test_every_public_cidr_form_is_recognized(self, cidr_attr: str) -> None:
        port = min(_DANGEROUS_PORTS)
        assert _contains_dangerous_public_ingress(_ingress_rule(port, cidr_attr))

    def test_safe_port_open_to_public_is_not_flagged(self) -> None:
        # 443 is not a sensitive port — public HTTPS ingress is expected.
        assert not _contains_dangerous_public_ingress(_ingress_rule(443, _PUBLIC_CIDR_FORMS[0]))

    def test_dangerous_port_with_restricted_cidr_is_not_flagged(self) -> None:
        rule = _ingress_rule(min(_DANGEROUS_PORTS), 'cidr_blocks = ["10.0.0.0/8"]')
        assert not _contains_dangerous_public_ingress(rule)
