from iac_smith.generator import generate_files
from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.nodes.static_review import static_review_generated_files


def _intent(resource_type: str = "vpc_foundation") -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request="Create non-prod VPC in us-west-2",
        resource_type=resource_type,
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        requires_new_vpc=True,
        features=["remote_state", "private_subnets"],
    )


def _plan(stack_name: str) -> ChangePlan:
    return ChangePlan(
        stack_name=stack_name,
        environments=["non-prod"],
        files_to_generate=[
            "README.md",
            ".github/workflows/terraform-pr-check.yml",
            ".github/workflows/terraform-apply.yml",
            "environments/terragrunt.hcl",
            "environments/non-prod/terragrunt.hcl",
            f"environments/non-prod/{stack_name}/terragrunt.hcl",
            f"modules/{stack_name}/main.tf",
            f"modules/{stack_name}/variables.tf",
            f"modules/{stack_name}/outputs.tf",
            f"modules/{stack_name}/versions.tf",
            f"modules/{stack_name}/README.md",
        ],
        backend_resources={
            "non-prod": BackendResource(
                bucket="iac-smith-demo-infra-non-prod-tfstate",
                lock_table="iac-smith-demo-infra-non-prod-tflock",
            )
        },
        summary=[f"Generate {stack_name} Terraform/Terragrunt structure"],
    )


