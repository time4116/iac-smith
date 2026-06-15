from iac_smith.generator import generate_files
from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent, SupportedIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.nodes.static_review import static_review_generated_files


def _intent(kind: SupportedIntent) -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request="Create non-prod VPC in us-west-2",
        supported_intent=kind,
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
            "live/terragrunt.hcl",
            "live/non-prod/terragrunt.hcl",
            f"live/non-prod/{stack_name}/terragrunt.hcl",
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
        intent=_intent(SupportedIntent.VPC_FOUNDATION),
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
    state_key = 'key            = "${path_relative_to_include()}/terraform.tfstate"'
    assert state_key in files["live/terragrunt.hcl"]
    assert 'include "root"' in files["live/non-prod/vpc/terragrunt.hcl"]
    assert static_review_generated_files(files).errors == []


def test_generate_rds_files_create_private_encrypted_database_module():
    files = generate_files(
        intent=_intent(SupportedIntent.RDS_POSTGRES),
        change_plan=_plan("rds-postgres"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    main_tf = files["modules/rds-postgres/main.tf"]
    assert 'module "db"' in main_tf
    assert 'source  = "terraform-aws-modules/rds/aws"' in main_tf
    assert "storage_encrypted" in main_tf
    assert "publicly_accessible" in main_tf
    assert "false" in main_tf
    assert "manage_master_user_password" in main_tf
    assert static_review_generated_files(files).errors == []


def test_generate_baseline_does_not_create_stack_module():
    plan = _plan("baseline")
    plan.files_to_generate = [
        "README.md",
        "live/terragrunt.hcl",
        "live/non-prod/terragrunt.hcl",
        "bootstrap/backend/non-prod/main.tf",
    ]

    files = generate_files(
        intent=_intent(SupportedIntent.BASELINE),
        change_plan=plan,
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert "bootstrap/backend/non-prod/main.tf" in files
    assert not any(path.startswith("modules/") for path in files)
