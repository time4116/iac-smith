import json
import os
from typing import Any, Protocol

from iac_smith.models.intent import InfrastructureIntent

INTENT_SCHEMA = """
{
  "resource_type": "snake_case infra label, e.g. vpc_foundation, eks_fargate, rds_postgres",
  "environment_scope": "non_prod_only | prod_only | both",
  "environments": ["non-prod"],
  "region": "us-west-2",
  "requires_new_vpc": true,
  "features": ["encryption", "private_subnets", "logging"],
  "assumptions": ["short factual assumption"],
  "warnings": [
    "short risk/gap/security concern — state what is absent or risky, not future actions"
  ],
  "blocked": false,
  "block_reason": null
}
""".strip()

# Structured-output contract so Bedrock returns a valid JSON object instead of
# prose or markdown. Models behind some inference profiles (e.g. the Sonnet
# global profile) do not reliably honour a prompt-only "return only JSON"
# instruction; forcing the shape here keeps intent parsing model-agnostic.
INTENT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resource_type": {"type": "string"},
        "environment_scope": {
            "type": "string",
            "enum": ["non_prod_only", "prod_only", "both"],
        },
        "environments": {"type": "array", "items": {"type": "string"}},
        "region": {"type": "string"},
        "requires_new_vpc": {"type": "boolean"},
        "features": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "blocked": {"type": "boolean"},
        "block_reason": {"type": ["string", "null"]},
    },
    "required": [
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
    ],
    "additionalProperties": False,
}


class BedrockRuntime(Protocol):
    def invoke_model_with_response_stream(self, **kwargs: Any) -> dict[str, Any]: ...


def _extract_json_object(text: str) -> dict[str, Any]:
    # On failure include a snippet of what the model actually returned: a
    # silently-ignored structured-output contract surfaces here as prose, and the
    # raw text is the only thing that tells us which model/profile misbehaved.
    snippet = text.strip()[:300]
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                f"Bedrock intent response must contain a valid JSON object; got: {snippet!r}"
            ) from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Bedrock intent response must contain a valid JSON object; got: {snippet!r}"
            ) from exc
    if not isinstance(value, dict):
        raise ValueError("Bedrock intent response must be a JSON object.")
    return value


def parse_bedrock_intent_text(text: str) -> InfrastructureIntent:
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

            region = os.getenv("AWS_REGION", "us-west-2")
            self._bedrock_runtime = boto3.client("bedrock-runtime", region_name=region)
        return self._bedrock_runtime

    def parse_issue(self, issue_text: str) -> InfrastructureIntent:
        # Stream the response: the Sonnet global inference profile honours the
        # output_config structured-output contract over the streaming endpoint
        # but silently ignores it on non-streaming InvokeModel (returning prose),
        # so intent must use the same streamed path that file generation does.
        from iac_smith.dynamic_terraform import _read_stream_document

        prompt = build_intent_prompt(issue_text)
        response = self.bedrock_runtime.invoke_model_with_response_stream(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1200,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                    "output_config": {
                        "format": {"type": "json_schema", "schema": INTENT_JSON_SCHEMA}
                    },
                }
            ),
        )
        text, _stop_reason = _read_stream_document(response)
        intent = parse_bedrock_intent_text(text)
        return intent.model_copy(update={"raw_request": issue_text})


def build_intent_prompt(issue_text: str) -> str:
    return f"""You are IaC Smith's infrastructure intent parser.

Map the GitHub issue into the exact JSON schema below. Return only JSON. Do not include markdown.

Rules:
* Identify the AWS infrastructure being requested and set resource_type to a short snake_case
  label, e.g. vpc_foundation, eks_fargate, rds_postgres, s3_bucket, lambda_function, baseline.
  There is no restricted list — use whatever best describes the request.
* Only set blocked=true when the issue explicitly requests an action IaC Smith must never do:
  applying infrastructure directly, destroying resources, or committing plaintext credentials.
  Do not block based on brittle resource-name checks.
* Always plan AWS infrastructure using best security practices, even when the issue asks for
  weaker security. Preserve the requested intent, but use secure defaults and add warnings
  that explain any deviation from what was asked.
* Warnings must describe a risk, gap, or security concern — not promise future actions.
  Write "No HTTPS listener configured; a certificate ARN is required to enable TLS" not
  "IaC Smith will add an HTTPS listener stub." If something is missing, say it is absent.
* Existing repository conventions are inspected later. Do not invent file paths.
* If no AWS region is specified, use us-west-2 and add a warning.
* If no environment is specified, use environment_scope=both and environments=["non-prod", "prod"].

Schema:
{INTENT_SCHEMA}

GitHub issue:
{issue_text}
"""
