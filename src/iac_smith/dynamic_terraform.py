import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Protocol

from pydantic import BaseModel, Field

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.rules import Ruleset
from iac_smith.models.validation import ValidationStatus
from iac_smith.nodes.static_review import static_review_generated_files


def _path_needs_repair(path: str, errors: list[str]) -> bool:
    """Return True if `path` appears in any error as a file that needs to be changed.

    For duplicate-declaration errors the hint reads "Remove from X, keep in Y."
    The file at Y is the canonical one — it does not need repair.  Only X does.
    This function returns False when the path appears exclusively as a "keep in"
    target so that variables.tf / outputs.tf / versions.tf are not unnecessarily
    regenerated (which can drop declarations that main.tf still references).
    """
    for error in errors:
        if path not in error:
            continue
        if f"keep in {path}." in error and f"Remove from {path}," not in error:
            continue
        return True
    return False


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
    repair_errors: list[str] | None = None,
    previous_content: str | None = None,
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
    repair_section = ""
    if repair_errors:
        repair_section = f"""

Static review failures:
{json.dumps(repair_errors, indent=2)}

Runtime validation failures use this same repair path when errors came from
terraform fmt/init/validate, terragrunt hclfmt/init/validate, or terragrunt plan.

Previous generated content that failed review:
```text
{previous_content or ""}
```

Regenerate the same file path only. Fix every validation failure. Do not
repeat the failing pattern. Validation failures may come from static review,
terraform fmt/init/validate, terragrunt hclfmt/init/validate, or terragrunt plan.
"""
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
* Terraform apply workflows must never run on pull_request events or feature
  branches. `.github/workflows/terraform-apply.yml` may run only on push events
  to `main` or `master`.
* Prefer secure AWS defaults: private networking, encryption, least privilege,
  no dangerous public ingress.
* If a workload stack depends on foundation outputs for VPC/subnets, do not
  reference module.vpc unless that same module declares module "vpc".
* Generate complete, syntactically valid file bodies for each requested path.
  Do not use placeholder comments instead of Terraform resources when the issue
  asks for concrete infrastructure.
* **Terraform module file organization — CRITICAL: Do not duplicate
  declarations across files.** Each type of declaration belongs in exactly
  one file and must NOT be repeated in another file of the same module:
  - `variables.tf` — ONLY variable declarations (e.g. `variable "name"` defined here)
  - `outputs.tf` — ONLY output declarations (e.g. `output "name"` defined here)
  - `versions.tf` — ONLY terraform settings and required_providers
  - `main.tf` — resource and data source definitions (NOT variables,
    outputs, or required_providers — those go in their dedicated files)
* When files_to_generate includes both a `main.tf` and a `variables.tf`
  for the same module, put variables in `variables.tf` only, not in
  `main.tf`. Same rule applies to outputs.tf and versions.tf.
* When files_to_generate contains one path, return exactly that one file path
  in files. Use the full change_plan and repo_patterns as context, but do not
  include sibling planned files in the response.
