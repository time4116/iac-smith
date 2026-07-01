from __future__ import annotations

import re
from pathlib import Path

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.infrastructure_spec import (
    BackendSpec,
    ComponentSpec,
    DependencySpec,
    InfrastructureSpec,
    OutputSpec,
    ProviderResourcesSpec,
    ValueExpression,
)
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.validation import ValidationResult, ValidationStatus

_OUTPUT_RE = re.compile(r'output\s+"([^"]+)"\s*{')


def _repo_has_foundation(repo_patterns: RepoPatterns | None) -> bool:
    if not repo_patterns:
        return False
    return any(
        path == "modules/foundation"
        or path.startswith("modules/foundation/")
        or path.endswith("/foundation")
        for path in repo_patterns.existing_stack_paths
    )


def _planned_module_paths(change_plan: ChangePlan) -> set[str]:
    return {path for path in change_plan.files_to_generate if path.startswith("modules/")}


def discover_foundation_outputs(repo_path: Path | None) -> list[str]:
    """Discover foundation outputs from the target repo instead of assuming names."""

    if repo_path is None:
        return []
    candidates = [
        repo_path / "modules/foundation/outputs.tf",
        repo_path / "modules/vpc-foundation/outputs.tf",
    ]
    for path in candidates:
        if path.exists():
            outputs = _OUTPUT_RE.findall(path.read_text(encoding="utf-8"))
            if outputs:
                return outputs
    return []


def _fallback_foundation_outputs() -> list[str]:
    # Last-resort compatibility only when the repo scanner says a foundation exists
    # but the source checkout is unavailable. Real runs pass repo_path and discover
    # outputs from the actual module.
    return ["vpc_id", "private_subnet_ids"]


def build_spec_from_intent(
    *,
    intent: InfrastructureIntent,
    change_plan: ChangePlan,
    repo_patterns: RepoPatterns | None,
    target_repo: str,
    repo_path: Path | None = None,
) -> InfrastructureSpec:
    backends = [
        BackendSpec(
            environment=env,
            bucket=backend.bucket,
            lock_table=backend.lock_table,
            region=intent.region,
        )
        for env, backend in sorted(change_plan.backend_resources.items())
    ]
    component_inputs = {
        "environment": ValueExpression(expression="local.environment"),
        "aws_region": ValueExpression(expression="local.aws_region"),
    }
    dependencies: list[DependencySpec] = []
    if _repo_has_foundation(repo_patterns):
        outputs = discover_foundation_outputs(repo_path) or _fallback_foundation_outputs()
        dependencies.append(
            DependencySpec(
                consumer=change_plan.stack_name,
                producer="foundation",
                outputs=outputs,
            )
        )
        component_inputs.update(
            {
                output: ValueExpression(expression=f"dependency.foundation.outputs.{output}")
                for output in outputs
            }
        )

    resources = []
    components = [
        ComponentSpec(
            name=change_plan.stack_name,
            kind="workload",
            implementation=ProviderResourcesSpec(resources=resources),
            inputs=component_inputs,
            outputs=[
                OutputSpec(
                    name="spec_summary",
                    description="Human-readable summary of the rendered infrastructure spec.",
                    value='"Rendered deterministic IaC Smith structure for ${var.environment}"',
                )
            ],
        )
    ]
    warnings = list(intent.warnings)
    if _planned_module_paths(change_plan) and not resources:
        warnings.append(
            "Spec renderer emitted deterministic structure only; no provider resources "
            "were selected because registry/module or provider-schema composition is "
            "not implemented in this mode."
        )

    return InfrastructureSpec(
        raw_request=intent.raw_request,
        target_repo=target_repo,
        stack_name=change_plan.stack_name,
        environments=change_plan.environments,
        region=intent.region,
        backends=backends,
        components=components,
        dependencies=dependencies,
        files_to_generate=change_plan.files_to_generate,
        assumptions=list(intent.assumptions),
        warnings=warnings,
    )


