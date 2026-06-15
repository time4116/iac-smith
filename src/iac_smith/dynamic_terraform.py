import json
import os
from typing import Any, Protocol

from pydantic import BaseModel, Field

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.rules import Ruleset


class BedrockRuntime(Protocol):
    def invoke_model(self, **kwargs: Any) -> dict[str, Any]: ...


class GeneratedTerraform(BaseModel):
    files: dict[str, str]
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GeneratedTerraformFile(BaseModel):
    path: str
    content: str
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


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
            raise ValueError(
                "Terraform generation response must contain a valid JSON object."
            ) from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Terraform generation response must contain a valid JSON object."
            ) from exc
    if not isinstance(value, dict):
        raise ValueError("Terraform generation response must be a JSON object.")
    return value


def _rules_payload(ruleset: Ruleset | None) -> list[dict[str, str]]:
    if not ruleset:
        return []
    return [
        {
            "id": rule.id,
            "category": rule.category,
            "severity": rule.severity.value,
            "description": rule.description,
        }
        for rule in ruleset.rules
    ]


def build_generation_prompt(
    *,
    intent: InfrastructureIntent,
    change_plan: ChangePlan,
    repo_patterns: RepoPatterns,
    ruleset: Ruleset | None,
    target_repo: str,
) -> str:
    context = {
        "target_repo": target_repo,
        "intent": intent.model_dump(mode="json"),
        "change_plan": change_plan.model_dump(mode="json"),
        "repo_patterns": repo_patterns.model_dump(mode="json"),
        "rules": _rules_payload(ruleset),
        "files_to_generate": change_plan.files_to_generate,
    }
    shape = '{"path": "path/to/file.tf", "content": "file body", "assumptions": [], "warnings": []}'
    return f"""You are IaC Smith's Terraform/Terragrunt generator.

Generate reviewable Terraform and Terragrunt file contents from structured issue
intent, repository patterns, and the active ruleset.

Non-negotiable rules:
* Return only JSON. Do not include markdown.
* Use this exact top-level shape: {shape}.
* Do not generate files outside files_to_generate.
* Existing repository conventions win over IaC Smith defaults unless the issue
  explicitly says not to follow them.
* Follow every active rule. Error-severity rules are hard requirements. Warning
  and preference rules must be followed unless they conflict with the explicit
  issue request or existing repo convention; explain conflicts in warnings.
* Never apply infrastructure, destroy resources, or include plaintext credentials.
* Prefer secure AWS defaults: private networking, encryption, least privilege,
  no dangerous public ingress.
* If a workload stack depends on foundation outputs for VPC/subnets, do not
  reference module.vpc unless that same module declares module "vpc".
* Generate complete, syntactically valid file bodies for each requested path.
  Do not use placeholder comments instead of Terraform resources when the issue
  asks for concrete infrastructure.
* When files_to_generate contains one path, return exactly that one file path
  in files. Use the full change_plan and repo_patterns as context, but do not
  include sibling planned files in the response.

Generation context JSON:
{json.dumps(context, indent=2)}
"""


def parse_generation_payload(raw_payload: str, allowed_paths: list[str]) -> GeneratedTerraform:
    payload = _extract_json_object(raw_payload)
    text = _extract_text_from_bedrock_payload(payload)
    generated = GeneratedTerraform.model_validate(_extract_json_object(text))
    allowed = set(allowed_paths)
    for path in generated.files:
        if path not in allowed:
            raise ValueError(f"Terraform generation returned unplanned file path `{path}`.")
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"Terraform generation returned unsafe file path `{path}`.")
    missing = sorted(allowed - set(generated.files))
    if missing:
        raise ValueError(f"Terraform generation is missing planned file `{missing[0]}`.")
    return generated


def parse_single_file_generation_payload(
    raw_payload: str, *, expected_path: str
) -> GeneratedTerraformFile:
    payload = _extract_json_object(raw_payload)
    text = _extract_text_from_bedrock_payload(payload)
    generated = GeneratedTerraformFile.model_validate(_extract_json_object(text))
    if generated.path != expected_path:
        raise ValueError(f"Terraform generation returned unplanned file path `{generated.path}`.")
    if generated.path.startswith("/") or ".." in generated.path.split("/"):
        raise ValueError(f"Terraform generation returned unsafe file path `{generated.path}`.")
    return generated


TERRAFORM_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The single planned file path."},
        "content": {
            "type": "string",
            "description": "Complete Terraform or Terragrunt file content.",
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short factual assumptions used while generating this file.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short risks, conflicts, or ambiguities for review.",
        },
    },
    "required": ["path", "content", "assumptions", "warnings"],
    "additionalProperties": False,
}


class BedrockTerraformGenerator:
    def __init__(
        self,
        model_id: str | None = None,
        bedrock_runtime: BedrockRuntime | None = None,
        *,
        read_timeout_seconds: int = 240,
        max_attempts: int = 3,
    ) -> None:
        self.model_id = model_id or os.getenv("BEDROCK_MODEL_ID", "")
        if not self.model_id:
            raise ValueError("BEDROCK_MODEL_ID must be set to generate Terraform with Bedrock.")
        self._bedrock_runtime = bedrock_runtime
        self.read_timeout_seconds = read_timeout_seconds
        self.max_attempts = max_attempts

    @property
    def bedrock_runtime(self) -> BedrockRuntime:
        if self._bedrock_runtime is None:
            import boto3
            from botocore.config import Config

            region = os.getenv("AWS_REGION", "us-west-2")
            self._bedrock_runtime = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=Config(
                    connect_timeout=10,
                    read_timeout=self.read_timeout_seconds,
                    retries={"max_attempts": self.max_attempts, "mode": "standard"},
                ),
            )
        return self._bedrock_runtime

    def _invoke_model_with_retries(self, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )

        retryable_errors = (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )
        last_error: Exception | None = None
        for _attempt in range(1, self.max_attempts + 1):
            try:
                return self.bedrock_runtime.invoke_model(**kwargs)
            except retryable_errors as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _generate_planned_file(
        self,
        *,
        path: str,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
    ) -> str:
        single_file_plan = change_plan.model_copy(update={"files_to_generate": [path]})
        prompt = build_generation_prompt(
            intent=intent,
            change_plan=single_file_plan,
            repo_patterns=repo_patterns,
            ruleset=ruleset,
            target_repo=target_repo,
        )
        response = self._invoke_model_with_retries(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 8000,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                    "output_config": {
                        "format": {
                            "type": "json_schema",
                            "schema": TERRAFORM_FILE_SCHEMA,
                        }
                    },
                }
            ),
        )
        raw_body = response["body"].read().decode("utf-8")
        generated = parse_single_file_generation_payload(raw_body, expected_path=path)
        return generated.content

    def generate_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
    ) -> dict[str, str]:
        return {
            path: self._generate_planned_file(
                path=path,
                intent=intent,
                change_plan=change_plan,
                repo_patterns=repo_patterns,
                ruleset=ruleset,
                target_repo=target_repo,
            )
            for path in change_plan.files_to_generate
        }