* Generated GitHub Action workflows (e.g. `.github/workflows/terraform-pr-check.yml`
  and `.github/workflows/terraform-apply.yml`) must strictly align with the
  concrete directory structure in files_to_generate. For example, if the planned
  directories are under `environments/`, your workflows must trigger on `environments/**`
  and use `environments/` subdirectories as their job working-directories. Do not
  hallucinate independent folder structures such as `envs/`, `live/`, or `environments/non-prod`
  (without the trailing `environments/` prefix) that are not present in the files_to_generate list.
{repair_section}
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
        max_repair_attempts: int = 1,
        concurrency: int | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.model_id = model_id or os.getenv("BEDROCK_MODEL_ID", "")
        if not self.model_id:
            raise ValueError("BEDROCK_MODEL_ID must be set to generate Terraform with Bedrock.")
        self._bedrock_runtime = bedrock_runtime
        self.read_timeout_seconds = read_timeout_seconds
        self.max_attempts = max_attempts
        self.max_repair_attempts = max_repair_attempts
        configured_concurrency = concurrency or int(os.getenv("IAC_SMITH_BEDROCK_CONCURRENCY", "4"))
        self.concurrency = max(1, configured_concurrency)
        self.logger = logger

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

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
        repair_errors: list[str] | None = None,
        previous_content: str | None = None,
    ) -> str:
        single_file_plan = change_plan.model_copy(update={"files_to_generate": [path]})
        prompt = build_generation_prompt(
            intent=intent,
            change_plan=single_file_plan,
            repo_patterns=repo_patterns,
            ruleset=ruleset,
            target_repo=target_repo,
            repair_errors=repair_errors,
            previous_content=previous_content,
        )
        last_error: Exception | None = None
        for attempt in range(3):
            response = self._invoke_model_with_retries(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    {
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 16384,
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
            try:
                generated = parse_single_file_generation_payload(raw_body, expected_path=path)
                return generated.content
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                self._log(
                    f"IaC Smith: JSON parse failed for {path} (attempt {attempt + 1}/3): "
                    f"{len(raw_body)} chars received, error: {exc}"
                )
                # Truncation often means the content is too complex — ask the model
                # to be more concise on retry by appending a hint to the prompt
                if attempt == 0:
                    prompt += (
                        "\n\nYour previous response was truncated or contained invalid JSON. "
                        "Be more concise. Focus on essential resources only."
                    )
        raise ValueError(
            f"Failed to generate valid JSON for `{path}` after 3 attempts: {last_error}"
        )

    def _generate_reviewed_file(
        self,
        *,
        path: str,
        generated_files: dict[str, str],
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
        file_index: int,
        total_files: int,
    ) -> str:
        self._log(f"IaC Smith: generating file {file_index}/{total_files}: {path}")
        content = self._generate_planned_file(
            path=path,
            intent=intent,
            change_plan=change_plan,
            repo_patterns=repo_patterns,
            ruleset=ruleset,
            target_repo=target_repo,
        )
        for _attempt in range(self.max_repair_attempts + 1):
            validation = static_review_generated_files({**generated_files, path: content})
            if validation.status != ValidationStatus.FAILED:
                if _attempt:
                    self._log(f"IaC Smith: static review passed for {path} after repair.")
                else:
                    self._log(f"IaC Smith: static review passed for {path}.")
                return content
            self._log(f"IaC Smith: static review failed for {path}: {'; '.join(validation.errors)}")
            if _attempt >= self.max_repair_attempts:
                joined_errors = "; ".join(validation.errors)
                raise ValueError(f"Generated file `{path}` failed static review: {joined_errors}")
            self._log(f"IaC Smith: repairing file {file_index}/{total_files}: {path}")
            content = self._generate_planned_file(
                path=path,
                intent=intent,
                change_plan=change_plan,
                repo_patterns=repo_patterns,
                ruleset=ruleset,
                target_repo=target_repo,
                repair_errors=validation.errors,
                previous_content=content,
            )
        return content

    def generate_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
    ) -> dict[str, str]:
        generated_files: dict[str, str] = {}
        total_files = len(change_plan.files_to_generate)
        max_workers = min(self.concurrency, max(1, total_files))
        self._log(
            f"IaC Smith: generating {total_files} planned file(s) with Bedrock "
            f"using concurrency {max_workers}."
        )

        def generate_one(path: str, file_index: int) -> tuple[str, str]:
            self._log(f"IaC Smith: generating file {file_index}/{total_files}: {path}")
            return path, self._generate_planned_file(
                path=path,
                intent=intent,
                change_plan=change_plan,
                repo_patterns=repo_patterns,
                ruleset=ruleset,
                target_repo=target_repo,
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(generate_one, path, file_index): path
                for file_index, path in enumerate(change_plan.files_to_generate, start=1)
            }
            for future in as_completed(futures):
                path, content = future.result()
                generated_files[path] = content

        generated_files = {path: generated_files[path] for path in change_plan.files_to_generate}

        for repair_attempt in range(self.max_repair_attempts + 1):
            validation = static_review_generated_files(generated_files)
            if validation.status != ValidationStatus.FAILED:
                for path in change_plan.files_to_generate:
                    suffix = " after repair" if repair_attempt else ""
                    self._log(f"IaC Smith: static review passed for {path}{suffix}.")
                self._log(f"IaC Smith: generated {len(generated_files)} file(s).")
                return generated_files

            self._log("IaC Smith: static review failed: " + "; ".join(validation.errors))
            if repair_attempt >= self.max_repair_attempts:
                joined_errors = "; ".join(validation.errors)
                raise ValueError(f"Generated files failed static review: {joined_errors}")

            paths_to_repair = [
                path
                for path in change_plan.files_to_generate
                if _path_needs_repair(path, validation.errors)
            ] or list(change_plan.files_to_generate)
            path_positions = {
                path: index for index, path in enumerate(change_plan.files_to_generate, start=1)
            }
            self._log(
                f"IaC Smith: repairing {len(paths_to_repair)} file(s) after static review failure."
            )
            repair_errors = list(validation.errors)
            previous_files = dict(generated_files)

            def repair_one(
                path: str,
                file_index: int,
                repair_errors: list[str] = repair_errors,
                previous_files: dict[str, str] = previous_files,
            ) -> tuple[str, str]:
                self._log(f"IaC Smith: repairing file {file_index}/{total_files}: {path}")
                return path, self._generate_planned_file(
                    path=path,
                    intent=intent,
                    change_plan=change_plan,
                    repo_patterns=repo_patterns,
                    ruleset=ruleset,
                    target_repo=target_repo,
                    repair_errors=repair_errors,
                    previous_content=previous_files[path],
                )

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(repair_one, path, path_positions[path]): path
                    for path in paths_to_repair
                }
                for future in as_completed(futures):
                    path, content = future.result()
                    generated_files[path] = content

            generated_files = {
                path: generated_files[path] for path in change_plan.files_to_generate
            }

        return generated_files

    def repair_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
        generated_files: dict[str, str],
        repair_errors: list[str],
    ) -> dict[str, str]:
        """Repair generated files using runtime validation/plan failures.

        Runtime validation happens after files are written into the target repo,
        so failures can include provider/schema/Terragrunt errors that static
        review cannot catch. Feed those exact errors back to Bedrock and repair
        only the implicated files when possible, falling back to the full planned
        file set when the error is cross-file or pathless.
        """

        paths_to_repair = [
            path
            for path in change_plan.files_to_generate
            if _path_needs_repair(path, repair_errors)
        ] or list(change_plan.files_to_generate)
        total_files = len(change_plan.files_to_generate)
        path_positions = {
            path: index for index, path in enumerate(change_plan.files_to_generate, start=1)
        }
        max_workers = min(self.concurrency, max(1, len(paths_to_repair)))
        repaired_files = dict(generated_files)
        self._log(
            f"IaC Smith: repairing {len(paths_to_repair)} file(s) after runtime validation failure."
        )

        def repair_one(path: str) -> tuple[str, str]:
            self._log(f"IaC Smith: repairing file {path_positions[path]}/{total_files}: {path}")
            return path, self._generate_planned_file(
                path=path,
                intent=intent,
                change_plan=change_plan,
                repo_patterns=repo_patterns,
                ruleset=ruleset,
                target_repo=target_repo,
                repair_errors=repair_errors,
                previous_content=generated_files[path],
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(repair_one, path): path for path in paths_to_repair}
            for future in as_completed(futures):
                path, content = future.result()
                repaired_files[path] = content

        return {path: repaired_files[path] for path in change_plan.files_to_generate}
