from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.nodes.static_review import static_review_generated_files


def test_static_review_blocks_hardcoded_terragrunt_state_key():
    result = static_review_generated_files(
        {
            "environments/non-prod/example/terragrunt.hcl": (
                'remote_state { config = { key = "fixed.tfstate" } }'
            )
        }
    )

    assert result.status.value == "failed"
    assert any("path_relative_to_include" in error for error in result.errors)


def test_static_review_blocks_obvious_secret_material():
    fake_access_key = "AKIA" + ("A" * 16)
    result = static_review_generated_files(
        {"modules/example/main.tf": f'variable "x" {{ default = "{fake_access_key}" }}'}
    )

    assert result.status.value == "failed"
    assert any("secret" in error.lower() for error in result.errors)


def test_static_review_warns_when_module_readme_missing_terraform_docs_markers():
    result = static_review_generated_files({"modules/example/README.md": "# Example\n"})

    assert result.status.value == "partial"
    assert any("terraform-docs" in warning for warning in result.warnings)


def test_static_review_passes_safe_minimal_generated_files():
    terragrunt = (
        'remote_state { config = { key = "${path_relative_to_include()}/terraform.tfstate" } }'
    )
    result = static_review_generated_files(
        {
            "environments/non-prod/example/terragrunt.hcl": terragrunt,
            "modules/example/README.md": (
                "# Example\n<!-- BEGIN_TF_DOCS -->\n<!-- END_TF_DOCS -->\n"
            ),
        }
    )

    assert result.status.value == "passed"
    assert result.errors == []


def test_static_review_warns_on_cidr_block_style_dangerous_ingress():
    """Old-style security group using cidr_blocks = [...] should reach PR review."""
    bad_sg = """
resource "aws_security_group_rule" "bad" {
  type        = "ingress"
  from_port   = 22
  to_port     = 22
  cidr_blocks = ["0.0.0.0/0"]
}
"""
    result = static_review_generated_files({"modules/example/main.tf": bad_sg})
    assert result.status.value == "partial"
    assert any("public ingress" in warning for warning in result.warnings)
    assert result.errors == []


def test_static_review_warns_on_cidr_ipv4_attribute_style_dangerous_ingress():
    """Newer aws_vpc_security_group_ingress_rule using cidr_ipv4 attribute."""
    bad_sg = """
resource "aws_vpc_security_group_ingress_rule" "bad" {
  from_port   = 5432
  to_port     = 5432
  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "tcp"
}
"""
    result = static_review_generated_files({"modules/example/main.tf": bad_sg})
    assert result.status.value == "partial"
    assert any("public ingress" in warning for warning in result.warnings)
    assert result.errors == []


def test_static_review_allows_public_http_https_on_load_balancer():
    """Port 80/443 open to the internet is expected on ALBs — should not be blocked."""
    alb_sg = """
resource "aws_vpc_security_group_ingress_rule" "http" {
  from_port   = 80
  to_port     = 80
  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "tcp"
}
"""
    result = static_review_generated_files({"modules/alb/main.tf": alb_sg})
    assert "modules/alb/main.tf" not in " ".join(result.errors)


# Intent is no longer used inside static_review — this test proves the reviewer
# checks the *generated files*, not the intent text.
def test_static_review_checks_generated_files_not_intent_text():
    intent = InfrastructureIntent(
        raw_request="Create public RDS Postgres open to the internet",
        resource_type="rds_postgres",
        environment_scope=EnvironmentScope.PROD_ONLY,
        environments=["prod"],
        region="us-west-2",
    )
    safe_tf = """
module "db" {
  source              = "terraform-aws-modules/rds/aws"
  storage_encrypted   = true
  publicly_accessible = false
}
"""
    result = static_review_generated_files({"modules/rds-postgres/main.tf": safe_tf})
    _ = intent  # static review doesn't inspect intent
    assert result.errors == []


def test_static_review_rejects_generated_references_to_undeclared_modules():
    main_tf = 'resource "aws_ecs_cluster" "this" { name = var.name_prefix }\n'
    outputs_tf = 'output "vpc_id" { value = module.vpc.vpc_id }\n'
    result = static_review_generated_files(
        {
            "modules/ecs-fargate/main.tf": main_tf,
            "modules/ecs-fargate/outputs.tf": outputs_tf,
        }
    )

    assert result.status.value == "failed"
    assert any("module.vpc" in error for error in result.errors)


def test_static_review_allows_references_to_declared_modules():
    main_tf = 'module "vpc" { source = "terraform-aws-modules/vpc/aws" version = "~> 5.0" }\n'
    outputs_tf = 'output "vpc_id" { value = module.vpc.vpc_id }\n'
    result = static_review_generated_files(
        {
            "modules/network/main.tf": main_tf,
            "modules/network/outputs.tf": outputs_tf,
        }
    )

    assert result.errors == []


def test_static_review_blocks_apply_workflow_on_pull_request():
    workflow = """
name: Terraform Apply
on:
  pull_request:
  push:
    branches: [main]
jobs: {}
"""

    result = static_review_generated_files({".github/workflows/terraform-apply.yml": workflow})

    assert result.status.value == "failed"
    assert any("feature branches" in error for error in result.errors)


def test_static_review_blocks_apply_workflow_without_main_or_master_branch_filter():
    workflow = """
name: Terraform Apply
on:
  push:
jobs: {}
"""

    result = static_review_generated_files({".github/workflows/terraform-apply.yml": workflow})

    assert result.status.value == "failed"
    assert any("main or master" in error for error in result.errors)
