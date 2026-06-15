import re

from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent, SupportedIntent
from iac_smith.models.repo_patterns import RepoPatterns

STACK_NAMES = {
    SupportedIntent.BASELINE: "baseline",
    SupportedIntent.VPC_FOUNDATION: "vpc",
    SupportedIntent.EKS_FARGATE: "eks-fargate",
    SupportedIntent.ECS_FARGATE: "ecs-fargate",
    SupportedIntent.RDS_POSTGRES: "rds-postgres",
}


def _repo_slug(target_repo: str) -> str:
    name = target_repo.split("/")[-1]
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40]


def _planned_environments(
    intent: InfrastructureIntent,
    repo_patterns: RepoPatterns | None,
) -> list[str]:
    if (
        repo_patterns
        and repo_patterns.default_environment_names
        and intent.environment_scope == EnvironmentScope.BOTH
    ):
        return repo_patterns.default_environment_names
    return intent.environments


def plan_changes(
    intent: InfrastructureIntent,
    target_repo: str,
    repo_patterns: RepoPatterns | None = None,
) -> ChangePlan:
    if intent.blocked or intent.supported_intent == SupportedIntent.UNSUPPORTED:
        raise ValueError(intent.block_reason or "Unsupported request family")

    stack_name = STACK_NAMES[intent.supported_intent]
    repo_slug = _repo_slug(target_repo)
    environments = _planned_environments(intent, repo_patterns)
    backend_resources = {
        env: BackendResource(
            bucket=f"{repo_slug}-{env}-tfstate",
            lock_table=f"{repo_slug}-{env}-tflock",
        )
        for env in environments
    }

    files = [
        "README.md",
        ".github/workflows/terraform-pr-check.yml",
        ".github/workflows/terraform-apply.yml",
        "live/terragrunt.hcl",
    ]
    for env in environments:
        files.extend(
            [
                f"bootstrap/backend/{env}/main.tf",
                f"bootstrap/backend/{env}/variables.tf",
                f"bootstrap/backend/{env}/outputs.tf",
                f"bootstrap/backend/{env}/README.md",
                f"live/{env}/terragrunt.hcl",
                f"live/{env}/{stack_name}/terragrunt.hcl",
                f"live/{env}/{stack_name}/README.md",
            ]
        )
    if stack_name != "baseline":
        files.extend(
            [
                f"modules/{stack_name}/main.tf",
                f"modules/{stack_name}/variables.tf",
                f"modules/{stack_name}/outputs.tf",
                f"modules/{stack_name}/versions.tf",
                f"modules/{stack_name}/README.md",
            ]
        )

    return ChangePlan(
        stack_name=stack_name,
        environments=environments,
        files_to_generate=files,
        backend_resources=backend_resources,
        summary=[
            f"Generate {stack_name} Terraform/Terragrunt structure",
            "Include backend bootstrap for S3 state and DynamoDB locking",
            "Include target repository PR check and post-merge apply workflows",
        ],
    )
