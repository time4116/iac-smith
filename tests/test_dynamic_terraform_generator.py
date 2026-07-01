import json
import threading
import time

import botocore.exceptions
import pytest

from iac_smith.dynamic_terraform import (
    BedrockTerraformGenerator,
    _build_apply_workflow,
    _build_pr_check_workflow,
    _dedup_module_declarations,
    _extract_module_names,
    _normalize_child_terragrunt,
    _path_needs_repair,
    _repair_unit_key,
    _strip_orphan_foundation_dependency,
    _wire_foundation_dependency,
    build_generation_prompt,
    parse_generation_payload,
    parse_single_file_generation_payload,
)
from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.rules import Rule, Ruleset, RuleSeverity


class FakeBody:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data


def _stream_events(text: str, stop_reason: str = "end_turn") -> list[dict]:
    """Wrap a full document into Bedrock InvokeModelWithResponseStream events."""

    def _ev(obj: dict) -> dict:
        return {"chunk": {"bytes": json.dumps(obj).encode()}}

    return [
        _ev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}),
        _ev({"type": "message_delta", "delta": {"stop_reason": stop_reason}}),
        _ev({"type": "message_stop"}),
    ]


class FakeBedrockRuntime:
    def __init__(
        self,
        files: dict[str, str],
        failures_before_success: int = 0,
        repairs: dict[str, str] | None = None,
    ):
        self.files = files
        self.repairs = repairs or {}
        self.failures_before_success = failures_before_success
        self.calls = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures_before_success:
            raise botocore.exceptions.ReadTimeoutError(
                endpoint_url="https://example.invalid/model/test/invoke",
                error="timed out",
            )
        body = json.loads(kwargs["body"])
        prompt = body["messages"][0]["content"]
        context = json.loads(prompt.split("Generation context JSON:\n", 1)[1])
        requested_paths = context["files_to_generate"]
        path = requested_paths[0]
        content = (
            self.repairs.get(path, self.files[path])
            if "Static review failures:" in prompt
            else self.files[path]
        )
        return {
            "body": FakeBody(
                json.dumps(
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "path": path,
                                        "content": content,
                                        "assumptions": ["Used repository rules."],
                                        "warnings": [],
                                    }
                                ),
                            }
                        ]
                    }
                ).encode()
            )
        }

    def invoke_model_with_response_stream(self, **kwargs):
        # File generation streams; reuse the non-streaming logic (call tracking,
        # failures_before_success, repairs) and wrap its text as stream events so
        # subclasses overriding invoke_model keep working unchanged.
        response = self.invoke_model(**kwargs)
        payload = json.loads(response["body"].read().decode("utf-8"))
        text = "".join(
            block.get("text", "") for block in payload.get("content", []) if isinstance(block, dict)
        )
        return {"body": _stream_events(text)}


class BlockingBedrockRuntime(FakeBedrockRuntime):
    def __init__(self, files: dict[str, str]):
        super().__init__(files)
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def invoke_model(self, **kwargs):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            return super().invoke_model(**kwargs)
        finally:
            with self.lock:
                self.active -= 1


def _intent() -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request=(
            "Create an ECS Fargate cluster in non-prod using the existing foundation pattern."
        ),
        resource_type="ecs_fargate",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        requires_new_vpc=False,
        features=["ecs", "fargate"],
    )


def _plan() -> ChangePlan:
    return ChangePlan(
        stack_name="ecs-fargate",
        environments=["non-prod"],
        files_to_generate=[
            "environments/non-prod/ecs-fargate/terragrunt.hcl",
            "modules/ecs-fargate/main.tf",
            "modules/ecs-fargate/variables.tf",
            "modules/ecs-fargate/outputs.tf",
        ],
        backend_resources={"non-prod": BackendResource(bucket="state", lock_table="lock")},
        summary=["Generate ECS Fargate from issue intent and repo rules"],
    )


def _ruleset() -> Ruleset:
    return Ruleset(
        rules=[
            Rule(
                id="workload-modules-depend-on-foundation",
                category="terraform",
                severity=RuleSeverity.WARNING,
                description=(
                    "Workload modules consume foundation outputs instead of declaring VPCs."
                ),
            )
        ]
    )


def test_generation_prompt_contains_rules_repo_patterns_and_requested_paths():
    prompt = build_generation_prompt(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(
            uses_terragrunt=True,
            environments=["non-prod"],
            existing_stack_paths=["modules/foundation", "environments/non-prod/foundation"],
            module_sources=["terraform-aws-modules/vpc/aws"],
            preferred_layout="terragrunt_live_modules",
        ),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert "workload-modules-depend-on-foundation" in prompt
    assert "Workload modules consume foundation outputs" in prompt
    assert "modules/foundation" in prompt
    assert "terraform-aws-modules/vpc/aws" in prompt
    assert "environments/non-prod/ecs-fargate/terragrunt.hcl" in prompt
    assert "Return only JSON" in prompt
    assert "Do not generate files outside files_to_generate" in prompt
    assert "top-level key listed" in prompt
    assert "Nested object keys are not inputs" in prompt
    assert "preserve existing" in prompt
    # Module READMEs must carry terraform-docs markers (canonical shape + rule).
    assert "<!-- BEGIN_TF_DOCS -->" in prompt
    assert "<!-- END_TF_DOCS -->" in prompt
    # required_providers must stay out of terragrunt generate blocks (canonical + rule).
    assert "required_providers placement" in prompt
    assert 'generate "provider"' in prompt
    assert "Provider schema errors are authoritative" in prompt
    assert "Do not special-case one AWS service" in prompt
    assert "aws_apprunner_service" not in prompt


def test_repair_prompt_treats_provider_schema_errors_as_dynamic_constraints():
    provider_error = (
        'Error: expected image_identifier to match regex "^public.ecr.aws/.+", '
        "got ghcr.io/example/app:latest"
    )
    prompt = build_generation_prompt(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=None,
        target_repo="time4116/iac-smith-demo-infra",
        repair_errors=[provider_error],
        previous_content='image_identifier = "ghcr.io/example/app:latest"\n',
    )

    assert "expected image_identifier to match regex" in prompt
    assert "ghcr.io/example/app:latest" in prompt
    assert "schema constraints as the source of truth" in prompt
    assert "exact regex, enum, type, range" in prompt
    assert "Do not special-case one AWS service" in prompt


def test_generation_prompt_includes_existing_file_content_when_provided():
    prompt = build_generation_prompt(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=None,
        target_repo="time4116/iac-smith-demo-infra",
        existing_content="# existing README\n## ECS Fargate\nPrevious section.\n",
    )

    assert "existing README" in prompt
    assert "Preserve all existing content" in prompt
    assert "Do not start from scratch" in prompt


def test_generation_prompt_omits_existing_section_when_not_provided():
    prompt = build_generation_prompt(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=None,
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert "Preserve all existing content" not in prompt


def test_parse_generation_payload_accepts_anthropic_json_files():
    payload = json.dumps(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "files": {
                                "modules/example/main.tf": 'resource "aws_s3_bucket" "this" {}\n'
                            },
                            "assumptions": [],
                            "warnings": [],
                        }
                    ),
                }
            ]
        }
    )

    result = parse_generation_payload(payload, allowed_paths=["modules/example/main.tf"])

    assert result.files == {"modules/example/main.tf": 'resource "aws_s3_bucket" "this" {}\n'}


def test_parse_generation_payload_rejects_unplanned_file_paths():
    payload = json.dumps(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "files": {"../escape.tf": "bad", "modules/example/main.tf": "ok"},
                            "assumptions": [],
                            "warnings": [],
                        }
                    ),
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="unplanned file path"):
        parse_generation_payload(payload, allowed_paths=["modules/example/main.tf"])


def test_parse_generation_payload_rejects_missing_planned_files():
    payload = json.dumps(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "files": {"modules/example/main.tf": "ok"},
                            "assumptions": [],
                            "warnings": [],
                        }
                    ),
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="missing planned file"):
        parse_generation_payload(
            payload,
            allowed_paths=["modules/example/main.tf", "modules/example/outputs.tf"],
        )


def test_parse_single_file_generation_payload_accepts_structured_output_shape():
    payload = json.dumps(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "path": "modules/example/main.tf",
                            "content": 'resource "aws_s3_bucket" "this" {}\n',
                            "assumptions": [],
                            "warnings": [],
                        }
                    ),
                }
            ]
        }
    )

    result = parse_single_file_generation_payload(payload, expected_path="modules/example/main.tf")

    assert result.path == "modules/example/main.tf"
    assert result.content == 'resource "aws_s3_bucket" "this" {}\n'


