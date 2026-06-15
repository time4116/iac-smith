import re

from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns


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


def _stack_name(intent: InfrastructureIntent) -> str:
    """Derive a stable filesystem-safe stack name from the resource_type."""
    return re.sub(r"[^a-z0-9-]", "-", intent.resource_type.lower().replace("_", "-")).strip("-")


def _module_already_exists(stack: str, repo_patterns: RepoPatterns | None) -> bool:
    """Return True if the target repo already has this stack under modules/."""
    if not repo_patterns:
        return False
    return any(
        path == f"modules/{stack}" or path.startswith(f"modules/{stack}/")
        for path in repo_patterns.existing_stack_paths
    )


def plan_changes(
    intent: InfrastructureIntent,
    target_repo: str,
    repo_patterns: RepoPatterns | None = None,
) -> ChangePlan:
    if intent.blocked:
        raise ValueError(intent.block_reason or "Blocked infrastructure request")

    stack_name = _stack_name(intent)
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

    # Only generate module scaffold if the repo doesn't already have one for this stack.
    if stack_name != "baseline" and not _module_already_exists(stack_name, repo_patterns):
        files.extend(
            [
                f"modules/{stack_name}/main.tf",
                f"modules/{stack_name}/variables.tf",
                f"modules/{stack_name}/outputs.tf",
                f"modules/{stack_name}/versions.tf",
                f"modules/{stack_name}/README.md",
            ]
        )

    summary = [
        f"Generate {stack_name} Terraform/Terragrunt structure",
        "Generate AWS infrastructure with secure defaults regardless of prompt wording",
        "Include backend bootstrap for S3 state and DynamoDB locking",
        "Include target repository PR check and post-merge apply workflows",
    ]
    if _module_already_exists(stack_name, repo_patterns):
        summary.append(
            f"Reusing existing modules/{stack_name} from repository — "
            "new live path wired to existing module"
        )

    return ChangePlan(
        stack_name=stack_name,
        environments=environments,
        files_to_generate=files,
        backend_resources=backend_resources,
        summary=summary,
    )
