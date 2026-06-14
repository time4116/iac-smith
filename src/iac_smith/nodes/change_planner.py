import re

from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import InfrastructureIntent, SupportedIntent

STACK_NAMES = {
    SupportedIntent.BASELINE: "baseline",
    SupportedIntent.VPC_FOUNDATION: "vpc",
    SupportedIntent.EKS_FARGATE: "eks-fargate",
    SupportedIntent.ECS_FARGATE: "ecs-fargate",
}


def _repo_slug(target_repo: str) -> str:
    name = target_repo.split("/")[-1]
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40]


def plan_changes(intent: InfrastructureIntent, target_repo: str) -> ChangePlan:
    if intent.blocked or intent.supported_intent == SupportedIntent.UNSUPPORTED:
        raise ValueError(intent.block_reason or "Unsupported request family")

    stack_name = STACK_NAMES[intent.supported_intent]
    repo_slug = _repo_slug(target_repo)
    backend_resources = {
        env: BackendResource(
            bucket=f"{repo_slug}-{env}-tfstate",
            lock_table=f"{repo_slug}-{env}-tflock",
        )
        for env in intent.environments
    }

    files = [
        "README.md",
        ".github/workflows/terraform-pr-check.yml",
        ".github/workflows/terraform-apply.yml",
        "live/terragrunt.hcl",
    ]
    for env in intent.environments:
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
        environments=intent.environments,
        files_to_generate=files,
        backend_resources=backend_resources,
        summary=[
            f"Generate {stack_name} Terraform/Terragrunt structure",
            "Include backend bootstrap for S3 state and DynamoDB locking",
            "Include target repository PR check and post-merge apply workflows",
        ],
    )
