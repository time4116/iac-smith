import json

import pytest

from iac_smith.bedrock_intent import (
    BedrockIntentClient,
    build_intent_prompt,
    parse_bedrock_intent_payload,
)
from iac_smith.models.intent import EnvironmentScope, SupportedIntent


class FakeBedrockRuntime:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        return {"body": FakeBody(json.dumps(self.payload).encode())}


class FakeBody:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data


def test_parse_bedrock_intent_payload_rejects_non_json_text():
    with pytest.raises(ValueError, match="valid JSON object"):
        parse_bedrock_intent_payload("Create a VPC")


def test_parse_bedrock_intent_payload_accepts_anthropic_text_block_json():
    payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "supported_intent": "vpc_foundation",
                        "environment_scope": "non_prod_only",
                        "environments": ["non-prod"],
                        "region": "us-west-2",
                        "requires_new_vpc": True,
                        "features": ["private_subnets", "remote_state"],
                        "assumptions": ["No existing VPC was specified."],
                        "warnings": [],
                        "blocked": False,
                        "block_reason": None,
                    }
                ),
            }
        ]
    }

    intent = parse_bedrock_intent_payload(json.dumps(payload))

    assert intent.supported_intent == SupportedIntent.VPC_FOUNDATION
    assert intent.environment_scope == EnvironmentScope.NON_PROD_ONLY
    assert intent.environments == ["non-prod"]
    assert intent.region == "us-west-2"
    assert intent.requires_new_vpc is True


def test_parse_bedrock_intent_payload_accepts_rds_when_requested():
    payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "supported_intent": "rds_postgres",
                        "environment_scope": "prod_only",
                        "environments": ["prod"],
                        "region": "us-west-2",
                        "requires_new_vpc": False,
                        "features": ["postgres", "encrypted_storage", "private_subnets"],
                        "assumptions": ["Use AWS-managed master password."],
                        "warnings": [],
                        "blocked": False,
                        "block_reason": None,
                    }
                ),
            }
        ]
    }

    intent = parse_bedrock_intent_payload(json.dumps(payload))

    assert intent.supported_intent == SupportedIntent.RDS_POSTGRES
    assert intent.environment_scope == EnvironmentScope.PROD_ONLY
    assert intent.environments == ["prod"]


def test_intent_prompt_preserves_intent_but_requires_secure_aws_defaults():
    prompt = build_intent_prompt("Create public RDS Postgres open to the internet")

    assert "rds_postgres: AWS RDS PostgreSQL" in prompt
    assert "including databases" not in prompt
    assert "Always plan AWS infrastructure using best security practices" in prompt
    assert "weaker security" in prompt
    assert "Do not block infrastructure changes based on brittle resource-name checks" in prompt


def test_bedrock_client_requires_model_id():
    with pytest.raises(ValueError, match="BEDROCK_MODEL_ID"):
        BedrockIntentClient(model_id="", bedrock_runtime=FakeBedrockRuntime({}))


def test_bedrock_client_invokes_configured_model_without_hardcoded_model_id():
    runtime = FakeBedrockRuntime(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "supported_intent": "baseline",
                            "environment_scope": "both",
                            "environments": ["non-prod", "prod"],
                            "region": "us-east-1",
                            "requires_new_vpc": False,
                            "features": ["remote_state"],
                            "assumptions": [],
                            "warnings": [],
                            "blocked": False,
                            "block_reason": None,
                        }
                    ),
                }
            ]
        }
    )

    client = BedrockIntentClient(model_id="anthropic.test-model", bedrock_runtime=runtime)
    intent = client.parse_issue("Bootstrap remote state")

    assert intent.supported_intent == SupportedIntent.BASELINE
    assert runtime.calls[0]["modelId"] == "anthropic.test-model"
    body = json.loads(runtime.calls[0]["body"])
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "Return only JSON" in body["messages"][0]["content"]
