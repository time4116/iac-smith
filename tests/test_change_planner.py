from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.nodes.change_planner import plan_changes


def _intent(resource_type: str = "eks_fargate") -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request="Create non-prod EKS Fargate",
        resource_type=resource_type,
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        requires_new_vpc=True,
        features=["remote_state", "private_subnets", "logging"],
    )


def test_plan_uses_root_hcl_for_the_environment_root_config():
    # The environment root config is root.hcl, not terragrunt.hcl (Terragrunt
    # deprecated terragrunt.hcl as an include root), and the redundant top-level
    # environments/terragrunt.hcl is not generated.
    plan = plan_changes(_intent("eks_fargate"), target_repo="time4116/iac-smith-demo-infra")

    assert "environments/non-prod/root.hcl" in plan.files_to_generate
    assert "environments/non-prod/terragrunt.hcl" not in plan.files_to_generate
    assert "environments/terragrunt.hcl" not in plan.files_to_generate
    # Stack configs stay terragrunt.hcl.
    assert "environments/non-prod/eks-fargate/terragrunt.hcl" in plan.files_to_generate


def test_workload_module_resources_split_across_concern_files():
    # The workload module's resources are spread across generic concern files so no
    # single file must be generated in one oversized model response (max_tokens
    # truncation). foundation stays single-file.
    plan = plan_changes(_intent("eks_fargate"), target_repo="time4116/iac-smith-demo-infra")

    for name in ("main.tf", "iam.tf", "security.tf", "monitoring.tf"):
        assert f"modules/eks-fargate/{name}" in plan.files_to_generate
    # foundation is networking-only and is NOT split.
    assert "modules/foundation/iam.tf" not in plan.files_to_generate
    assert "modules/foundation/monitoring.tf" not in plan.files_to_generate


def test_plan_derives_stack_name_from_resource_type():
    plan = plan_changes(_intent("eks_fargate"), target_repo="time4116/iac-smith-demo-infra")

    assert plan.stack_name == "eks-fargate"
    assert "bootstrap/backend/non-prod/main.tf" in plan.files_to_generate
    assert "environments/non-prod/eks-fargate/terragrunt.hcl" in plan.files_to_generate
    assert "modules/eks-fargate/README.md" in plan.files_to_generate
    assert ".github/workflows/terraform-pr-check.yml" in plan.files_to_generate
    assert ".github/workflows/terraform-apply.yml" in plan.files_to_generate
    assert (
        plan.backend_resources["non-prod"].bucket == "iac-smith-state-non-prod-iac-smith-demo-infra"
    )
    assert plan.backend_resources["non-prod"].lock_table == "iac-smith-lock-non-prod"
    assert (
        "Generate AWS infrastructure with secure defaults regardless of prompt wording"
        in plan.summary
    )


def test_plan_accepts_arbitrary_resource_type():
    """Any resource_type string should produce a valid change plan, not raise."""
    for resource_type in ["rds_postgres", "s3_bucket", "aurora_cluster", "custom_widget"]:
        plan = plan_changes(_intent(resource_type), target_repo="time4116/iac-smith-demo-infra")
        assert plan.stack_name  # non-empty
        assert plan.files_to_generate


def test_plan_changes_does_not_inject_language_specific_golden_path_files():
    # No golden paths: a .NET/web-app request must not produce hardcoded C#
    # scaffolding (.csproj/Program.cs). Source artifacts, if any, are figured out
    # dynamically, never templated per language.
    intent = InfrastructureIntent(
        raw_request=(
            "Create a dotnet web app welcome page on Elastic Beanstalk and create "
            "a src directory where the code will be stored."
        ),
        resource_type="elastic_beanstalk_dotnet",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        features=["dotnet", "web", "https"],
    )

    plan = plan_changes(intent, "time4116/iac-smith-demo-infra")

    assert not any(
        p.endswith(".csproj") or p.endswith("Program.cs") for p in plan.files_to_generate
    )
    assert not any("application source under src/" in item for item in plan.summary)