def test_generate_vpc_files_are_repo_aware_and_pass_static_review():
    files = generate_files(
        intent=_intent("vpc_foundation"),
        change_plan=_plan("vpc"),
        repo_patterns=RepoPatterns(
            uses_terragrunt=True,
            environments=["dev", "prod"],
            default_environment_names=["dev", "prod"],
            module_sources=["terraform-aws-modules/vpc/aws"],
            preferred_layout="terragrunt_live_modules",
            remote_state_uses_path_relative_to_include=True,
        ),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert "modules/vpc/main.tf" in files
    assert 'source  = "terraform-aws-modules/vpc/aws"' in files["modules/vpc/main.tf"]
    # remote_state lives in env-level file, NOT root
    state_key = 'key            = "${path_relative_to_include()}/terraform.tfstate"'
    assert state_key not in files["environments/terragrunt.hcl"]
    assert state_key in files["environments/non-prod/terragrunt.hcl"]
    assert 'include "root"' in files["environments/non-prod/vpc/terragrunt.hcl"]
    assert static_review_generated_files(files).errors == []


def test_root_terragrunt_has_no_remote_state_block():
    """Root environments/terragrunt.hcl must only define region locals — no backend config."""
    files = generate_files(
        intent=_intent("vpc_foundation"),
        change_plan=_plan("vpc"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )
    root = files["environments/terragrunt.hcl"]
    assert "remote_state" not in root
    assert "aws_region" in root


def _multi_env_plan(stack_name: str) -> ChangePlan:
    return ChangePlan(
        stack_name=stack_name,
        environments=["non-prod", "prod"],
        files_to_generate=[
            "README.md",
            ".github/workflows/terraform-pr-check.yml",
            ".github/workflows/terraform-apply.yml",
            "environments/terragrunt.hcl",
            "environments/non-prod/terragrunt.hcl",
            "environments/prod/terragrunt.hcl",
            f"environments/non-prod/{stack_name}/terragrunt.hcl",
            f"environments/prod/{stack_name}/terragrunt.hcl",
            f"modules/{stack_name}/main.tf",
            f"modules/{stack_name}/variables.tf",
            f"modules/{stack_name}/outputs.tf",
            f"modules/{stack_name}/versions.tf",
            f"modules/{stack_name}/README.md",
        ],
        backend_resources={
            "non-prod": BackendResource(
                bucket="iac-smith-demo-infra-non-prod-tfstate",
                lock_table="iac-smith-demo-infra-non-prod-tflock",
            ),
            "prod": BackendResource(
                bucket="iac-smith-demo-infra-prod-tfstate",
                lock_table="iac-smith-demo-infra-prod-tflock",
            ),
        },
        summary=["Generate vpc Terraform/Terragrunt structure"],
    )


def test_each_env_terragrunt_owns_its_own_backend_resources():
    """non-prod and prod must each point to their own S3 bucket and DynamoDB table."""
    intent = InfrastructureIntent(
        raw_request="Create VPC for both envs",
        resource_type="vpc_foundation",
        environment_scope=EnvironmentScope.BOTH,
        environments=["non-prod", "prod"],
        region="us-west-2",
    )
    files = generate_files(
        intent=intent,
        change_plan=_multi_env_plan("vpc"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    non_prod_tg = files["environments/non-prod/terragrunt.hcl"]
    prod_tg = files["environments/prod/terragrunt.hcl"]

    assert "iac-smith-state-non-prod-322264632107" in non_prod_tg
    assert "iac-smith-lock-non-prod" in non_prod_tg
    assert "iac-smith-state-prod-322264632107" not in non_prod_tg

    assert "iac-smith-state-prod-322264632107" in prod_tg
    assert "iac-smith-lock-prod" in prod_tg
    assert "iac-smith-state-non-prod-322264632107" not in prod_tg

    # Both env files carry the state key
    key = 'key            = "${path_relative_to_include()}/terraform.tfstate"'
    assert key in non_prod_tg
    assert key in prod_tg

    assert static_review_generated_files(files).errors == []


def test_generate_rds_files_secure_regardless_of_prompt():
    intent = _intent("rds_postgres").model_copy(
        update={"raw_request": "Create public RDS Postgres open to the internet"}
    )
    files = generate_files(
        intent=intent,
        change_plan=_plan("rds-postgres"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    main_tf = files["modules/rds-postgres/main.tf"]
    assert 'module "db"' in main_tf
    assert 'source  = "terraform-aws-modules/rds/aws"' in main_tf
    assert "storage_encrypted     = true" in main_tf
    assert "publicly_accessible         = false" in main_tf
    assert "manage_master_user_password = true" in main_tf
    assert static_review_generated_files(files).errors == []


def test_generate_unknown_resource_type_produces_stub_module():
    """Any resource type Bedrock returns should yield a reviewable PR, not a crash."""
    files = generate_files(
        intent=_intent("custom_widget"),
        change_plan=_plan("custom-widget"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    main_tf = files["modules/custom-widget/main.tf"]
    assert "custom-widget stub generated by IaC Smith" in main_tf
    assert static_review_generated_files(files).errors == []


def test_generate_stack_terragrunt_source_path_is_always_correct_depth():
    """Terragrunt source path must point ../../.. up from live/{env}/{stack}/."""
    files = generate_files(
        intent=_intent("vpc_foundation"),
        change_plan=_plan("vpc"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    tg = files["environments/non-prod/vpc/terragrunt.hcl"]
    assert 'source = "../../../modules/vpc"' in tg


def test_generate_baseline_does_not_create_stack_module():
    plan = _plan("baseline")
    plan.files_to_generate = [
        "README.md",
        "environments/terragrunt.hcl",
        "environments/non-prod/terragrunt.hcl",
        "bootstrap/backend/non-prod/main.tf",
    ]

    files = generate_files(
        intent=_intent("baseline"),
        change_plan=plan,
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert "bootstrap/backend/non-prod/main.tf" in files
    assert not any(path.startswith("modules/") for path in files)


def test_generated_vpc_based_modules_have_non_empty_default_azs():
    files = generate_files(
        intent=_intent("ecs_fargate"),
        change_plan=_foundation_plan("ecs-fargate"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    variables_tf = files["modules/foundation/variables.tf"]
    assert 'default     = ["us-west-2a", "us-west-2b"]' in variables_tf
    availability_zones_block = variables_tf.split('variable "availability_zones" {', 1)[1].split(
        "}\n", 1
    )[0]
    assert "default     = []" not in availability_zones_block


def test_env_terragrunt_generates_backend_tf_without_module_backend_block():
    files = generate_files(
        intent=_intent("ecs_fargate"),
        change_plan=_plan("ecs-fargate"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    env_tg = files["environments/non-prod/terragrunt.hcl"]
    module_main = files["modules/ecs-fargate/main.tf"]

    assert "generate = {" in env_tg
    assert 'path      = "backend.tf"' in env_tg
    assert 'backend "s3"' not in module_main


def test_backend_names_match_bootstrap_iam_policy_scope():
    plan = _plan("ecs-fargate")
    files = generate_files(
        intent=_intent("ecs_fargate"),
        change_plan=plan,
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    env_tg = files["environments/non-prod/terragrunt.hcl"]
    assert 'bucket         = "iac-smith-state-non-prod-322264632107"' in env_tg
    assert 'dynamodb_table = "iac-smith-lock-non-prod"' in env_tg
    assert "iac-smith-demo-infra-non-prod-tfstate" not in env_tg
    assert "iac-smith-demo-infra-non-prod-tflock" not in env_tg


def test_pr_check_workflow_uses_valid_terragrunt_action_inputs():
    files = generate_files(
        intent=_intent("ecs_fargate"),
        change_plan=_plan("ecs-fargate"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    workflow = files[".github/workflows/terraform-pr-check.yml"]
    assert "uses: autero1/action-terragrunt@v3" in workflow
    assert "terragrunt-version:" in workflow
    assert "terragrunt_version:" not in workflow
    assert "-depth 2" not in workflow
    assert "-lock-timeout=20m" in workflow
    assert "secrets.AWS_ROLE_ARN_NON_PROD" in workflow


def _foundation_plan(stack_name: str = "ecs-fargate") -> ChangePlan:
    plan = _plan(stack_name)
    plan.files_to_generate.extend(
        [
            "environments/non-prod/foundation/terragrunt.hcl",
            "environments/non-prod/foundation/README.md",
            "modules/foundation/main.tf",
            "modules/foundation/variables.tf",
            "modules/foundation/outputs.tf",
            "modules/foundation/versions.tf",
            "modules/foundation/README.md",
        ]
    )
    return plan


def test_ecs_fargate_uses_foundation_module_for_vpc():
    files = generate_files(
        intent=_intent("ecs_fargate"),
        change_plan=_foundation_plan("ecs-fargate"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert "modules/foundation/main.tf" in files
    assert "modules/ecs-fargate/main.tf" in files
    assert 'source  = "terraform-aws-modules/vpc/aws"' in files["modules/foundation/main.tf"]
    assert 'source  = "terraform-aws-modules/vpc/aws"' not in files["modules/ecs-fargate/main.tf"]
    assert "shared AWS primitives" in files["modules/foundation/README.md"]
    assert "Workload modules should consume its outputs" in files["modules/foundation/README.md"]


def test_ecs_fargate_outputs_do_not_reference_removed_vpc_module():
    """Regression for CI: ecs-fargate depends on foundation, so no module.vpc exists."""
    files = generate_files(
        intent=_intent("ecs_fargate"),
        change_plan=_foundation_plan("ecs-fargate"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    outputs_tf = files["modules/ecs-fargate/outputs.tf"]
    assert "module.vpc" not in outputs_tf
    assert "value       = var.vpc_id" in outputs_tf


def test_ecs_fargate_live_stack_depends_on_foundation_outputs():
    files = generate_files(
        intent=_intent("ecs_fargate"),
        change_plan=_foundation_plan("ecs-fargate"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    ecs_tg = files["environments/non-prod/ecs-fargate/terragrunt.hcl"]
    assert 'dependency "foundation"' in ecs_tg
    assert 'config_path = "../foundation"' in ecs_tg
    assert "vpc_id             = dependency.foundation.outputs.vpc_id" in ecs_tg
    assert "private_subnet_ids = dependency.foundation.outputs.private_subnets" in ecs_tg