def test_parse_single_file_generation_payload_rejects_wrong_path():
    payload = json.dumps(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "path": "modules/wrong/main.tf",
                            "content": "bad",
                            "assumptions": [],
                            "warnings": [],
                        }
                    ),
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="unplanned file path"):
        parse_single_file_generation_payload(payload, expected_path="modules/example/main.tf")


def test_bedrock_terraform_generator_returns_model_generated_files_without_renderer_map():
    files = {
        "modules/ecs-fargate/main.tf": (
            'resource "aws_ecs_cluster" "this" { name = var.name_prefix }\n'
        ),
        "modules/ecs-fargate/variables.tf": 'variable "name_prefix" { type = string }\n',
        "modules/ecs-fargate/outputs.tf": (
            'output "cluster_name" { value = aws_ecs_cluster.this.name }\n'
        ),
        "environments/non-prod/ecs-fargate/terragrunt.hcl": (
            'terraform { source = "../../../modules/ecs-fargate" }\n'
            'inputs = { name_prefix = "test" }\n'
        ),
    }
    runtime = FakeBedrockRuntime(files)
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
    )

    result = generator.generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert result == files
    assert len(runtime.calls) == len(files)
    assert runtime.calls[0]["modelId"] == "anthropic.test-model"
    body = json.loads(runtime.calls[0]["body"])
    assert body["temperature"] == 0
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == [
        "path",
        "content",
        "assumptions",
        "warnings",
    ]
    assert "workload-modules-depend-on-foundation" in body["messages"][0]["content"]
    first_context = json.loads(
        body["messages"][0]["content"].split("Generation context JSON:\n", 1)[1]
    )
    assert first_context["files_to_generate"] == [
        "environments/non-prod/ecs-fargate/terragrunt.hcl"
    ]


def test_generate_files_renders_root_hcl_deterministically():
    from iac_smith.dynamic_terraform import _render_root_hcl

    plan = ChangePlan(
        stack_name="ecs-fargate",
        environments=["non-prod"],
        files_to_generate=[
            "environments/non-prod/root.hcl",
            "environments/non-prod/ecs-fargate/terragrunt.hcl",
            "modules/ecs-fargate/main.tf",
            "modules/ecs-fargate/variables.tf",
            "modules/ecs-fargate/outputs.tf",
        ],
        backend_resources={"non-prod": BackendResource(bucket="my-state", lock_table="my-lock")},
        summary=["Generate ECS Fargate"],
    )
    files = {
        "modules/ecs-fargate/main.tf": (
            'resource "aws_ecs_cluster" "this" { name = var.name_prefix }\n'
        ),
        "modules/ecs-fargate/variables.tf": 'variable "name_prefix" { type = string }\n',
        "modules/ecs-fargate/outputs.tf": (
            'output "cluster_name" { value = aws_ecs_cluster.this.name }\n'
        ),
        "environments/non-prod/ecs-fargate/terragrunt.hcl": (
            'terraform { source = "../../../modules/ecs-fargate" }\n'
            'inputs = { name_prefix = "test" }\n'
        ),
    }
    runtime = FakeBedrockRuntime(files)
    generator = BedrockTerraformGenerator(model_id="anthropic.test-model", bedrock_runtime=runtime)

    result = generator.generate_files(
        intent=_intent(),
        change_plan=plan,
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    # root.hcl is rendered deterministically, not by the model.
    assert result["environments/non-prod/root.hcl"] == _render_root_hcl(
        environment="non-prod",
        aws_region="us-west-2",
        bucket="my-state",
        lock_table="my-lock",
    )
    # The model was asked for the four non-envelope files only — never root.hcl.
    requested: list[str] = []
    for call in runtime.calls:
        body = json.loads(call["body"])
        ctx = json.loads(body["messages"][0]["content"].split("Generation context JSON:\n", 1)[1])
        requested.extend(ctx["files_to_generate"])
    assert "environments/non-prod/root.hcl" not in requested
    assert len(runtime.calls) == len(files)


def test_generate_files_renders_foundation_from_registry_module():
    from iac_smith.nodes.static_review import static_review_generated_files

    plan = ChangePlan(
        stack_name="foundation",
        environments=["non-prod"],
        files_to_generate=[
            "environments/non-prod/root.hcl",
            "environments/non-prod/foundation/terragrunt.hcl",
            "modules/foundation/main.tf",
            "modules/foundation/variables.tf",
            "modules/foundation/outputs.tf",
            "modules/foundation/versions.tf",
        ],
        backend_resources={"non-prod": BackendResource(bucket="b", lock_table="l")},
        summary=["foundation"],
    )
    # Every planned file is deterministic envelope — the model is never called.
    runtime = FakeBedrockRuntime({})
    generator = BedrockTerraformGenerator(model_id="anthropic.test-model", bedrock_runtime=runtime)

    result = generator.generate_files(
        intent=_intent(),
        change_plan=plan,
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert runtime.calls == []
    main = result["modules/foundation/main.tf"]
    assert 'source  = "terraform-aws-modules/vpc/aws"' in main
    assert 'version = "~> 5.0"' in main
    outputs = result["modules/foundation/outputs.tf"]
    # Outputs match the names workloads consume via dependency.foundation.outputs.*
    assert "value       = module.vpc.vpc_id" in outputs
    assert "value       = module.vpc.private_subnets" in outputs
    assert "value       = module.vpc.public_subnets" in outputs
    assert "value       = module.vpc.vpc_cidr_block" in outputs
    # The foundation stack carries the deterministic source and a normalized envelope.
    stack = result["environments/non-prod/foundation/terragrunt.hcl"]
    assert 'source = "../../../modules/foundation"' in stack
    assert 'include "root" {' in stack
    assert 'environment = "non-prod"' in stack
    # The deterministic foundation passes static review on its own.
    assert static_review_generated_files(result).errors == []


def test_render_root_hcl_emits_locals_remote_state_and_provider():
    from iac_smith.dynamic_terraform import _render_root_hcl

    rendered = _render_root_hcl(
        environment="non-prod",
        aws_region="us-west-2",
        bucket="my-state",
        lock_table="my-lock",
    )
    assert 'environment = "non-prod"' in rendered
    assert 'aws_region  = "us-west-2"' in rendered
    assert 'bucket         = "my-state"' in rendered
    assert 'dynamodb_table = "my-lock"' in rendered
    # Terragrunt interpolations are emitted verbatim, not evaluated/escaped.
    assert "${path_relative_to_include()}/terraform.tfstate" in rendered
    assert 'region = "${local.aws_region}"' in rendered


def test_invoke_file_generation_streams_and_concatenates_text_deltas():
    path = "modules/example/main.tf"
    full_doc = json.dumps(
        {"path": path, "content": 'resource "x" "y" {}\n', "assumptions": [], "warnings": []}
    )
    # The document arrives as several text_delta events that must be concatenated.
    first, second = full_doc[: len(full_doc) // 2], full_doc[len(full_doc) // 2 :]

    class StreamingRuntime:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def invoke_model_with_response_stream(self, **kwargs):
            self.calls.append(kwargs)

            def _ev(obj: dict) -> dict:
                return {"chunk": {"bytes": json.dumps(obj).encode()}}

            return {
                "body": [
                    _ev({"type": "content_block_delta", "delta": {"text": first}}),
                    _ev({"type": "content_block_delta", "delta": {"text": second}}),
                    _ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
                    _ev({"type": "message_stop"}),
                ]
            }

    runtime = StreamingRuntime()
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
    )

    document = generator._invoke_file_generation(prompt="PROMPT", path=path)

    assert document == full_doc
    assert len(runtime.calls) == 1
    body = json.loads(runtime.calls[0]["body"])
    # Streaming requests still use structured output and a single user turn.
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["messages"][-1]["role"] == "user"


def test_invoke_file_generation_logs_when_response_hits_max_tokens():
    path = "modules/example/main.tf"

    class TruncatedStreamRuntime:
        def invoke_model_with_response_stream(self, **kwargs):
            def _ev(obj: dict) -> dict:
                return {"chunk": {"bytes": json.dumps(obj).encode()}}

            return {
                "body": [
                    _ev({"type": "content_block_delta", "delta": {"text": '{"path":'}}),
                    _ev({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}),
                ]
            }

    logs: list[str] = []
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=TruncatedStreamRuntime(),
        logger=logs.append,
    )

    document = generator._invoke_file_generation(prompt="PROMPT", path=path)

    assert document == '{"path":'
    assert any("max_tokens" in line for line in logs)


def _chunk_event(obj: dict) -> dict:
    return {"chunk": {"bytes": json.dumps(obj).encode()}}


def test_read_stream_document_raises_on_bedrock_exception_member():
    from iac_smith.dynamic_terraform import BedrockStreamError, _read_stream_document

    response = {
        "body": [
            _chunk_event({"type": "content_block_delta", "delta": {"text": '{"path":'}}),
            {"throttlingException": {"message": "slow down"}},
        ]
    }
    with pytest.raises(BedrockStreamError) as exc_info:
        _read_stream_document(response)
    assert exc_info.value.member == "throttlingException"
    assert exc_info.value.transient is True


def test_invoke_file_generation_retries_on_transient_stream_error_member():
    path = "modules/example/main.tf"
    full_doc = json.dumps(
        {"path": path, "content": 'resource "x" "y" {}\n', "assumptions": [], "warnings": []}
    )

    class FlakyStreamRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def invoke_model_with_response_stream(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                # A mid-stream timeout must drive the transient retry path, not be
                # dropped as a short document that burns the parse-retry budget.
                return {"body": [{"modelTimeoutException": {"message": "timed out"}}]}
            return {
                "body": [
                    _chunk_event({"type": "content_block_delta", "delta": {"text": full_doc}}),
                    _chunk_event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
                ]
            }

    runtime = FlakyStreamRuntime()
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
    )

    document = generator._invoke_file_generation(prompt="PROMPT", path=path)

    assert document == full_doc
    assert runtime.calls == 2


def test_invoke_file_generation_does_not_retry_non_transient_stream_error():
    from iac_smith.dynamic_terraform import BedrockStreamError

    class ValidationStreamRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def invoke_model_with_response_stream(self, **kwargs):
            self.calls += 1
            return {"body": [{"validationException": {"message": "bad input"}}]}

    runtime = ValidationStreamRuntime()
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
    )

    with pytest.raises(BedrockStreamError) as exc_info:
        generator._invoke_file_generation(prompt="PROMPT", path="modules/example/main.tf")
    assert exc_info.value.member == "validationException"
    assert runtime.calls == 1


