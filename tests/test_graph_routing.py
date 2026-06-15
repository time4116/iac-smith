from iac_smith.graph import build_graph
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.state import IaCSmithState


def _fake_intent_parser(issue_text: str) -> InfrastructureIntent:
    if "apply" in issue_text.lower():
        return InfrastructureIntent(
            raw_request=issue_text,
            resource_type="",
            environment_scope=EnvironmentScope.PROD_ONLY,
            environments=["prod"],
            region="us-west-2",
            blocked=True,
            block_reason="Issue requests terraform apply directly.",
        )
    return InfrastructureIntent(
        raw_request=issue_text,
        resource_type="eks_fargate",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        requires_new_vpc=True,
        features=["remote_state", "private_subnets"],
        assumptions=["Created a new VPC because no existing network was specified."],
    )


def _graph():
    return build_graph(intent_parser_fn=_fake_intent_parser)


def test_graph_compiles_and_routes_supported_issue_to_generated_pr_ready(tmp_path):
    graph = _graph()
    result = graph.invoke(
        IaCSmithState(
            issue_number=12,
            issue_title="Create EKS Fargate infra",
            issue_body="Create AWS infrastructure for a non-prod EKS Fargate setup in us-west-2.",
            issue_url="https://github.com/time4116/iac-smith/issues/12",
            labels=["iac-smith"],
            target_repo="time4116/iac-smith-demo-infra",
            target_repo_path=str(tmp_path),
        )
    )

    assert result["status"] == "pr_ready"
    assert result["intent"].resource_type == "eks_fargate"
    assert result["change_plan"].stack_name == "eks-fargate"
    assert result["validation"].status.value in {"passed", "partial"}
    assert "modules/eks-fargate/main.tf" in result["generated_files"]
    assert result["pr_body"] is not None


def test_graph_blocks_apply_request_before_pr_writer():
    graph = _graph()
    result = graph.invoke(
        IaCSmithState(
            issue_number=13,
            issue_title="Apply terraform",
            issue_body="Please apply terraform now.",
            issue_url="https://github.com/time4116/iac-smith/issues/13",
            labels=["iac-smith"],
            target_repo="time4116/iac-smith-demo-infra",
        )
    )

    assert result["status"] == "blocked"
    assert result.get("pr_body") is None
    assert result["intent"].blocked is True


def test_graph_ignores_unlabeled_issue_before_intent_parsing():
    graph = _graph()
    result = graph.invoke(
        IaCSmithState(
            issue_number=14,
            issue_title="Create EKS Fargate infra",
            issue_body="Create AWS infrastructure for a non-prod EKS Fargate setup in us-west-2.",
            issue_url="https://github.com/time4116/iac-smith/issues/14",
            labels=[],
            target_repo="time4116/iac-smith-demo-infra",
        )
    )

    assert result["status"] == "ignored"
    assert result["block_reason"] == "Missing iac-smith label"
    assert "intent" not in result
    assert "change_plan" not in result
    assert "pr_body" not in result


def test_graph_blocks_failed_static_review_before_pr_writer():
    graph = _graph()
    result = graph.invoke(
        IaCSmithState(
            issue_number=15,
            issue_title="Create EKS Fargate infra",
            issue_body="Create AWS infrastructure for a non-prod EKS Fargate setup in us-west-2.",
            issue_url="https://github.com/time4116/iac-smith/issues/15",
            labels=["iac-smith"],
            target_repo="time4116/iac-smith-demo-infra",
            generated_files={
                "live/non-prod/example/terragrunt.hcl": (
                    'remote_state { config = { key = "fixed.tfstate" } }'
                )
            },
        )
    )

    assert result["status"] == "blocked"
    assert "pr_body" not in result
    assert result["validation"].status.value == "failed"
