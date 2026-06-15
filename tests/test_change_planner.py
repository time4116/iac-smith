from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent, SupportedIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.nodes.change_planner import plan_changes


def test_plan_eks_fargate_non_prod_structure():
    intent = InfrastructureIntent(
        raw_request="Create non-prod EKS Fargate",
        supported_intent=SupportedIntent.EKS_FARGATE,
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        requires_new_vpc=True,
        features=["remote_state", "private_subnets", "logging"],
    )

    plan = plan_changes(intent, target_repo="time4116/iac-smith-demo-infra")

    assert plan.stack_name == "eks-fargate"
    assert "bootstrap/backend/non-prod/main.tf" in plan.files_to_generate
    assert "live/non-prod/eks-fargate/terragrunt.hcl" in plan.files_to_generate
    assert "modules/eks-fargate/README.md" in plan.files_to_generate
    assert ".github/workflows/terraform-pr-check.yml" in plan.files_to_generate
    assert ".github/workflows/terraform-apply.yml" in plan.files_to_generate
    assert plan.backend_resources["non-prod"].bucket == "iac-smith-demo-infra-non-prod-tfstate"
    assert plan.backend_resources["non-prod"].lock_table == "iac-smith-demo-infra-non-prod-tflock"


def test_plan_uses_existing_environment_names_when_issue_does_not_pin_scope():
    intent = InfrastructureIntent(
        raw_request="Create a VPC foundation",
        supported_intent=SupportedIntent.VPC_FOUNDATION,
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
    assert "live/staging/vpc/terragrunt.hcl" in plan.files_to_generate


def test_plan_raises_for_blocked_intent():
    intent = InfrastructureIntent(
        raw_request="Create public RDS",
        supported_intent=SupportedIntent.UNSUPPORTED,
        environment_scope=EnvironmentScope.PROD_ONLY,
        environments=["prod"],
        region="us-west-2",
        blocked=True,
        block_reason="Unsupported request family",
    )

    try:
        plan_changes(intent, target_repo="time4116/iac-smith-demo-infra")
    except ValueError as exc:
        assert "Unsupported request family" in str(exc)
    else:
        raise AssertionError("blocked intent should not produce a change plan")