def validate_spec(spec: InfrastructureSpec) -> ValidationResult:
    errors: list[str] = []
    if not spec.components:
        errors.append("InfrastructureSpec must include at least one component.")
    for component in spec.components:
        for dependency in spec.dependencies:
            if dependency.consumer == component.name:
                for output in dependency.outputs:
                    if output not in component.inputs:
                        errors.append(
                            f"Dependency output `{output}` is not wired into "
                            f"`{component.name}` inputs."
                        )
    status = ValidationStatus.FAILED if errors else ValidationStatus.PASSED
    checks = [] if errors else ["InfrastructureSpec cross-file contracts are internally valid."]
    return ValidationResult(status=status, errors=errors, checks=checks)


def render_spec(spec: InfrastructureSpec) -> dict[str, str]:
    validation = validate_spec(spec)
    if validation.status == ValidationStatus.FAILED:
        raise ValueError("; ".join(validation.errors))
    return {path: _render_path(spec, path) for path in spec.files_to_generate}


def _render_path(spec: InfrastructureSpec, path: str) -> str:
    if path == "README.md":
        return _render_root_readme(spec)
    if path == ".github/workflows/terraform-pr-check.yml":
        return _render_pr_check_workflow(spec)
    if path == ".github/workflows/terraform-apply.yml":
        return _render_apply_workflow(spec)
    if path.startswith("bootstrap/backend/"):
        return _render_backend_file(spec, path)
    if path.endswith("/root.hcl") and path.startswith("environments/"):
        return _render_environment_root(spec, path)
    if path.endswith("/terragrunt.hcl") and path.startswith("environments/"):
        return _render_stack_terragrunt(spec, path)
    if path.endswith("/README.md") and path.startswith("environments/"):
        return _render_stack_readme(spec, path)
    if path.startswith("modules/"):
        return _render_module_file(spec, path)
    return "# Generated by IaC Smith spec renderer.\n"


def _component(spec: InfrastructureSpec) -> ComponentSpec:
    return spec.components[0]


def _env_from_path(path: str) -> str:
    return path.split("/")[1]


def _backend_for(spec: InfrastructureSpec, env: str) -> BackendSpec:
    for backend in spec.backends:
        if backend.environment == env:
            return backend
    raise KeyError(f"No backend spec for environment {env}")


def _render_root_readme(spec: InfrastructureSpec) -> str:
    warning_lines = "\n".join(f"* {warning}" for warning in spec.warnings) or "* None"
    return (
        f"# {spec.target_repo} infrastructure\n\n"
        "Generated by IaC Smith's typed spec renderer. The renderer owns repo "
        "layout, Terragrunt wiring, module contracts, backend bootstrap, and workflows.\n\n"
        f"## Stack\n\n* `{spec.stack_name}`\n\n"
        f"## Environments\n\n{''.join(f'* `{env}`\n' for env in spec.environments)}\n"
        "## Warnings\n\n"
        f"{warning_lines}\n"
    )


def _render_pr_check_workflow(spec: InfrastructureSpec) -> str:
    module_dirs = sorted(
        {
            "/".join(path.split("/")[:2])
            for path in spec.files_to_generate
            if path.startswith("modules/")
        }
    )
    module_steps = []
    for module_dir in module_dirs:
        module_steps.extend(
            [
                f"      - name: Terraform init and validate — {module_dir}",
                f"        working-directory: {module_dir}",
                "        run: |",
                "          terraform init -backend=false -input=false",
                "          terraform validate",
            ]
        )
    if not module_steps:
        module_steps = ["      - run: echo 'No new module directories in this change plan.'"]
    return "\n".join(
        [
            "name: Terraform PR Check",
            "",
            "on:",
            "  pull_request:",
            "    paths:",
            "      - 'environments/**'",
            "      - 'modules/**'",
            "      - 'bootstrap/**'",
            "",
            "permissions:",
            "  contents: read",
            "  pull-requests: read",
            "",
            "jobs:",
            "  validate:",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - uses: hashicorp/setup-terraform@v3",
            *module_steps,
            "",
        ]
    )


