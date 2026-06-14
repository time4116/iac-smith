from iac_smith.graph import build_graph
from iac_smith.state import IaCSmithState


def test_graph_compiles_and_routes_supported_issue_to_validation_block_without_generated_files():
    graph = build_graph()
    result = graph.invoke(
        IaCSmithState(
            issue_number=12,
            issue_title="Create EKS Fargate infra",
            issue_body="Create AWS infrastructure for a non-prod EKS Fargate setup in us-west-2.",
            issue_url="https://github.com/time4116/iac-smith/issues/12",
            labels=["iac-smith"],
            target_repo="time4116/iac-smith-demo-infra",
        )
    )

    assert result["status"] == "blocked"
    assert result["intent"].supported_intent.value == "eks_fargate"
    assert result["change_plan"].stack_name == "eks-fargate"
    assert result["validation"].status.value == "failed"
    assert "pr_body" not in result


def test_graph_blocks_unsupported_issue_before_pr_writer():
    graph = build_graph()
    result = graph.invoke(
        IaCSmithState(
            issue_number=13,
            issue_title="Create database",
            issue_body="Create a production RDS PostgreSQL database open to the internet.",
            issue_url="https://github.com/time4116/iac-smith/issues/13",
            labels=["iac-smith"],
            target_repo="time4116/iac-smith-demo-infra",
        )
    )

    assert result["status"] == "blocked"
    assert result["pr_body"] is None
    assert result["intent"].blocked is True


def test_graph_ignores_unlabeled_issue_before_intent_parsing():
    graph = build_graph()
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
    graph = build_graph()
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