def test_invoke_file_generation_retries_when_stream_iteration_raises():
    path = "modules/example/main.tf"
    full_doc = json.dumps(
        {"path": path, "content": 'resource "x" "y" {}\n', "assumptions": [], "warnings": []}
    )

    class _RaisingStream:
        def __iter__(self):
            raise botocore.exceptions.ReadTimeoutError(
                endpoint_url="https://example.invalid/model/test/invoke-with-response-stream",
                error="timed out",
            )

    class StreamReadFailsRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def invoke_model_with_response_stream(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                # Error raised while CONSUMING the stream — after the call returned.
                return {"body": _RaisingStream()}
            return {
                "body": [
                    _chunk_event({"type": "content_block_delta", "delta": {"text": full_doc}}),
                    _chunk_event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
                ]
            }

    runtime = StreamReadFailsRuntime()
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
    )

    document = generator._invoke_file_generation(prompt="PROMPT", path=path)

    assert document == full_doc
    assert runtime.calls == 2


def test_invoke_file_generation_retries_when_stream_raises_urllib3_read_timeout():
    # Regression: a mid-stream stall surfaces as urllib3.exceptions.ReadTimeoutError,
    # which botocore does NOT wrap during EventStream iteration. It must be retried,
    # not allowed to escape and crash the run.
    import urllib3.exceptions

    path = "modules/example/main.tf"
    full_doc = json.dumps(
        {"path": path, "content": 'resource "x" "y" {}\n', "assumptions": [], "warnings": []}
    )

    class _RaisingStream:
        def __iter__(self):
            raise urllib3.exceptions.ReadTimeoutError(
                None, "https://example.invalid", "Read timed out."
            )

    class StreamReadFailsRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def invoke_model_with_response_stream(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"body": _RaisingStream()}
            return {
                "body": [
                    _chunk_event({"type": "content_block_delta", "delta": {"text": full_doc}}),
                    _chunk_event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
                ]
            }

    runtime = StreamReadFailsRuntime()
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
    )

    document = generator._invoke_file_generation(prompt="PROMPT", path=path)

    assert document == full_doc
    assert runtime.calls == 2


def test_bedrock_terraform_generator_generates_files_with_bounded_parallelism():
    files = {
        "modules/ecs-fargate/main.tf": (
            'resource "aws_ecs_cluster" "this" { name = var.name_prefix }\n'
        ),
        "modules/ecs-fargate/variables.tf": 'variable "name_prefix" { type = string }\n',
        "modules/ecs-fargate/outputs.tf": (
            'output "cluster_name" { value = aws_ecs_cluster.this.name }\n'
        ),
        "environments/non-prod/ecs-fargate/terragrunt.hcl": (
            'terraform { source = "../../../modules/ecs-fargate" }\n'
            'inputs = { name_prefix = "test" }\n'
        ),
    }
    runtime = BlockingBedrockRuntime(files)
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
        concurrency=2,
    )

    result = generator.generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert result == files
    assert runtime.max_active == 2


def test_bedrock_terraform_generator_retries_transient_read_timeouts():
    files = {
        "modules/ecs-fargate/main.tf": (
            'resource "aws_ecs_cluster" "this" { name = var.name_prefix }\n'
        ),
        "modules/ecs-fargate/variables.tf": 'variable "name_prefix" { type = string }\n',
        "modules/ecs-fargate/outputs.tf": (
            'output "cluster_name" { value = aws_ecs_cluster.this.name }\n'
        ),
        "environments/non-prod/ecs-fargate/terragrunt.hcl": (
            'terraform { source = "../../../modules/ecs-fargate" }\n'
            'inputs = { name_prefix = "test" }\n'
        ),
    }
    runtime = FakeBedrockRuntime(files, failures_before_success=1)
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
    )

    result = generator.generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert result == files
    assert len(runtime.calls) == len(files) + 1


def _client_error(code: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": code}}, "InvokeModel"
    )


def test_invoke_model_retries_throttling_then_succeeds():
    calls = []

    class Client:
        def invoke_model(self, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise _client_error("ThrottlingException")
            return {"ok": True}

    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model", bedrock_runtime=Client(), max_attempts=3
    )

    assert generator._invoke_model_with_retries(modelId="anthropic.test-model") == {"ok": True}
    assert len(calls) == 2


def test_invoke_model_does_not_retry_non_throttle_client_error():
    calls = []

    class Client:
        def invoke_model(self, **kwargs):
            calls.append(1)
            raise _client_error("AccessDeniedException")

    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model", bedrock_runtime=Client(), max_attempts=3
    )

    with pytest.raises(botocore.exceptions.ClientError):
        generator._invoke_model_with_retries(modelId="anthropic.test-model")
    assert len(calls) == 1


def test_invoke_model_retries_are_bounded_by_max_attempts():
    calls = []

    class Client:
        def invoke_model(self, **kwargs):
            calls.append(1)
            raise botocore.exceptions.ReadTimeoutError(
                endpoint_url="https://example.invalid/model/test/invoke", error="timed out"
            )

    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model", bedrock_runtime=Client(), max_attempts=2
    )

    with pytest.raises(botocore.exceptions.ReadTimeoutError):
        generator._invoke_model_with_retries(modelId="anthropic.test-model")
    # No nesting: exactly max_attempts calls, not max_attempts * botocore_attempts.
    assert len(calls) == 2


def test_bedrock_terraform_generator_repairs_file_after_static_review_failure():
    bad_terragrunt = 'remote_state { config = { key = "fixed.tfstate" } }\n'
    fixed_terragrunt = (
        'remote_state { config = { key = "${path_relative_to_include()}/terraform.tfstate" } }\n'
    )
    files = {
        "environments/non-prod/ecs-fargate/terragrunt.hcl": bad_terragrunt,
        "modules/ecs-fargate/main.tf": (
            'resource "aws_ecs_cluster" "this" { name = var.name_prefix }\n'
        ),
        "modules/ecs-fargate/variables.tf": 'variable "name_prefix" { type = string }\n',
        "modules/ecs-fargate/outputs.tf": (
            'output "cluster_name" { value = aws_ecs_cluster.this.name }\n'
        ),
    }
    runtime = FakeBedrockRuntime(
        files,
        repairs={"environments/non-prod/ecs-fargate/terragrunt.hcl": fixed_terragrunt},
    )
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
    )

    result = generator.generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert result["environments/non-prod/ecs-fargate/terragrunt.hcl"] == fixed_terragrunt
    assert len(runtime.calls) == len(files) + 1
    repair_body = next(
        json.loads(call["body"])
        for call in runtime.calls
        if "Static review failures:" in json.loads(call["body"])["messages"][0]["content"]
    )
    assert "Static review failures:" in repair_body["messages"][0]["content"]
    assert "path_relative_to_include" in repair_body["messages"][0]["content"]