def _render_apply_workflow(spec: InfrastructureSpec) -> str:
    env = spec.environments[0]
    return "\n".join(
        [
            "name: Terraform Apply",
            "",
            "on:",
            "  push:",
            "    branches: [main]",
            "    paths:",
            "      - 'environments/**'",
            "      - 'modules/**'",
            "      - 'bootstrap/**'",
            "",
            "permissions:",
            "  contents: read",
            "  id-token: write",
            "",
            "jobs:",
            "  detect:",
            "    runs-on: ubuntu-latest",
            "    outputs:",
            "      stack_changed: ${{ steps.filter.outputs.stack_changed }}",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - id: filter",
            "        run: echo 'stack_changed=true' >> \"$GITHUB_OUTPUT\"",
            "  plan-summary:",
            "    needs: detect",
            "    if: needs.detect.outputs.stack_changed == 'true'",
            "    runs-on: ubuntu-latest",
            "    environment: production",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - run: echo 'Spec-rendered apply workflow placeholder. Review generated plan'",
            "      - run: echo 'before apply.'",
            f"      - run: echo 'Default environment: {env}'",
            "",
        ]
    )


def _render_backend_file(spec: InfrastructureSpec, path: str) -> str:
    env = path.split("/")[2]
    backend = _backend_for(spec, env)
    filename = path.rpartition("/")[2]
    if filename == "main.tf":
        return (
            'resource "aws_s3_bucket" "terraform_state" {\n'
            "  bucket = var.state_bucket_name\n}\n\n"
            'resource "aws_dynamodb_table" "terraform_locks" {\n'
            "  name         = var.state_lock_table_name\n"
            '  billing_mode = "PAY_PER_REQUEST"\n'
            '  hash_key     = "LockID"\n\n'
            '  attribute {\n    name = "LockID"\n    type = "S"\n  }\n}\n'
        )
    if filename == "variables.tf":
        return (
            'variable "state_bucket_name" {\n'
            f'  default = "{backend.bucket}"\n'
            "}\n\n"
            'variable "state_lock_table_name" {\n'
            f'  default = "{backend.lock_table}"\n'
            "}\n"
        )
    if filename == "outputs.tf":
        return (
            'output "state_bucket_name" {\n  value = aws_s3_bucket.terraform_state.bucket\n}\n\n'
            'output "state_lock_table_name" {\n'
            "  value = aws_dynamodb_table.terraform_locks.name\n}\n"
        )
    return f"# Backend bootstrap for `{env}`.\n"


def _render_environment_root(spec: InfrastructureSpec, path: str) -> str:
    env = _env_from_path(path)
    backend = _backend_for(spec, env)
    return (
        "locals {\n"
        f'  environment = "{env}"\n'
        f'  aws_region  = "{backend.region}"\n'
        "}\n\n"
        "remote_state {\n"
        '  backend = "s3"\n'
        "  config = {\n"
        f'    bucket         = "{backend.bucket}"\n'
        '    key            = "${path_relative_to_include()}/terraform.tfstate"\n'
        f'    region         = "{backend.region}"\n'
        "    encrypt        = true\n"
        f'    dynamodb_table = "{backend.lock_table}"\n'
        "  }\n"
        "  generate = {\n"
        '    path      = "backend.tf"\n'
        '    if_exists = "overwrite_terragrunt"\n'
        "  }\n"
        "}\n\n"
        'generate "provider" {\n'
        '  path      = "provider.tf"\n'
        '  if_exists = "overwrite_terragrunt"\n'
        "  contents  = <<EOF\n"
        'provider "aws" {\n'
        '  region = "${local.aws_region}"\n'
        "}\n"
        "EOF\n"
        "}\n"
    )


