import json
import threading
import time

import botocore.exceptions
import pytest

from iac_smith.dynamic_terraform import (
    BedrockTerraformGenerator,
    _path_needs_repair,
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
    assert any("static review failed" in message for message in messages)
    assert (
        "IaC Smith: repairing file 1/4: environments/non-prod/ecs-fargate/terragrunt.hcl"
        in messages
    )
    assert (
        "IaC Smith: static review passed for "
        "environments/non-prod/ecs-fargate/terragrunt.hcl after repair." in messages
    )
    assert messages[-1] == "IaC Smith: generated 4 file(s)."


def test_bedrock_terraform_generator_stops_after_unrepaired_static_review_failure():
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

    with pytest.raises(ValueError, match="failed static review"):
        generator.generate_files(
            intent=_intent(),
            change_plan=_plan(),
            repo_patterns=RepoPatterns(),
            ruleset=_ruleset(),
            target_repo="time4116/iac-smith-demo-infra",
        )

    assert len(runtime.calls) == len(files) + 1


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
    assert kwargs["config"].retries["max_attempts"] >= 3


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

    def test_returns_true_via_directory_match_for_runtime_validate_error(self):
        # Runtime errors name the module directory, not individual .tf files.
        # All files in that directory should be considered for repair.
        errors = [
            "terraform validate modules/ecs-fargate failed in `modules/ecs-fargate`:\n"
            "│ Error: Reference to undeclared resource\n"
            "│   on main.tf line 42\n"
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


def test_variables_tf_not_repaired_when_only_main_tf_has_duplicate_declarations():
    """Regression: when main.tf duplicates variable decls from variables.tf, repair
    must only regenerate main.tf — not variables.tf.  Regenerating variables.tf can
    drop declarations (e.g. var.project, var.region) that main.tf still references,
    causing a second static review failure for undeclared variables.
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
        "modules/foundation/main.tf": main_tf_bad,
        "modules/foundation/variables.tf": variables_tf,
        "modules/foundation/outputs.tf": 'output "vpc_id" { value = aws_vpc.this.id }\n',
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
                if (in_repair and path == "modules/foundation/main.tf")
                else files[path]
            )
            return {
                "body": FakeBody(
                    json.dumps(
                        {"path": path, "content": content, "assumptions": [], "warnings": []}
                    ).encode()
                )
            }

    runtime = TrackingBedrockRuntime()
    plan = ChangePlan(
        stack_name="foundation",
        environments=["non-prod"],
        files_to_generate=list(files),
        backend_resources={},
        summary=["foundation module"],
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

    assert result["modules/foundation/main.tf"] == main_tf_fixed
    assert result["modules/foundation/variables.tf"] == variables_tf
    assert call_count_by_path.get("modules/foundation/main.tf", 0) == 2
    assert call_count_by_path.get("modules/foundation/variables.tf", 0) == 1