def test_bedrock_terraform_generator_logs_generation_and_repair_progress():
    bad_terragrunt = 'remote_state { config = { key = "fixed.tfstate" } }\n'
    fixed_terragrunt = (
        'remote_state { config = { key = "${path_relative_to_include()}/terraform.tfstate" } }\n'
    )
    files = {
        "environments/non-prod/ecs-fargate/terragrunt.hcl": bad_terragrunt,
        "modules/ecs-fargate/main.tf": (
            'resource "aws_ecs_cluster" "this" { name = var.name_prefix }\n'
        ),
        "modules/ecs-fargate/variables.tf": 'variable "name_prefix" { type = string }\n',
        "modules/ecs-fargate/outputs.tf": (
            'output "cluster_name" { value = aws_ecs_cluster.this.name }\n'
        ),
    }
    messages: list[str] = []
    runtime = FakeBedrockRuntime(
        files,
        repairs={"environments/non-prod/ecs-fargate/terragrunt.hcl": fixed_terragrunt},
    )
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
        logger=messages.append,
    )

    generator.generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert messages[0] == (
        "IaC Smith: generating 4 planned file(s) with Bedrock"
        " (model: anthropic.test-model, concurrency: 4)."
    )
    assert (
        "IaC Smith: generating file 1/4: environments/non-prod/ecs-fargate/terragrunt.hcl"
        in messages
    )
    assert any("static review found issues" in message for message in messages)
    assert (
        "IaC Smith: repairing file 1/4: environments/non-prod/ecs-fargate/terragrunt.hcl"
        in messages
    )
    assert (
        "IaC Smith: static review passed for "
        "environments/non-prod/ecs-fargate/terragrunt.hcl after repair." in messages
    )
    assert messages[-1] == "IaC Smith: generated 4 file(s)."


def test_bedrock_terraform_generator_returns_best_effort_after_unrepaired_static_review_failure():
    files = {
        "environments/non-prod/ecs-fargate/terragrunt.hcl": (
            'remote_state { config = { key = "fixed.tfstate" } }\n'
        ),
        "modules/ecs-fargate/main.tf": (
            'resource "aws_ecs_cluster" "this" { name = var.name_prefix }\n'
        ),
        "modules/ecs-fargate/variables.tf": 'variable "name_prefix" { type = string }\n',
        "modules/ecs-fargate/outputs.tf": (
            'output "cluster_name" { value = aws_ecs_cluster.this.name }\n'
        ),
    }
    runtime = FakeBedrockRuntime(files)
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
        max_repair_attempts=1,
    )

    # Non-convergence must not crash the run: best-effort files are returned so
    # the graph's validation_runner and the real terraform/terragrunt validation
    # in cli.py become the gate, rather than a static-review check killing the run.
    result = generator.generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert set(result) == set(files)
    # Initial generation of every file, then one repair round before the
    # oscillation guard halts (the unrepaired error recurs identically).
    assert len(runtime.calls) == len(files) + 1


def test_bedrock_terraform_generator_keeps_previous_content_when_repair_returns_wrong_path():
    """A single file failing to repair (model returns an unplanned path) must not
    crash the whole run — the file keeps its previous content and downstream
    validation gates."""
    bad_terragrunt = 'remote_state { config = { key = "fixed.tfstate" } }\n'
    files = {
        "environments/non-prod/ecs-fargate/terragrunt.hcl": bad_terragrunt,
        "modules/ecs-fargate/main.tf": (
            'resource "aws_ecs_cluster" "this" { name = var.name_prefix }\n'
        ),
        "modules/ecs-fargate/variables.tf": 'variable "name_prefix" { type = string }\n',
        "modules/ecs-fargate/outputs.tf": (
            'output "cluster_name" { value = aws_ecs_cluster.this.name }\n'
        ),
    }

    class WrongPathOnRepairRuntime(FakeBedrockRuntime):
        def invoke_model(self, **kwargs):
            self.calls.append(kwargs)
            prompt = json.loads(kwargs["body"])["messages"][0]["content"]
            requested = json.loads(prompt.split("Generation context JSON:\n", 1)[1])
            path = requested["files_to_generate"][0]
            # During repair, return a payload for a different planned file so
            # parse_single_file_generation_payload rejects it as unplanned.
            returned = path
            if "Static review failures:" in prompt:
                returned = next(p for p in files if p != path)
            return {
                "body": FakeBody(
                    json.dumps(
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "path": returned,
                                            "content": files[returned],
                                            "assumptions": [],
                                            "warnings": [],
                                        }
                                    ),
                                }
                            ]
                        }
                    ).encode()
                )
            }

    runtime = WrongPathOnRepairRuntime(files)
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
        max_repair_attempts=1,
    )

    result = generator.generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert set(result) == set(files)
    # The unrepairable file kept its previous (best-effort) content.
    assert result["environments/non-prod/ecs-fargate/terragrunt.hcl"] == bad_terragrunt


def test_bedrock_terraform_generator_uses_extended_bedrock_timeout(monkeypatch):
    created_clients = []

    class FakeBoto3:
        def client(self, service_name, **kwargs):
            created_clients.append((service_name, kwargs))
            return FakeBedrockRuntime(
                {
                    "modules/ecs-fargate/main.tf": "main",
                    "modules/ecs-fargate/variables.tf": "variables",
                    "modules/ecs-fargate/outputs.tf": "outputs",
                    "environments/non-prod/ecs-fargate/terragrunt.hcl": "terragrunt",
                }
            )

    monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3())
    generator = BedrockTerraformGenerator(model_id="anthropic.test-model")

    _ = generator.bedrock_runtime

    service_name, kwargs = created_clients[0]
    assert service_name == "bedrock-runtime"
    assert kwargs["region_name"] == "us-west-2"
    assert kwargs["config"].read_timeout >= 180
    # botocore's own retries are disabled: _invoke_model_with_retries is the single
    # retry authority, so they must not nest and multiply the worst-case wall time.
    assert kwargs["config"].retries["max_attempts"] == 1
    assert generator.max_attempts >= 2


class TestRepairUnitKey:
    def test_module_and_its_stack_share_a_unit(self):
        # A module and its Terragrunt stack must repair together so variables.tf
        # and the stack's inputs converge instead of oscillating.
        assert _repair_unit_key("modules/ecs-fargate/variables.tf") == "stack:ecs-fargate"
        assert (
            _repair_unit_key("environments/non-prod/ecs-fargate/terragrunt.hcl")
            == "stack:ecs-fargate"
        )

    def test_deeper_environment_path_keys_on_stack_dir(self):
        assert (
            _repair_unit_key("environments/non-prod/group/ecs-fargate/terragrunt.hcl")
            == "stack:ecs-fargate"
        )

    def test_distinct_stacks_do_not_share_a_unit(self):
        assert _repair_unit_key("modules/foundation/main.tf") != _repair_unit_key(
            "modules/ecs-fargate/main.tf"
        )

    def test_non_stack_paths_key_on_directory(self):
        assert _repair_unit_key("environments/terragrunt.hcl") == "dir:environments"
        assert (
            _repair_unit_key("bootstrap/backend/non-prod/main.tf")
            == "dir:bootstrap/backend/non-prod"
        )
        # Environment-level config is not a stack and must not pair with a module.
        assert (
            _repair_unit_key("environments/non-prod/terragrunt.hcl") == "dir:environments/non-prod"
        )