def test_plan_uses_existing_environment_names_when_issue_does_not_pin_scope():
    intent = InfrastructureIntent(
        raw_request="Create a VPC foundation",
        resource_type="vpc_foundation",
        environment_scope=EnvironmentScope.BOTH,
        environments=["non-prod", "prod"],
        region="us-west-2",
        requires_new_vpc=True,
        features=["remote_state", "private_subnets"],
        assumptions=[
            "Generated both non-prod and prod because no environment scope was specified."
        ],
    )
    patterns = RepoPatterns(
        environments=["dev", "staging", "prod"],
        default_environment_names=["dev", "staging", "prod"],
    )

    plan = plan_changes(
        intent,
        target_repo="time4116/iac-smith-demo-infra",
        repo_patterns=patterns,
    )

    assert plan.environments == ["dev", "staging", "prod"]
    assert "environments/staging/vpc-foundation/terragrunt.hcl" in plan.files_to_generate


def test_plan_skips_module_scaffold_when_stack_already_exists_in_repo():
    intent = _intent("eks_fargate")
    patterns = RepoPatterns(
        existing_stack_paths=["modules/eks-fargate"],
    )

    plan = plan_changes(
        intent,
        target_repo="time4116/iac-smith-demo-infra",
        repo_patterns=patterns,
    )

    assert not any(path.startswith("modules/eks-fargate/") for path in plan.files_to_generate)
    assert any("Reusing existing" in s for s in plan.summary)


def test_plan_raises_for_blocked_intent():
    intent = InfrastructureIntent(
        raw_request="terraform apply",
        resource_type="",
        environment_scope=EnvironmentScope.PROD_ONLY,
        environments=["prod"],
        region="us-west-2",
        blocked=True,
        block_reason="Issue requests terraform apply directly.",
    )

    try:
        plan_changes(intent, target_repo="time4116/iac-smith-demo-infra")
    except ValueError as exc:
        assert "apply" in str(exc)
    else:
        raise AssertionError("blocked intent should not produce a change plan")


def test_plan_ecs_fargate_adds_foundation_stack_and_module():
    plan = plan_changes(
        _intent("ecs_fargate"),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert "environments/non-prod/foundation/terragrunt.hcl" in plan.files_to_generate
    assert "modules/foundation/main.tf" in plan.files_to_generate
    assert "modules/ecs-fargate/main.tf" in plan.files_to_generate
    assert (
        plan.backend_resources["non-prod"].bucket == "iac-smith-state-non-prod-iac-smith-demo-infra"
    )
    assert plan.backend_resources["non-prod"].lock_table == "iac-smith-lock-non-prod"


def test_plan_strips_stack_suffix_from_resource_type():
    plan = plan_changes(
        InfrastructureIntent(
            raw_request="Create ECS Fargate cluster",
            resource_type="ecs_fargate_stack",
            environment_scope=EnvironmentScope.NON_PROD_ONLY,
            environments=["non-prod"],
            region="us-west-2",
            requires_new_vpc=False,
            features=[],
        ),
        target_repo="time4116/iac-smith-demo-infra",
    )
    assert plan.stack_name == "ecs-fargate"
    assert "modules/ecs-fargate/main.tf" in plan.files_to_generate
    assert not any("ecs-fargate-stack" in p for p in plan.files_to_generate)


def test_plan_existing_foundation_applies_to_arbitrary_workload_stack():
    patterns = RepoPatterns(
        existing_stack_paths=["modules/foundation", "environments/non-prod/foundation"]
    )

    plan = plan_changes(
        _intent("worker_service"),
        target_repo="time4116/iac-smith-demo-infra",
        repo_patterns=patterns,
    )

    assert "environments/non-prod/foundation/terragrunt.hcl" not in plan.files_to_generate
    assert "modules/foundation/main.tf" not in plan.files_to_generate
    assert "environments/non-prod/worker-service/terragrunt.hcl" in plan.files_to_generate
    assert any("foundation" in item.lower() for item in plan.summary)
