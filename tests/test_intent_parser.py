from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.nodes.intent_parser import parse_intent


class FakeIntentClient:
    def __init__(self, intent: InfrastructureIntent):
        self.intent = intent
        self.calls = []

    def parse_issue(self, issue_text: str) -> InfrastructureIntent:
        self.calls.append(issue_text)
        return self.intent.model_copy(update={"raw_request": issue_text})


def test_parse_intent_uses_mandatory_bedrock_client_result():
    client = FakeIntentClient(
        InfrastructureIntent(
            raw_request="",
            resource_type="eks_fargate",
            environment_scope=EnvironmentScope.NON_PROD_ONLY,
            environments=["non-prod"],
            region="us-west-2",
            requires_new_vpc=True,
            features=["remote_state", "private_subnets", "logging"],
            assumptions=["Private subnets are preferred."],
        )
    )

    intent = parse_intent(
        "Create AWS infrastructure for a non-prod EKS Fargate setup in us-west-2.",
        intent_client=client,
    )

    assert client.calls == [
        "Create AWS infrastructure for a non-prod EKS Fargate setup in us-west-2."
    ]
    assert intent.resource_type == "eks_fargate"
    assert intent.environment_scope == EnvironmentScope.NON_PROD_ONLY
    assert intent.environments == ["non-prod"]
    assert intent.region == "us-west-2"
    assert intent.requires_new_vpc is True
    assert "remote_state" in intent.features


def test_parse_intent_preserves_bedrock_defaults_for_unspecified_environment():
    client = FakeIntentClient(
        InfrastructureIntent(
            raw_request="",
            resource_type="vpc_foundation",
            environment_scope=EnvironmentScope.BOTH,
            environments=["non-prod", "prod"],
            region="us-west-2",
            warnings=["Region defaulted to us-west-2 because the issue did not specify one."],
        )
    )

    intent = parse_intent(
        "Create a VPC foundation with private subnets and remote state.",
        intent_client=client,
    )

    assert intent.resource_type == "vpc_foundation"
    assert intent.environment_scope == EnvironmentScope.BOTH
    assert intent.environments == ["non-prod", "prod"]
    assert any("region defaulted" in warning.lower() for warning in intent.warnings)


def test_parse_intent_passes_through_any_resource_type_without_blocking():
    """No resource type should be blocked by IaC Smith itself — that's the PR reviewer's job."""
    for resource_type in ["rds_postgres", "s3_bucket", "lambda_function", "aurora_cluster"]:
        client = FakeIntentClient(
            InfrastructureIntent(
                raw_request="",
                resource_type=resource_type,
                environment_scope=EnvironmentScope.PROD_ONLY,
                environments=["prod"],
                region="us-west-2",
                blocked=False,
            )
        )
        intent = parse_intent(f"Create {resource_type}", intent_client=client)
        assert intent.resource_type == resource_type
        assert intent.blocked is False
        assert intent.block_reason is None


def test_parse_intent_blocks_only_when_bedrock_sets_blocked():
    client = FakeIntentClient(
        InfrastructureIntent(
            raw_request="",
            resource_type="",
            environment_scope=EnvironmentScope.BOTH,
            environments=["non-prod", "prod"],
            region="us-west-2",
            blocked=True,
            block_reason="Issue requests terraform apply directly.",
        )
    )

    intent = parse_intent("Please apply terraform now.", intent_client=client)

    assert intent.blocked is True
    assert intent.block_reason == "Issue requests terraform apply directly."


def test_parse_intent_blocks_on_empty_resource_type_without_explicit_block():
    """Guard: if Bedrock returns no resource_type and didn't set blocked, we block it."""
    client = FakeIntentClient(
        InfrastructureIntent(
            raw_request="",
            resource_type="",
            environment_scope=EnvironmentScope.BOTH,
            environments=["non-prod", "prod"],
            region="us-west-2",
            blocked=False,
        )
    )

    intent = parse_intent("???", intent_client=client)

    assert intent.blocked is True
    assert intent.block_reason
