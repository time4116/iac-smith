import re

from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns


def _repo_slug(target_repo: str) -> str:
    name = target_repo.split("/")[-1]
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40]


def _backend_resource(env: str, repo_slug: str) -> BackendResource:
    return BackendResource(
        bucket=f"iac-smith-state-{env}-{repo_slug}",
        lock_table=f"iac-smith-lock-{env}",
    )


# Stack names treated as a shared network/foundation layer. Single source of truth
# so the dangling-dependency detector (static_review) and the planner agree on what
# counts as "foundation" without keyword drift.
FOUNDATION_STACK_NAMES = frozenset({"baseline", "foundation", "vpc", "vpc-foundation"})


def _is_foundation_stack(stack_name: str) -> bool:
    return stack_name in FOUNDATION_STACK_NAMES


def _foundation_files(environments: list[str]) -> list[str]:
    """Every file the foundation module + per-environment foundation stack needs."""
    files: list[str] = []
    for env in environments:
        files.append(f"environments/{env}/foundation/terragrunt.hcl")
        files.append(f"environments/{env}/foundation/README.md")
    files.extend(
        [
            "modules/foundation/main.tf",
            "modules/foundation/variables.tf",
            "modules/foundation/outputs.tf",
            "modules/foundation/versions.tf",
            "modules/foundation/README.md",
        ]
    )
    return files


def add_foundation_stack(change_plan: ChangePlan) -> ChangePlan:
    """Return a plan with the foundation module + stack added, if not already present.

    Used when generated output proves a foundation is truly needed (a workload stack
    depends on one that was not planned) — see ``plan_changes`` for the up-front
    decision. Idempotent: adds only the foundation files missing from the plan.
    """
    planned = set(change_plan.files_to_generate)
    additions = [f for f in _foundation_files(change_plan.environments) if f not in planned]
    if not additions:
        return change_plan
    note = (
        "Generate foundation module for shared network dependencies "
        "(a generated stack depends on it)"
    )
    summary = change_plan.summary if note in change_plan.summary else [*change_plan.summary, note]
    return change_plan.model_copy(
        update={
            "files_to_generate": [*change_plan.files_to_generate, *additions],
            "summary": summary,
        }
    )


def _repo_has_foundation(repo_patterns: RepoPatterns | None) -> bool:
    if not repo_patterns:
        return False
    return any(
        path == "modules/foundation"
        or path.startswith("modules/foundation/")
        or path.endswith("/foundation")
        for path in repo_patterns.existing_stack_paths
    )


def _uses_foundation(
    stack_name: str,
    intent: InfrastructureIntent,
    repo_patterns: RepoPatterns | None,
) -> bool:
    if _is_foundation_stack(stack_name):
        return False
    return _repo_has_foundation(repo_patterns) or intent.requires_new_vpc


def _should_generate_foundation(
    stack_name: str,
    intent: InfrastructureIntent,
    repo_patterns: RepoPatterns | None,
) -> bool:
    return _uses_foundation(stack_name, intent, repo_patterns) and not _repo_has_foundation(
        repo_patterns
    )


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
    name = re.sub(r"[^a-z0-9-]", "-", intent.resource_type.lower().replace("_", "-")).strip("-")
    # Strip redundant -stack suffix — all stacks are stacks; the model sometimes
    # appends it (e.g. resource_type="ecs_fargate_stack" → "ecs-fargate-stack").
    if name.endswith("-stack"):
        name = name[: -len("-stack")]
    return name


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
    environments = _planned_environments(intent, repo_patterns)
    slug = _repo_slug(target_repo)
    backend_resources = {env: _backend_resource(env, slug) for env in environments}

    files = [
        "README.md",
        ".github/workflows/terraform-pr-check.yml",
        ".github/workflows/terraform-apply.yml",
    ]
    for env in environments:
        files.extend(
            [
                f"bootstrap/backend/{env}/main.tf",
                f"bootstrap/backend/{env}/variables.tf",
                f"bootstrap/backend/{env}/outputs.tf",
                f"bootstrap/backend/{env}/README.md",
                # The environment root config is named root.hcl (not terragrunt.hcl):
                # Terragrunt deprecated using terragrunt.hcl as an include root. Stacks
                # include it via find_in_parent_folders("root.hcl").
                f"environments/{env}/root.hcl",
                f"environments/{env}/{stack_name}/terragrunt.hcl",
                f"environments/{env}/{stack_name}/README.md",
            ]
        )

    if _should_generate_foundation(stack_name, intent, repo_patterns):
        files.extend(_foundation_files(environments))

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
    if _uses_foundation(stack_name, intent, repo_patterns):
        if _repo_has_foundation(repo_patterns):
            summary.append(
                "Follow existing foundation module pattern for shared network dependencies"
            )
        else:
            summary.append("Generate foundation module for shared network dependencies")

    return ChangePlan(
        stack_name=stack_name,
        environments=environments,
        files_to_generate=files,
        backend_resources=backend_resources,
        summary=summary,
    )
