import json

import pytest

from iac_smith.bedrock_intent import (
    BedrockIntentClient,
    build_intent_prompt,
    parse_bedrock_intent_text,
)
from iac_smith.models.intent import EnvironmentScope


def _stream_response(text: str, stop_reason: str = "end_turn") -> dict:
    """Shape a streamed InvokeModelWithResponseStream response carrying `text`."""
    return {
        "body": [
            {
                "chunk": {
                    "bytes": json.dumps(
                        {"type": "content_block_delta", "delta": {"text": text}}
                    ).encode()
                }
            },
            {
                "chunk": {
                    "bytes": json.dumps(
                        {"type": "message_stop", "stop_reason": stop_reason}
                    ).encode()
                }
            },
        ]
    }


class FakeBedrockRuntime:
    def __init__(self, text: str):
        self.text = text
        self.calls = []

    def invoke_model_with_response_stream(self, **kwargs):
        self.calls.append(kwargs)
        return _stream_response(self.text)


def _intent_text(**overrides) -> str:
    payload = {
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
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_bedrock_intent_text_rejects_non_json():
    with pytest.raises(ValueError, match="valid JSON object"):
        parse_bedrock_intent_text("Create a VPC")


def test_parse_bedrock_intent_text_error_includes_offending_text():
    with pytest.raises(ValueError, match="I cannot help with that"):
        parse_bedrock_intent_text("I cannot help with that")


def test_parse_bedrock_intent_text_accepts_plain_json():
    intent = parse_bedrock_intent_text(_intent_text())

    assert intent.resource_type == "vpc_foundation"
    assert intent.environment_scope == EnvironmentScope.NON_PROD_ONLY
    assert intent.environments == ["non-prod"]
    assert intent.region == "us-west-2"
    assert intent.requires_new_vpc is True


def test_parse_bedrock_intent_text_accepts_any_resource_type():
    """resource_type is a free-form string — Bedrock can return anything."""
    for resource_type in ["rds_postgres", "s3_bucket", "aurora_cluster", "custom_thing"]:
        intent = parse_bedrock_intent_text(
            _intent_text(resource_type=resource_type, environment_scope="prod_only")
        )
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


def test_bedrock_client_requires_model_id(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    with pytest.raises(ValueError, match="BEDROCK_MODEL_ID"):
        BedrockIntentClient(model_id="", bedrock_runtime=FakeBedrockRuntime(""))


def test_bedrock_client_invokes_configured_model_without_hardcoded_model_id():
    runtime = FakeBedrockRuntime(
        _intent_text(
            resource_type="baseline",
            environment_scope="both",
            environments=["non-prod", "prod"],
            region="us-east-1",
            requires_new_vpc=False,
            features=["remote_state"],
        )
    )

    client = BedrockIntentClient(model_id="anthropic.test-model", bedrock_runtime=runtime)
    intent = client.parse_issue("Bootstrap remote state")

    assert intent.resource_type == "baseline"
    assert runtime.calls[0]["modelId"] == "anthropic.test-model"
    body = json.loads(runtime.calls[0]["body"])
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "Return only JSON" in body["messages"][0]["content"]


def test_bedrock_client_streams_with_structured_json_output():
    runtime = FakeBedrockRuntime(_intent_text(resource_type="baseline"))

    client = BedrockIntentClient(model_id="anthropic.test-model", bedrock_runtime=runtime)
    client.parse_issue("Bootstrap remote state")

    body = json.loads(runtime.calls[0]["body"])
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["required"] == [
        "resource_type",
        "environment_scope",
        "environments",
        "region",
        "requires_new_vpc",
        "features",
        "assumptions",
        "warnings",
        "blocked",
        "block_reason",
    ]
