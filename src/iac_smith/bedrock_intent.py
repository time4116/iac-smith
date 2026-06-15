import json
import os
from typing import Any, Protocol

from iac_smith.models.intent import InfrastructureIntent

SUPPORTED_SCHEMA = """
{
  "supported_intent": "one of the supported request family names listed below",
  "environment_scope": "non_prod_only | prod_only | both",
  "environments": ["non-prod"],
  "region": "us-west-2",
  "requires_new_vpc": true,
  "features": ["remote_state", "private_subnets", "logging"],
  "assumptions": ["short factual assumption"],
  "warnings": ["short risk or ambiguity"],
  "blocked": false,
  "block_reason": null
}
""".strip()


class BedrockRuntime(Protocol):
    def invoke_model(self, **kwargs: Any) -> dict[str, Any]: ...


def _extract_text_from_bedrock_payload(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("content"), list):
        parts = []
        for block in payload["content"]:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "\n".join(parts)
    if isinstance(payload.get("outputText"), str):
        return payload["outputText"]
    if isinstance(payload.get("completion"), str):
        return payload["completion"]
    return json.dumps(payload)


def _extract_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Bedrock intent response must contain a valid JSON object.") from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("Bedrock intent response must contain a valid JSON object.") from exc
    if not isinstance(value, dict):
        raise ValueError("Bedrock intent response must be a JSON object.")
    return value


def parse_bedrock_intent_payload(raw_payload: str) -> InfrastructureIntent:
    payload = _extract_json_object(raw_payload)
    text = _extract_text_from_bedrock_payload(payload)
    intent_payload = _extract_json_object(text)
    if "raw_request" not in intent_payload:
        intent_payload["raw_request"] = ""
    return InfrastructureIntent.model_validate(intent_payload)


class BedrockIntentClient:
    def __init__(
        self,
        model_id: str | None = None,
        bedrock_runtime: BedrockRuntime | None = None,
    ) -> None:
        self.model_id = model_id or os.getenv("BEDROCK_MODEL_ID", "")
        if not self.model_id:
            raise ValueError(
                "BEDROCK_MODEL_ID must be set to a Bedrock model ID or inference profile ARN"
            )
        self._bedrock_runtime = bedrock_runtime

    @property
    def bedrock_runtime(self) -> BedrockRuntime:
        if self._bedrock_runtime is None:
            import boto3

            self._bedrock_runtime = boto3.client("bedrock-runtime")
        return self._bedrock_runtime

    def parse_issue(self, issue_text: str) -> InfrastructureIntent:
        prompt = build_intent_prompt(issue_text)
        response = self.bedrock_runtime.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1200,
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                        }
                    ],
                }
            ),
        )
        raw_body = response["body"].read().decode("utf-8")
        intent = parse_bedrock_intent_payload(raw_body)
        return intent.model_copy(update={"raw_request": issue_text})


def build_intent_prompt(issue_text: str) -> str:
    return f"""You are IaC Smith's infrastructure intent parser.

Map the GitHub issue into the exact JSON schema below. Return only JSON. Do not include markdown.

Supported MVP request families:
* baseline: Terraform/Terragrunt repo baseline, remote state, backend bootstrap
* vpc_foundation: AWS VPC foundation
* eks_fargate: AWS EKS Fargate foundation
* ecs_fargate: AWS ECS Fargate foundation
* rds_postgres: AWS RDS PostgreSQL database in private subnets with encryption enabled
* unsupported: anything outside the MVP boundary, including public SSH/RDP, plaintext
  secrets, or apply requests

Rules:
* Existing repository conventions are inspected later. Do not invent file paths.
* If the issue explicitly requests an unsupported or dangerous action, set blocked=true.
* If no AWS region is specified, use us-west-2 and add a warning.
* If no environment is specified, use environment_scope=both and environments=["non-prod", "prod"].
* Prefer private subnets unless public access is explicitly requested.
* For security-sensitive requests, preserve the user's requested intent and describe the risk in
  warnings. Do not block infrastructure changes based on brittle resource-name checks.
* Do not generate Terraform. Parse intent only.

Schema:
{SUPPORTED_SCHEMA}

GitHub issue:
{issue_text}
"""
