"""Tests for static review — especially cross-file duplicate detection."""

from __future__ import annotations

from iac_smith.models.validation import ValidationStatus
from iac_smith.nodes.static_review import (
    _find_cross_file_duplicates,
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
        assert "but no variable" in errors[0]
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
    def test_duplicate_variable_triggers_failed(self) -> None:
        """Cross-file duplicates bubble up to static_review_generated_files."""
        main_tf = 'variable "vpc_id" { type = string }\nresource "null_resource" "x" {}'
        files = {
            "modules/ecs-fargate/main.tf": main_tf,
            "modules/ecs-fargate/variables.tf": 'variable "vpc_id" { type = string }',
        }
        result = static_review_generated_files(files)
        assert result.status == ValidationStatus.FAILED
        assert any("vpc_id" in e for e in result.errors)
        assert any("Variable" in e for e in result.errors)

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