class TestPathNeedsRepair:
    def test_returns_true_for_remove_from_target(self):
        errors = [
            'Variable "env" declared in multiple files of module `modules/foundation`: '
            "modules/foundation/main.tf, modules/foundation/variables.tf. "
            "Remove from modules/foundation/main.tf, keep in modules/foundation/variables.tf."
        ]
        assert _path_needs_repair("modules/foundation/main.tf", errors) is True

    def test_returns_false_for_keep_in_target_only(self):
        errors = [
            'Variable "env" declared in multiple files of module `modules/foundation`: '
            "modules/foundation/main.tf, modules/foundation/variables.tf. "
            "Remove from modules/foundation/main.tf, keep in modules/foundation/variables.tf."
        ]
        assert _path_needs_repair("modules/foundation/variables.tf", errors) is False

    def test_returns_false_when_path_not_in_any_error(self):
        errors = [
            'Variable "env" declared in multiple files of module `modules/foundation`: '
            "modules/foundation/main.tf, modules/foundation/variables.tf. "
            "Remove from modules/foundation/main.tf, keep in modules/foundation/variables.tf."
        ]
        assert _path_needs_repair("modules/other/main.tf", errors) is False

    def test_returns_true_for_non_duplicate_error_mentioning_path(self):
        errors = [
            "Terragrunt state key in `environments/non-prod/ecs-fargate/terragrunt.hcl` "
            "must use path_relative_to_include()."
        ]
        assert (
            _path_needs_repair("environments/non-prod/ecs-fargate/terragrunt.hcl", errors) is True
        )

    def test_returns_true_when_path_appears_as_both_remove_and_keep(self):
        errors = [
            'Variable "x" declared in multiple files: a.tf, modules/foundation/variables.tf. '
            "Remove from modules/foundation/variables.tf, keep in a.tf.",
            'Variable "y" declared in multiple files: modules/foundation/variables.tf, b.tf. '
            "Remove from c.tf, keep in modules/foundation/variables.tf.",
        ]
        assert _path_needs_repair("modules/foundation/variables.tf", errors) is True

    def test_returns_true_when_keep_in_target_has_other_errors(self):
        errors = [
            'Variable "env" declared in multiple files of module `modules/foundation`: '
            "modules/foundation/main.tf, modules/foundation/variables.tf. "
            "Remove from modules/foundation/main.tf, keep in modules/foundation/variables.tf.",
            'Variable "project" is referenced via var.project in `modules/foundation` '
            '(modules/foundation/variables.tf) but no variable "project" is declared.',
        ]
        assert _path_needs_repair("modules/foundation/variables.tf", errors) is True

    def test_pinpointed_error_repairs_only_the_blamed_file(self):
        # Terraform pinpoints the exact file ("on main.tf line 42"); only that
        # file is repaired so a sibling that already validated is never
        # regenerated (and regressed) by the repair model.
        errors = [
            "terraform validate modules/ecs-fargate failed in `modules/ecs-fargate`:\n"
            "│ Error: Reference to undeclared resource\n"
            "│   on main.tf line 42\n"
        ]
        assert _path_needs_repair("modules/ecs-fargate/main.tf", errors) is True
        assert _path_needs_repair("modules/ecs-fargate/variables.tf", errors) is False

    def test_unsupported_block_pinpoint_spares_unrelated_module_files(self):
        # Regression guard for issue #40: a schema error in main.tf must not drag
        # variables.tf / outputs.tf / README.md into repair.
        errors = [
            "terraform validate modules/dynamodb-table failed in `modules/dynamodb-table`:\n"
            "│ Error: Unsupported block type\n"
            '│   on main.tf line 5, in resource "aws_dynamodb_table" "feature_flags":\n'
            "│    5:   stream_specification {\n"
            '│ Blocks of type "stream_specification" are not expected here.\n'
        ]
        assert _path_needs_repair("modules/dynamodb-table/main.tf", errors) is True
        assert _path_needs_repair("modules/dynamodb-table/variables.tf", errors) is False
        assert _path_needs_repair("modules/dynamodb-table/outputs.tf", errors) is False
        assert _path_needs_repair("modules/dynamodb-table/README.md", errors) is False

    def test_stack_plan_error_reaches_sourced_module_tf_files(self):
        # A terragrunt plan failure names the stack dir; the offending value lives
        # in modules/<stack>, so the module's .tf files must be in scope.
        errors = [
            "terragrunt plan environments/non-prod/app-runner-open-webui failed in "
            "`environments/non-prod/app-runner-open-webui`:\n"
            "│ Error: expected image_identifier to match regular expression ...\n"
        ]
        assert _path_needs_repair("modules/app-runner-open-webui/main.tf", errors) is True
        assert _path_needs_repair("modules/app-runner-open-webui/variables.tf", errors) is True
        # An unrelated module must not be pulled in.
        assert _path_needs_repair("modules/foundation/main.tf", errors) is False

    def test_stack_bridge_does_not_touch_module_non_tf_files(self):
        errors = [
            "terragrunt plan environments/non-prod/app-runner-open-webui failed in "
            "`environments/non-prod/app-runner-open-webui`:\n│ Error: ...\n"
        ]
        assert _path_needs_repair("modules/app-runner-open-webui/README.md", errors) is False

    def test_directory_error_without_pinpoint_repairs_whole_unit(self):
        # A directory-level error with no "on <file> line N" pinpoint (e.g. a
        # missing-provider init failure) still falls back to whole-unit repair.
        errors = [
            "terraform init modules/ecs-fargate failed in `modules/ecs-fargate`:\n"
            "│ Error: Failed to install provider\n"
        ]
        assert _path_needs_repair("modules/ecs-fargate/main.tf", errors) is True
        assert _path_needs_repair("modules/ecs-fargate/variables.tf", errors) is True

    def test_directory_match_does_not_match_sibling_module(self):
        # A runtime error for modules/ecs-fargate must not implicate modules/foundation.
        errors = [
            "terraform validate modules/ecs-fargate failed in `modules/ecs-fargate`:\n"
            "│ Error: Reference to undeclared resource\n"
        ]
        assert _path_needs_repair("modules/foundation/main.tf", errors) is False

    def test_directory_match_does_not_match_parent_directory(self):
        # `environments` must not match an error about `environments/non-prod/foundation`.
        errors = [
            "terragrunt init environments/non-prod/foundation failed in "
            "`environments/non-prod/foundation`:\n│ Error: Duplicate required providers\n"
        ]
        assert _path_needs_repair("environments/terragrunt.hcl", errors) is False

    def test_keep_in_exclusion_not_bypassed_by_directory_match(self):
        # If a file is explicitly marked "keep in", the directory match must not
        # override that and cause it to be regenerated.
        errors = [
            "required_providers block found in multiple files of module `modules/foundation`: "
            "modules/foundation/main.tf, modules/foundation/versions.tf. "
            "Remove from modules/foundation/main.tf, keep in modules/foundation/versions.tf."
        ]
        assert _path_needs_repair("modules/foundation/versions.tf", errors) is False

    def test_undeclared_variable_repairs_variables_tf_not_main_tf(self):
        # The fix for an undeclared variable is to add the declaration to
        # variables.tf — main.tf, which only references it, must be left alone.
        errors = [
            "var.alb_name is referenced in modules/ecs-fargate/main.tf "
            'but "alb_name" is not declared in modules/ecs-fargate/variables.tf. '
            'Add variable "alb_name" to modules/ecs-fargate/variables.tf.'
        ]
        assert _path_needs_repair("modules/ecs-fargate/variables.tf", errors) is True
        assert _path_needs_repair("modules/ecs-fargate/main.tf", errors) is False

    def test_main_tf_still_repaired_when_it_has_an_independent_error(self):
        # The undeclared-variable exclusion must not mask an unrelated error that
        # genuinely requires regenerating main.tf.
        errors = [
            "var.alb_name is referenced in modules/ecs-fargate/main.tf "
            'but "alb_name" is not declared in modules/ecs-fargate/variables.tf. '
            'Add variable "alb_name" to modules/ecs-fargate/variables.tf.',
            "Terragrunt state key in `modules/ecs-fargate/main.tf` "
            "must use path_relative_to_include().",
        ]
        assert _path_needs_repair("modules/ecs-fargate/main.tf", errors) is True


