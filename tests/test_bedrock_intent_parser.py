import json

import pytest

from iac_smith.bedrock_intent import (
    BedrockIntentClient,
    build_intent_prompt,
    parse_bedrock_intent_payload,
)
from iac_smith.models.intent import EnvironmentScope


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
                        "resource_type": "vpc_foundation",
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

    assert intent.resource_type == "vpc_foundation"
    assert intent.environment_scope == EnvironmentScope.NON_PROD_ONLY
    assert intent.environments == ["non-prod"]
    assert intent.region == "us-west-2"
    assert intent.requires_new_vpc is True


def test_parse_bedrock_intent_payload_accepts_any_resource_type():
    """resource_type is a free-form string — Bedrock can return anything."""
    for resource_type in ["rds_postgres", "s3_bucket", "aurora_cluster", "custom_thing"]:
        payload = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "resource_type": resource_type,
                            "environment_scope": "prod_only",
                            "environments": ["prod"],
                            "region": "us-west-2",
                            "requires_new_vpc": False,
                            "features": [],
                            "assumptions": [],
                            "warnings": [],
                            "blocked": False,
                            "block_reason": None,
                        }
                    ),
                }
            ]
        }
        intent = parse_bedrock_intent_payload(json.dumps(payload))
        assert intent.resource_type == resource_type
        assert intent.blocked is False


def test_intent_prompt_does_not_hardcode_resource_type_allowlist():
    prompt = build_intent_prompt("Create some AWS infrastructure")

    assert "There is no restricted list" in prompt
    assert "blocked=true" not in prompt or "only set blocked=true" in prompt.lower()
    assert "Always plan AWS infrastructure using best security practices" in prompt
    assert "weaker security" in prompt


def test_intent_prompt_only_blocks_apply_and_destroy_not_resource_types():
    prompt = build_intent_prompt("Create RDS Postgres")

    assert "applying infrastructure directly" in prompt
    assert "destroying resources" in prompt


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
                            "resource_type": "baseline",
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

    assert intent.resource_type == "baseline"
    assert runtime.calls[0]["modelId"] == "anthropic.test-model"
    body = json.loads(runtime.calls[0]["body"])
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "Return only JSON" in body["messages"][0]["content"]
