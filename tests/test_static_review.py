"""Tests for static review — especially cross-file duplicate detection."""

from __future__ import annotations

from iac_smith.models.validation import ValidationStatus
from iac_smith.nodes.static_review import (
    _find_cross_file_duplicates,
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
            "environments/non-prod/terragrunt.hcl": 'variable "region" { default = "us-west-2" }',
        }
        errors = _find_cross_file_duplicates(files)
        assert errors == []


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
        assert result.status != ValidationStatus.FAILED