def test_variables_tf_not_repaired_when_only_main_tf_has_duplicate_declarations():
    """When main.tf duplicates variable decls from variables.tf, the deterministic
    `_dedup_module_declarations` normalizer strips the main.tf copies before static
    review — so the duplicate is resolved with no repair round at all, and
    variables.tf is never regenerated (regenerating it can drop declarations like
    var.project/var.region that main.tf still references).
    """
    main_tf_bad = (
        'variable "env" { type = string }\n'
        'resource "aws_vpc" "this" { cidr_block = var.vpc_cidr }\n'
        'resource "aws_internet_gateway" "this"'
        " { tags = { Project = var.project, Region = var.region } }\n"
    )
    main_tf_fixed = (
        'resource "aws_vpc" "this" { cidr_block = var.vpc_cidr }\n'
        'resource "aws_internet_gateway" "this"'
        " { tags = { Project = var.project, Region = var.region } }\n"
    )
    variables_tf = (
        'variable "env" { type = string }\n'
        'variable "vpc_cidr" { type = string }\n'
        'variable "project" { type = string }\n'
        'variable "region" { type = string }\n'
    )

    files = {
        "modules/networking/main.tf": main_tf_bad,
        "modules/networking/variables.tf": variables_tf,
        "modules/networking/outputs.tf": 'output "vpc_id" { value = aws_vpc.this.id }\n',
    }

    call_count_by_path: dict[str, int] = {}

    class TrackingBedrockRuntime:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def invoke_model(self, **kwargs):
            self.calls.append(kwargs)
            body = json.loads(kwargs["body"])
            prompt = body["messages"][0]["content"]
            context = json.loads(prompt.split("Generation context JSON:\n", 1)[1])
            path = context["files_to_generate"][0]
            call_count_by_path[path] = call_count_by_path.get(path, 0) + 1
            in_repair = "Static review failures:" in prompt
            content = (
                main_tf_fixed
                if (in_repair and path == "modules/networking/main.tf")
                else files[path]
            )
            return {
                "body": FakeBody(
                    json.dumps(
                        {"path": path, "content": content, "assumptions": [], "warnings": []}
                    ).encode()
                )
            }

        def invoke_model_with_response_stream(self, **kwargs):
            text = self.invoke_model(**kwargs)["body"].read().decode("utf-8")
            return {"body": _stream_events(text)}

    runtime = TrackingBedrockRuntime()
    plan = ChangePlan(
        stack_name="networking",
        environments=["non-prod"],
        files_to_generate=list(files),
        backend_resources={},
        summary=["networking module"],
    )
    generator = BedrockTerraformGenerator(
        model_id="anthropic.test-model",
        bedrock_runtime=runtime,
    )

    result = generator.generate_files(
        intent=_intent(),
        change_plan=plan,
        repo_patterns=RepoPatterns(),
        ruleset=_ruleset(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert result["modules/networking/main.tf"] == main_tf_fixed
    assert result["modules/networking/variables.tf"] == variables_tf
    # Deterministic dedup fixes main.tf up front, so no repair round is needed:
    # each file is generated exactly once.
    assert call_count_by_path.get("modules/networking/main.tf", 0) == 1
    assert call_count_by_path.get("modules/networking/variables.tf", 0) == 1


class TestExtractModuleNames:
    def test_returns_module_names_in_plan_order(self):
        files = [
            "modules/foundation/main.tf",
            "modules/foundation/variables.tf",
            "modules/ecs-fargate/main.tf",
            "modules/ecs-fargate/outputs.tf",
            "environments/non-prod/ecs-fargate/terragrunt.hcl",
        ]
        assert _extract_module_names(files) == ["foundation", "ecs-fargate"]

    def test_deduplicates_same_module(self):
        files = ["modules/foo/main.tf", "modules/foo/variables.tf", "modules/foo/outputs.tf"]
        assert _extract_module_names(files) == ["foo"]

    def test_ignores_non_module_paths(self):
        files = [
            ".github/workflows/terraform-pr-check.yml",
            "environments/non-prod/ecs-fargate/terragrunt.hcl",
            "bootstrap/backend/non-prod/main.tf",
        ]
        assert _extract_module_names(files) == []


class TestBuildPrCheckWorkflow:
    def _plan_with_modules(self, *module_names: str) -> ChangePlan:
        files: list[str] = [".github/workflows/terraform-pr-check.yml"]
        for name in module_names:
            files += [f"modules/{name}/main.tf", f"modules/{name}/variables.tf"]
        files += ["bootstrap/backend/non-prod/main.tf"]
        return ChangePlan(
            stack_name=module_names[-1] if module_names else "test",
            environments=["non-prod"],
            files_to_generate=files,
            backend_resources={},
            summary=[],
        )

    def test_references_correct_module_working_directories(self):
        plan = self._plan_with_modules("foundation", "ecs-fargate")
        content = _build_pr_check_workflow(plan)

        assert "working-directory: modules/foundation" in content
        assert "working-directory: modules/ecs-fargate" in content
        # Must not reference any name not in the plan
        assert "ecs-fargate-stack" not in content

    def test_bootstrap_step_uses_correct_env(self):
        plan = self._plan_with_modules("ecs-fargate")
        content = _build_pr_check_workflow(plan)

        assert "working-directory: bootstrap/backend/non-prod" in content

    def test_single_validate_job(self):
        plan = self._plan_with_modules("ecs-fargate")
        content = _build_pr_check_workflow(plan)

        assert "jobs:" in content
        assert "  validate:" in content
        # Should not have multiple top-level jobs
        import yaml

        parsed = yaml.safe_load(content)
        assert list(parsed["jobs"].keys()) == ["validate"]

    def test_no_aws_credentials_in_pr_check(self):
        plan = self._plan_with_modules("ecs-fargate")
        content = _build_pr_check_workflow(plan)

        assert "AWS_ROLE_ARN" not in content
        assert "id-token" not in content


class TestBuildApplyWorkflow:
    def _plan_with_foundation_and_stack(self, stack: str) -> ChangePlan:
        return ChangePlan(
            stack_name=stack,
            environments=["non-prod"],
            files_to_generate=[
                ".github/workflows/terraform-apply.yml",
                "bootstrap/backend/non-prod/main.tf",
                "environments/non-prod/foundation/terragrunt.hcl",
                "environments/non-prod/ecs-fargate/terragrunt.hcl",
                "modules/foundation/main.tf",
                f"modules/{stack}/main.tf",
            ],
            backend_resources={},
            summary=[],
        )

    def test_apply_workflow_references_correct_stack_directory(self):
        plan = self._plan_with_foundation_and_stack("ecs-fargate")
        content = _build_apply_workflow(plan)

        assert "working-directory: environments/non-prod/${{ matrix.stack }}" in content
        # The matrix is driven by the detect job; the stack list flows through its env.
        assert "stack: ${{ fromJson(needs.detect.outputs.stacks) }}" in content
        assert 'WORKLOAD_STACKS: "ecs-fargate"' in content
        assert "ecs-fargate-stack" not in content

    def test_apply_workflow_has_foundation_job_when_foundation_in_plan(self):
        plan = self._plan_with_foundation_and_stack("ecs-fargate")
        content = _build_apply_workflow(plan)

        assert "apply-foundation:" in content
        assert "working-directory: environments/non-prod/foundation" in content

    def test_apply_workflow_plans_before_apply(self):
        plan = self._plan_with_foundation_and_stack("ecs-fargate")
        content = _build_apply_workflow(plan)

        assert "terraform plan -out=tfplan" in content
        assert "terraform apply -auto-approve tfplan" in content
        assert "- name: Plan foundation" in content
        assert "terragrunt plan --non-interactive -lock-timeout=20m -out=tfplan" in content
        assert "terragrunt apply --non-interactive tfplan" in content
        assert "terragrunt apply --non-interactive --auto-approve" not in content
        assert content.index("- name: Plan foundation") < content.index("- name: Apply foundation")

    def test_apply_workflow_uses_matrix_for_workloads(self):
        plan = ChangePlan(
            stack_name="api",
            environments=["non-prod"],
            files_to_generate=[
                ".github/workflows/terraform-apply.yml",
                "bootstrap/backend/non-prod/main.tf",
                "environments/non-prod/foundation/terragrunt.hcl",
                "environments/non-prod/api/terragrunt.hcl",
                "environments/non-prod/batch/terragrunt.hcl",
                "modules/foundation/main.tf",
                "modules/api/main.tf",
                "modules/batch/main.tf",
            ],
            backend_resources={},
            summary=[],
        )
        content = _build_apply_workflow(plan)

        assert "apply-workloads:" in content
        assert "strategy:" in content
        # Workloads run as a single matrix job scoped to the stacks the detect job found.
        assert "stack: ${{ fromJson(needs.detect.outputs.stacks) }}" in content
        assert 'WORKLOAD_STACKS: "api batch"' in content
        assert "working-directory: environments/non-prod/${{ matrix.stack }}" in content
        assert "apply-api:" not in content
        assert "apply-batch:" not in content

    def test_apply_workflow_uses_oidc_not_static_keys(self):
        plan = self._plan_with_foundation_and_stack("ecs-fargate")
        content = _build_apply_workflow(plan)

        assert "role-to-assume: ${{ secrets.AWS_ROLE_ARN_NON_PROD }}" in content
        assert "AWS_ACCESS_KEY_ID" not in content
        assert "AWS_SECRET_ACCESS_KEY" not in content
        # Account ID must be masked in apply logs (the action does not mask by default).
        assert content.count("mask-aws-account-id: true") == content.count(
            "uses: aws-actions/configure-aws-credentials@v4"
        )
        assert "mask-aws-account-id: true" in content

    def test_apply_workflow_has_change_detect_job(self):
        plan = self._plan_with_foundation_and_stack("ecs-fargate")
        content = _build_apply_workflow(plan)

        assert "  detect:" in content
        # Greenfield (no before-SHA) applies everything; otherwise diff the push range.
        assert "git ls-files environments modules bootstrap" in content
        assert 'git diff --name-only "$BEFORE" "$SHA"' in content
        # The workload matrix is driven by the components the detect job found changed.
        assert "stack: ${{ fromJson(needs.detect.outputs.stacks) }}" in content

    def test_apply_jobs_are_scoped_to_changed_components(self):
        plan = self._plan_with_foundation_and_stack("ecs-fargate")
        content = _build_apply_workflow(plan)

        assert "needs.detect.outputs.bootstrap == 'true'" in content
        assert "needs.detect.outputs.foundation == 'true'" in content
        assert "needs.detect.outputs.stacks != '[]'" in content
        # Skipped upstream jobs must not cancel independent downstream applies.
        assert "always()" in content

    def test_apply_workflow_has_single_approval_gate(self):
        plan = self._plan_with_foundation_and_stack("ecs-fargate")
        content = _build_apply_workflow(plan)

        assert "  gate:" in content
        assert "    environment: non-prod" in content
        # Exactly one environment gate covers the whole run — not one per apply job.
        assert content.count("environment:") == 1
        # The gate only prompts when something will actually apply.
        assert "needs.detect.outputs.any == 'true'" in content
        # Every apply job waits on the gate before any AWS mutation.
        assert "needs.gate.result == 'success'" in content

    def test_workflow_overrides_model_generated_content(self):
        """generate_files must replace model workflow content with deterministic version."""
        files = {
            ".github/workflows/terraform-pr-check.yml": (
                # Model hallucinated 'ecs-fargate' instead of 'ecs-fargate-stack'
                "jobs:\n  validate:\n    steps:\n      - working-directory: modules/ecs-fargate\n"
            ),
            "modules/ecs-fargate-stack/main.tf": (
                'resource "aws_ecs_cluster" "this" { name = var.cluster_name }\n'
            ),
            "modules/ecs-fargate-stack/variables.tf": (
                'variable "cluster_name" { type = string }\n'
            ),
        }
        plan = ChangePlan(
            stack_name="ecs-fargate-stack",
            environments=["non-prod"],
            files_to_generate=list(files.keys()) + ["bootstrap/backend/non-prod/main.tf"],
            backend_resources={},
            summary=[],
        )
        # Add the bootstrap file with minimal content
        files["bootstrap/backend/non-prod/main.tf"] = 'resource "aws_s3_bucket" "b" {}\n'

        runtime = FakeBedrockRuntime(files)
        generator = BedrockTerraformGenerator(
            model_id="anthropic.test-model",
            bedrock_runtime=runtime,
        )

        result = generator.generate_files(
            intent=_intent(),
            change_plan=plan,
            repo_patterns=RepoPatterns(),
            ruleset=None,
            target_repo="time4116/iac-smith-demo-infra",
        )

        # The deterministic override uses actual module names from files_to_generate
        wf = result[".github/workflows/terraform-pr-check.yml"]
        assert "working-directory: modules/ecs-fargate-stack" in wf
        # Model's wrong name must not appear
        assert "working-directory: modules/ecs-fargate\n" not in wf


class TestNormalizeChildTerragrunt:
    _STACK = "environments/non-prod/ecs-fargate/terragrunt.hcl"

    def _root(self, path: str = "environments/non-prod/root.hcl") -> dict[str, str]:
        return {path: 'locals {\n  environment = "non-prod"\n  aws_region  = "us-east-1"\n}\n'}

    def test_renders_deterministic_include_and_locals(self):
        from iac_smith.nodes.static_review import _find_terragrunt_orphaned_locals

        files = {
            **self._root(),
            self._STACK: (
                'include "root" { path = find_in_parent_folders("root.hcl") }\n'
                'terraform { source = "../../../modules/ecs-fargate" }\n'
                "inputs = {\n  environment = local.environment\n"
                "  aws_region  = local.aws_region\n}\n"
            ),
        }
        _normalize_child_terragrunt(files)
        child = files[self._STACK]
        assert 'include "root" {' in child
        assert 'path = find_in_parent_folders("root.hcl")' in child
        assert 'environment = "non-prod"' in child
        assert 'aws_region = "us-east-1"' in child
        # The model's terraform source and inputs are preserved.
        assert 'source = "../../../modules/ecs-fargate"' in child
        assert "environment = local.environment" in child
        assert _find_terragrunt_orphaned_locals(files) == []

    def test_legacy_terragrunt_hcl_root_name(self):
        files = {
            **self._root("environments/non-prod/terragrunt.hcl"),
            self._STACK: (
                'include "root" { path = find_in_parent_folders() }\n'
                "inputs = { environment = local.environment }\n"
            ),
        }
        _normalize_child_terragrunt(files)
        assert 'environment = "non-prod"' in files[self._STACK]

    def test_replaces_model_written_locals_without_duplication(self):
        # A partial/incorrect locals block the model wrote is replaced wholesale.
        files = {
            **self._root(),
            self._STACK: (
                'include "root" { path = find_in_parent_folders("root.hcl") }\n'
                'locals {\n  environment = "WRONG"\n}\n'
                "inputs = { environment = local.environment }\n"
            ),
        }
        _normalize_child_terragrunt(files)
        child = files[self._STACK]
        assert "WRONG" not in child
        assert child.count("locals {") == 1
        assert child.count('environment = "non-prod"') == 1
        assert 'aws_region = "us-east-1"' in child

    def test_inserts_include_when_model_omitted_it(self):
        # The old additive injector skipped files with no include block, leaving a
        # broken stack; the normalizer always emits a correct include.
        files = {
            **self._root(),
            self._STACK: "inputs = { environment = local.environment }\n",
        }
        _normalize_child_terragrunt(files)
        assert 'include "root" {' in files[self._STACK]
        assert 'environment = "non-prod"' in files[self._STACK]

    def test_does_not_mangle_canonical_locals_comment(self):
        files = {
            **self._root(),
            self._STACK: (
                'include "root" { path = find_in_parent_folders("root.hcl") }\n'
                "# Redeclare values you need from the parent in this locals {} block.\n"
                "inputs = { environment = local.environment }\n"
            ),
        }
        _normalize_child_terragrunt(files)
        child = files[self._STACK]
        assert '"non-prod"} block' not in child
        assert 'environment = "non-prod"' in child

    def test_root_config_is_untouched(self):
        files = self._root()
        before = dict(files)
        _normalize_child_terragrunt(files)
        assert files == before

    def test_declares_stack_name_and_environment_derived_from_path(self):
        # A child that references local.stack_name (or local.environment) must get
        # them declared from its own path — the root does not expose stack_name, so
        # the rebuilt envelope would otherwise fail with "Unsupported attribute".
        files = {
            **self._root(),
            self._STACK: (
                'include "root" { path = find_in_parent_folders("root.hcl") }\n'
                'terraform { source = "../../../modules/ecs-fargate" }\n'
                "inputs = {\n  name        = local.stack_name\n"
                "  environment = local.environment\n}\n"
            ),
        }
        _normalize_child_terragrunt(files)
        child = files[self._STACK]
        assert 'stack_name = "ecs-fargate"' in child
        assert 'environment = "non-prod"' in child
        assert child.count("locals {") == 1

    def test_root_environment_wins_over_derived(self):
        # The root's own environment value takes precedence over the path-derived one.
        files = {
            "environments/prod/root.hcl": (
                'locals {\n  environment = "prod"\n  aws_region  = "us-east-1"\n}\n'
            ),
            "environments/prod/ecs-fargate/terragrunt.hcl": (
                "inputs = { environment = local.environment }\n"
            ),
        }
        _normalize_child_terragrunt(files)
        child = files["environments/prod/ecs-fargate/terragrunt.hcl"]
        assert child.count('environment = "prod"') == 1


class TestDedupModuleDeclarations:
    def test_removes_variable_duplicated_in_main_tf(self):
        files = {
            "modules/db/variables.tf": (
                'variable "environment" {\n  type = string\n}\n'
                'variable "instance_class" {\n  type = string\n}\n'
            ),
            "modules/db/main.tf": (
                'variable "environment" {\n  type = string\n}\n'
                'resource "aws_db_instance" "this" {\n  instance_class = var.instance_class\n}\n'
            ),
        }
        _dedup_module_declarations(files)
        main_tf = files["modules/db/main.tf"]
        assert 'variable "environment"' not in main_tf
        # The real resource is preserved.
        assert 'resource "aws_db_instance" "this"' in main_tf
        # variables.tf is authoritative and untouched.
        assert files["modules/db/variables.tf"].count('variable "environment"') == 1

    def test_keeps_variable_declared_only_in_main_tf(self):
        files = {
            "modules/db/variables.tf": 'variable "environment" {\n  type = string\n}\n',
            "modules/db/main.tf": 'variable "only_here" {\n  type = string\n}\n',
        }
        _dedup_module_declarations(files)
        assert 'variable "only_here"' in files["modules/db/main.tf"]

    def test_dedups_outputs_too(self):
        endpoint_output = 'output "endpoint" {\n  value = aws_db_instance.this.endpoint\n}\n'
        files = {
            "modules/db/outputs.tf": endpoint_output,
            "modules/db/main.tf": endpoint_output + 'resource "aws_db_instance" "this" {}\n',
        }
        _dedup_module_declarations(files)
        assert 'output "endpoint"' not in files["modules/db/main.tf"]
        assert 'resource "aws_db_instance" "this"' in files["modules/db/main.tf"]

    def test_noop_without_dedicated_file(self):
        files = {
            "modules/db/main.tf": 'variable "environment" {\n  type = string\n}\n',
        }
        before = dict(files)
        _dedup_module_declarations(files)
        assert files == before

    def test_handles_nested_braces_in_variable_block(self):
        files = {
            "modules/db/variables.tf": 'variable "tags" {\n  type = map(string)\n}\n',
            "modules/db/main.tf": (
                'variable "tags" {\n  type = object({\n    Name = string\n  })\n'
                "  default = {}\n}\n"
                'resource "aws_db_instance" "this" {}\n'
            ),
        }
        _dedup_module_declarations(files)
        main_tf = files["modules/db/main.tf"]
        assert 'variable "tags"' not in main_tf
        assert 'resource "aws_db_instance" "this"' in main_tf


class TestWireFoundationDependency:
    _STACK = "environments/non-prod/data-platform/terragrunt.hcl"
    _FOUNDATION_OUTPUTS = (
        'output "vpc_id" {\n  value = module.vpc.vpc_id\n}\n'
        'output "private_subnet_ids" {\n  value = module.vpc.private_subnets\n}\n'
        'output "public_subnet_ids" {\n  value = module.vpc.public_subnets\n}\n'
        'output "vpc_cidr" {\n  value = module.vpc.vpc_cidr_block\n}\n'
    )
    _WORKLOAD_VARS = (
        'variable "environment" {\n  type = string\n}\n'
        'variable "vpc_id" {\n  type = string\n}\n'
        'variable "private_subnet_ids" {\n  type = list(string)\n}\n'
        'variable "instance_class" {\n  type = string\n  default = "db.t3.medium"\n}\n'
    )

    def _files(self, stack_body: str, *, with_foundation: bool = True) -> dict[str, str]:
        files = {
            self._STACK: stack_body,
            "modules/data-platform/variables.tf": self._WORKLOAD_VARS,
        }
        if with_foundation:
            files["environments/non-prod/foundation/terragrunt.hcl"] = 'include "root" {}\n'
            files["modules/foundation/outputs.tf"] = self._FOUNDATION_OUTPUTS
        return files

    def test_wires_intersection_inputs_to_dependency(self):
        files = self._files(
            'terraform {\n  source = "../../../modules/data-platform"\n}\n'
            "inputs = {\n"
            "  environment        = local.environment\n"
            '  vpc_id             = ""\n'
            "  private_subnet_ids = []\n"
            '  instance_class     = "db.t3.medium"\n'
            "}\n"
        )
        _wire_foundation_dependency(files)
        child = files[self._STACK]
        assert 'dependency "foundation" {' in child
        assert 'config_path = "../foundation"' in child
        # Exact alignment is left to `terragrunt hcl format`; assert the wiring.
        assert "vpc_id = dependency.foundation.outputs.vpc_id" in child
        assert "private_subnet_ids = dependency.foundation.outputs.private_subnet_ids" in child
        # Non-networking model inputs are preserved untouched.
        assert 'instance_class     = "db.t3.medium"' in child
        # The placeholder empties are gone.
        assert 'vpc_id             = ""' not in child
        assert "private_subnet_ids = []" not in child

    def test_mock_outputs_typed_from_consuming_variable(self):
        files = self._files(
            'terraform {\n  source = "../../../modules/data-platform"\n}\n'
            "inputs = {\n  vpc_id = local.x\n  private_subnet_ids = local.y\n}\n"
        )
        _wire_foundation_dependency(files)
        child = files[self._STACK]
        # string -> string mock, list(string) -> list mock.
        assert 'vpc_id = "mock"' in child
        assert 'private_subnet_ids = ["mock-0", "mock-1"]' in child
        assert 'mock_outputs_allowed_terraform_commands = ["validate", "plan"]' in child

    def test_no_op_when_workload_declares_no_foundation_inputs(self):
        # A workload that needs no networking gets nothing forced on it.
        files = {
            self._STACK: (
                'terraform {\n  source = "../../../modules/data-platform"\n}\n'
                "inputs = {\n  environment = local.environment\n}\n"
            ),
            "modules/data-platform/variables.tf": 'variable "environment" {\n  type = string\n}\n',
            "environments/non-prod/foundation/terragrunt.hcl": 'include "root" {}\n',
            "modules/foundation/outputs.tf": self._FOUNDATION_OUTPUTS,
        }
        before = dict(files)
        _wire_foundation_dependency(files)
        assert files == before

    def test_no_op_without_foundation_in_plan(self):
        files = self._files("inputs = {\n  vpc_id = local.x\n}\n", with_foundation=False)
        before = dict(files)
        _wire_foundation_dependency(files)
        assert files == before

    def test_replaces_model_authored_dependency_block(self):
        files = self._files(
            'dependency "foundation" {\n'
            '  config_path = "../wrong-path"\n'
            "  mock_outputs = {\n    vpc_id = 123\n  }\n"
            "}\n"
            'terraform {\n  source = "../../../modules/data-platform"\n}\n'
            "inputs = {\n  vpc_id = local.x\n  private_subnet_ids = local.y\n}\n"
        )
        _wire_foundation_dependency(files)
        child = files[self._STACK]
        assert "../wrong-path" not in child
        assert child.count('dependency "foundation" {') == 1
        assert 'config_path = "../foundation"' in child

    def test_idempotent(self):
        files = self._files(
            'terraform {\n  source = "../../../modules/data-platform"\n}\n'
            'inputs = {\n  vpc_id = ""\n  private_subnet_ids = []\n}\n'
        )
        _wire_foundation_dependency(files)
        once = files[self._STACK]
        _wire_foundation_dependency(files)
        assert files[self._STACK] == once

    def test_foundation_stack_itself_untouched(self):
        files = {
            "environments/non-prod/foundation/terragrunt.hcl": (
                'terraform {\n  source = "../../../modules/foundation"\n}\n'
                "inputs = {\n  environment = local.environment\n}\n"
            ),
            "modules/foundation/outputs.tf": self._FOUNDATION_OUTPUTS,
            "modules/foundation/variables.tf": 'variable "vpc_id" {\n  type = string\n}\n',
        }
        before = dict(files)
        _wire_foundation_dependency(files)
        assert files == before


class TestStripOrphanFoundationDependency:
    _STACK = "environments/non-prod/rds-aurora/terragrunt.hcl"
    _WITH_DEP = (
        'terraform {\n  source = "../../../modules/rds-aurora"\n}\n'
        'dependency "foundation" {\n  config_path = "../foundation"\n'
        '  mock_outputs = {\n    vpc_id = "vpc-0"\n  }\n}\n'
        "inputs = {\n"
        "  environment        = local.environment\n"
        "  vpc_id             = dependency.foundation.outputs.vpc_id\n"
        "  private_subnet_ids = dependency.foundation.outputs.private_subnet_ids\n"
        "}\n"
    )

    def test_strips_dependency_and_output_refs_when_no_foundation(self):
        files = {self._STACK: self._WITH_DEP}
        _strip_orphan_foundation_dependency(files)
        out = files[self._STACK]
        assert 'dependency "foundation"' not in out
        assert "dependency.foundation.outputs" not in out
        # Unrelated inputs and the terraform source survive.
        assert "environment        = local.environment" in out
        assert 'source = "../../../modules/rds-aurora"' in out

    def test_no_op_when_foundation_stack_present(self):
        files = {
            self._STACK: self._WITH_DEP,
            "environments/non-prod/foundation/terragrunt.hcl": (
                'terraform {\n  source = "../../../modules/foundation"\n}\n'
            ),
            "modules/foundation/outputs.tf": 'output "vpc_id" {\n  value = module.vpc.vpc_id\n}\n',
        }
        before = dict(files)
        _strip_orphan_foundation_dependency(files)
        assert files == before

    def test_no_op_when_workload_has_no_foundation_reference(self):
        files = {
            self._STACK: (
                'terraform {\n  source = "../../../modules/rds-aurora"\n}\n'
                "inputs = {\n  environment = local.environment\n}\n"
            )
        }
        before = dict(files)
        _strip_orphan_foundation_dependency(files)
        assert files == before
