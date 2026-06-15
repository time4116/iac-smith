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


def test_plan_derives_stack_name_from_resource_type():
    plan = plan_changes(_intent("eks_fargate"), target_repo="time4116/iac-smith-demo-infra")

    assert plan.stack_name == "eks-fargate"
    assert "bootstrap/backend/non-prod/main.tf" in plan.files_to_generate
    assert "live/non-prod/eks-fargate/terragrunt.hcl" in plan.files_to_generate
    assert "modules/eks-fargate/README.md" in plan.files_to_generate
    assert ".github/workflows/terraform-pr-check.yml" in plan.files_to_generate
    assert ".github/workflows/terraform-apply.yml" in plan.files_to_generate
    assert plan.backend_resources["non-prod"].bucket == "iac-smith-demo-infra-non-prod-tfstate"
    assert plan.backend_resources["non-prod"].lock_table == "iac-smith-demo-infra-non-prod-tflock"
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
    assert "live/staging/vpc-foundation/terragrunt.hcl" in plan.files_to_generate


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
