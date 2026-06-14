from iac_smith.models.intent import EnvironmentScope, SupportedIntent
from iac_smith.nodes.intent_parser import parse_intent


def test_parse_non_prod_eks_fargate_request():
    intent = parse_intent(
        "Create AWS infrastructure for a non-prod EKS Fargate setup in us-west-2. "
        "Use a new VPC, private subnets, standard tags, remote state, and basic logging."
    )

    assert intent.supported_intent == SupportedIntent.EKS_FARGATE
    assert intent.environment_scope == EnvironmentScope.NON_PROD_ONLY
    assert intent.environments == ["non-prod"]
    assert intent.region == "us-west-2"
    assert intent.requires_new_vpc is True
    assert "remote_state" in intent.features
    assert any("private subnet" in assumption.lower() for assumption in intent.assumptions)


def test_parse_unspecified_environment_defaults_to_both_and_region_warning():
    intent = parse_intent("Create a VPC foundation with private subnets and remote state.")

    assert intent.supported_intent == SupportedIntent.VPC_FOUNDATION
    assert intent.environment_scope == EnvironmentScope.BOTH
    assert intent.environments == ["non-prod", "prod"]
    assert intent.region == "us-west-2"
    assert any("region defaulted" in warning.lower() for warning in intent.warnings)


def test_parse_unsupported_database_request_refuses_mvp():
    intent = parse_intent("Create a production RDS PostgreSQL database open to the internet.")

    assert intent.supported_intent == SupportedIntent.UNSUPPORTED
    assert intent.blocked is True
    assert intent.block_reason
