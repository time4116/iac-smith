from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.validation import ValidationResult, ValidationStatus
from iac_smith.nodes.change_planner import plan_changes
from iac_smith.nodes.pr_writer import branch_name_for_issue, build_pr_body


def test_branch_name_for_issue_is_stable_and_slugged():
    assert (
        branch_name_for_issue(12, "Create EKS Fargate Infra!!!")
        == "iac-smith/issue-12-create-eks-fargate-infra"
    )


def test_pr_body_contains_required_sections_and_no_apply_confirmation():
    intent = InfrastructureIntent(
        raw_request="Create non-prod EKS Fargate",
        resource_type="eks_fargate",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        requires_new_vpc=True,
        features=["remote_state"],
        assumptions=["Created a new VPC because no existing network was specified."],
        warnings=["Plan skipped because AWS credentials were unavailable."],
    )
    plan = plan_changes(intent, target_repo="time4116/iac-smith-demo-infra")
    validation = ValidationResult(
        status=ValidationStatus.PARTIAL,
        checks=["terragrunt hclfmt passed"],
    )

    body = build_pr_body(
        issue_url="https://github.com/time4116/iac-smith/issues/12",
        intent=intent,
        change_plan=plan,
        validation=validation,
    )

    assert "Source issue" in body
    assert "Generated infrastructure summary" in body
    assert "Assumptions and defaults" in body
    assert "Validation results" in body
    assert "Warnings and risks" in body
    assert "Expected post-merge apply behavior" in body
    assert "IaC Smith did not apply infrastructure" in body
