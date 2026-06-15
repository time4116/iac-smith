from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent, SupportedIntent
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
            supported_intent=SupportedIntent.EKS_FARGATE,
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
    assert intent.supported_intent == SupportedIntent.EKS_FARGATE
    assert intent.environment_scope == EnvironmentScope.NON_PROD_ONLY
    assert intent.environments == ["non-prod"]
    assert intent.region == "us-west-2"
    assert intent.requires_new_vpc is True
    assert "remote_state" in intent.features


def test_parse_intent_preserves_bedrock_defaults_for_unspecified_environment():
    client = FakeIntentClient(
        InfrastructureIntent(
            raw_request="",
            supported_intent=SupportedIntent.VPC_FOUNDATION,
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

    assert intent.supported_intent == SupportedIntent.VPC_FOUNDATION
    assert intent.environment_scope == EnvironmentScope.BOTH
    assert intent.environments == ["non-prod", "prod"]
    assert any("region defaulted" in warning.lower() for warning in intent.warnings)


def test_parse_intent_allows_rds_when_bedrock_classifies_it_as_supported():
    client = FakeIntentClient(
        InfrastructureIntent(
            raw_request="",
            supported_intent=SupportedIntent.RDS_POSTGRES,
            environment_scope=EnvironmentScope.PROD_ONLY,
            environments=["prod"],
            region="us-west-2",
            features=["postgres", "encrypted_storage", "private_subnets"],
            blocked=False,
        )
    )

    intent = parse_intent(
        "Create a production RDS PostgreSQL database.",
        intent_client=client,
    )

    assert intent.supported_intent == SupportedIntent.RDS_POSTGRES
    assert intent.blocked is False


def test_parse_intent_final_guard_blocks_public_database_exposure():
    client = FakeIntentClient(
        InfrastructureIntent(
            raw_request="",
            supported_intent=SupportedIntent.RDS_POSTGRES,
            environment_scope=EnvironmentScope.PROD_ONLY,
            environments=["prod"],
            region="us-west-2",
            blocked=False,
        )
    )

    intent = parse_intent(
        "Create a production RDS PostgreSQL database open to the internet.",
        intent_client=client,
    )

    assert intent.supported_intent == SupportedIntent.UNSUPPORTED
    assert intent.blocked is True
    assert intent.block_reason