def _render_stack_terragrunt(spec: InfrastructureSpec, path: str) -> str:
    env = _env_from_path(path)
    component = _component(spec)
    dependency_blocks = []
    input_lines = [
        "  environment = local.environment",
        "  aws_region  = local.aws_region",
    ]
    for name, value in component.inputs.items():
        if name in {"environment", "aws_region"}:
            continue
        if value.expression.startswith(("dependency.", "var.")):
            continue
        input_lines.append(f"  {name} = {value.expression}")
    for dependency in spec.dependencies:
        if dependency.consumer != component.name:
            continue
        mock_outputs = "\n".join(
            f"    {name} = {_mock_output_value(name)}" for name in dependency.outputs
        )
        dependency_blocks.append(
            f'dependency "{dependency.producer}" {{\n'
            f'  config_path = "../{dependency.producer}"\n\n'
            f"  mock_outputs = {{\n{mock_outputs}\n  }}\n"
            '  mock_outputs_allowed_terraform_commands = ["validate", "plan"]\n'
            "}\n"
        )
        for output in dependency.outputs:
            input_lines.append(f"  {output} = dependency.{dependency.producer}.outputs.{output}")
    dependencies = "\n".join(dependency_blocks)
    if dependencies:
        dependencies += "\n"
    return (
        'include "root" {\n  path = find_in_parent_folders("root.hcl")\n}\n\n'
        "locals {\n"
        f'  environment = "{env}"\n'
        f'  aws_region  = "{spec.region}"\n'
        "}\n\n"
        "terraform {\n"
        f'  source = "../../../modules/{component.name}"\n'
        "}\n\n"
        f"{dependencies}"
        "inputs = {\n" + "\n".join(input_lines) + "\n}\n"
    )


def _mock_output_value(name: str) -> str:
    if name.endswith("_ids"):
        return '["mock-id"]'
    if name.endswith("_id"):
        return '"mock-id"'
    return '"mock-value"'


def _render_stack_readme(spec: InfrastructureSpec, path: str) -> str:
    return f"# {spec.stack_name}\n\nGenerated Terragrunt stack for `{spec.stack_name}`.\n"


def _render_module_file(spec: InfrastructureSpec, path: str) -> str:
    filename = path.rpartition("/")[2]
    component = _component(spec)
    if filename == "main.tf":
        return _render_resources(component)
    if filename == "variables.tf":
        return _render_variables(component)
    if filename == "outputs.tf":
        return _render_outputs(component)
    if filename == "versions.tf":
        return (
            "terraform {\n"
            '  required_version = ">= 1.5"\n'
            "  required_providers {\n"
            "    aws = {\n"
            '      source  = "hashicorp/aws"\n'
            '      version = "~> 5.0"\n'
            "    }\n"
            "  }\n"
            "}\n"
        )
    return (
        f"# {component.name}\n\n"
        "This module is rendered from a typed InfrastructureSpec.\n\n"
        "<!-- BEGIN_TF_DOCS -->\n<!-- END_TF_DOCS -->\n"
    )


def _render_resources(component: ComponentSpec) -> str:
    implementation = component.implementation
    if implementation.kind != "provider_resources" or not implementation.resources:
        return (
            "# Deterministic skeleton generated from InfrastructureSpec.\n"
            "# No provider resources were selected for this component.\n"
        )
    blocks = []
    for resource in implementation.resources:
        lines = [f'resource "{resource.type}" "{resource.name}" {{']
        for key, value in resource.arguments.items():
            lines.append(f"  {key} = {value}")
        for block in resource.blocks:
            lines.append(f"  {block}")
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _render_variables(component: ComponentSpec) -> str:
    blocks = []
    for name in component.inputs:
        type_expr = "list(string)" if name.endswith("_ids") else "string"
        blocks.append(
            f'variable "{name}" {{\n'
            f'  description = "Spec-rendered input {name}."\n'
            f"  type        = {type_expr}\n"
            "}\n"
        )
    return "\n".join(blocks)


def _render_outputs(component: ComponentSpec) -> str:
    if not component.outputs:
        return ""
    return "\n".join(
        f'output "{output.name}" {{\n'
        f'  description = "{output.description}"\n'
        f"  value       = {output.value}\n"
        "}\n"
        for output in component.outputs
    )


class SpecRendererGenerator:
    """File-generator adapter used by graph.default_file_generator."""

    def generate_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset=None,
        target_repo: str,
        repo_path=None,
        blackboard=None,
    ) -> dict[str, str]:
        spec = build_spec_from_intent(
            intent=intent,
            change_plan=change_plan,
            repo_patterns=repo_patterns,
            target_repo=target_repo,
            repo_path=Path(repo_path) if repo_path else None,
        )
        return render_spec(spec)
