from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

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
from iac_smith.nodes.change_planner import FOUNDATION_ALIASES

if TYPE_CHECKING:
    from iac_smith.blackboard import RunBlackboard
    from iac_smith.spec_composer import ComposedComponent, SpecComposer

_OUTPUT_RE = re.compile(r'output\s+"([^"]+)"\s*{')

_STRUCTURE_ONLY_WARNING = (
    "Spec renderer emitted deterministic structure only; no provider resources "
    "were selected for this component."
)


class StructureOnlyBlocked(RuntimeError):
    """Planned workload modules would be placeholders and the opt-in is not set.

    Raised instead of degrading to a structure-only render: a PR whose modules
    contain no provider resources misleads reviewers, so the default is to fail
    closed (``IAC_SMITH_ALLOW_STRUCTURE_ONLY=1`` re-enables the old fallback).
    """


class RenderedFiles(dict[str, str]):
    """Generated file mapping with renderer metadata carried out of the spec layer."""

    def __init__(self, files: dict[str, str], *, structure_only: bool = False):
        super().__init__(files)
        self.structure_only = structure_only


def _planned_module_paths(change_plan: ChangePlan) -> set[str]:
    return _planned_module_paths_from_files(change_plan.files_to_generate)


def _planned_module_paths_from_files(files_to_generate: list[str]) -> set[str]:
    return {path for path in files_to_generate if path.startswith("modules/")}


def discover_stack_outputs(repo_path: Path | None, stack_name: str) -> list[str]:
    """Discover an existing stack's outputs from the target repo instead of assuming names."""

    if repo_path is None:
        return []
    candidates = [repo_path / f"modules/{stack_name}/outputs.tf"]
    if stack_name == "foundation":
        candidates.append(repo_path / "modules/vpc-foundation/outputs.tf")
    for path in candidates:
        if path.exists():
            outputs = _OUTPUT_RE.findall(path.read_text(encoding="utf-8"))
            if outputs:
                return outputs
    return []


def discover_foundation_outputs(repo_path: Path | None) -> list[str]:
    return discover_stack_outputs(repo_path, "foundation")


def _normalized_stack_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _repo_stack_names(repo_patterns: RepoPatterns | None) -> set[str]:
    if not repo_patterns:
        return set()
    return {
        _normalized_stack_name(path.rstrip("/").split("/")[-1])
        for path in repo_patterns.existing_stack_paths
        if path.strip("/")
    }


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
    warnings = list(intent.warnings)
    dependencies: list[DependencySpec] = []
    repo_stacks = _repo_stack_names(repo_patterns)

    def _resolve_producer(required: str) -> str | None:
        # An issue may say "foundation" while the repo's actual stack is named
        # `vpc-foundation` (or vice versa); the alias family resolves to the
        # stack that really exists so wiring uses real paths, never the label.
        normalized = _normalized_stack_name(required)
        if not normalized or normalized == change_plan.stack_name:
            return None
        candidates = [normalized]
        if normalized in FOUNDATION_ALIASES:
            candidates.extend(sorted(FOUNDATION_ALIASES - {normalized}))
        for candidate in candidates:
            if candidate in repo_stacks and candidate != change_plan.stack_name:
                return candidate
        return None

    producers: list[str] = []
    existing_foundation = next(
        (
            stack
            for stack in sorted(repo_stacks & FOUNDATION_ALIASES)
            if stack != change_plan.stack_name
        ),
        None,
    )
    if existing_foundation:
        producers.append(existing_foundation)
    # The issue may require consuming other stacks that already exist in the
    # target repo; wire every one that is really there. Missing ones were
    # already blocked by the prerequisite gate before generation.
    for name in intent.depends_on_existing:
        resolved = _resolve_producer(name)
        if resolved and resolved not in producers:
            producers.append(resolved)
    for producer in producers:
        outputs = discover_stack_outputs(repo_path, producer)
        if not outputs and producer in FOUNDATION_ALIASES:
            outputs = _fallback_foundation_outputs()
        if not outputs:
            warnings.append(
                f"Existing stack `{producer}` exposes no discoverable outputs; "
                "its dependency was not wired."
            )
            continue
        dependencies.append(
            DependencySpec(
                consumer=change_plan.stack_name,
                producer=producer,
                outputs=outputs,
            )
        )
        component_inputs.update(
            {
                output: ValueExpression(expression=f"dependency.{producer}.outputs.{output}")
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
    if _planned_module_paths(change_plan) and not resources:
        warnings.append(_STRUCTURE_ONLY_WARNING)

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


def is_structure_only_spec(spec: InfrastructureSpec) -> bool:
    """True when planned module components contain no selected implementation bodies."""

    if not _planned_module_paths_from_files(spec.files_to_generate):
        return False
    for component in spec.components:
        implementation = component.implementation
        if implementation.kind == "provider_resources" and implementation.resources:
            return False
        if implementation.kind == "registry_module":
            return False
    return True


def render_spec(spec: InfrastructureSpec) -> RenderedFiles:
    validation = validate_spec(spec)
    if validation.status == ValidationStatus.FAILED:
        raise ValueError("; ".join(validation.errors))
    files = {path: _render_path(spec, path) for path in spec.files_to_generate}
    return RenderedFiles(files, structure_only=is_structure_only_spec(spec))


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


_HCL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _escape_hcl_template(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")


def quote_hcl_template(value: str) -> str:
    """Quote a JSON string as an HCL string template.

    Terraform JSON configuration semantics: plain text becomes a quoted literal,
    while ``${...}`` interpolation is preserved as the expression channel — so a
    natural model value like ``"IaC Smith"`` renders as a valid literal and
    ``"${var.environment}"`` stays an expression (a template that is exactly one
    interpolation yields the referenced value's native type).
    """
    return f'"{_escape_hcl_template(value)}"'


def render_hcl_value(value, indent: int = 1) -> str:
    """Render a native JSON argument value to HCL.

    Numbers and booleans render as literals; strings follow Terraform JSON
    configuration semantics (see ``quote_hcl_template``); lists and objects
    render recursively, so the model can express e.g. ``tags`` as a plain JSON
    object instead of stringified HCL.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return quote_hcl_template(value)
    if value is None:
        return "null"
    pad = "  " * indent
    if isinstance(value, list):
        if not value:
            return "[]"
        items = ",\n".join(f"{pad}  {render_hcl_value(item, indent + 1)}" for item in value)
        return f"[\n{items}\n{pad}]"
    if not value:
        return "{}"
    entries = []
    for key, entry in value.items():
        rendered_key = key if _HCL_IDENTIFIER_RE.match(key) else quote_hcl_template(key)
        entries.append(f"{pad}  {rendered_key} = {render_hcl_value(entry, indent + 1)}")
    return "{\n" + "\n".join(entries) + f"\n{pad}}}"


def _render_nested_block(name: str, entry: dict, indent: int = 1) -> list[str]:
    pad = "  " * indent
    lines = [f"{pad}{name} {{"]
    for key, value in entry.items():
        lines.append(f"{pad}  {key} = {render_hcl_value(value, indent + 1)}")
    lines.append(f"{pad}}}")
    return lines


def render_provider_resources(resources) -> str:
    """Render ``ResourceSpec`` blocks to HCL. Shared with the composer's contract gate."""
    blocks = []
    for resource in resources:
        lines = [f'resource "{resource.type}" "{resource.name}" {{']
        for key, value in resource.arguments.items():
            lines.append(f"  {key} = {render_hcl_value(value)}")
        for name, entries in resource.nested_blocks.items():
            for entry in entries:
                lines.extend(_render_nested_block(name, entry))
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _render_resources(component: ComponentSpec) -> str:
    implementation = component.implementation
    if implementation.kind != "provider_resources" or not implementation.resources:
        return (
            "# Deterministic skeleton generated from InfrastructureSpec.\n"
            "# No provider resources were selected for this component.\n"
        )
    return render_provider_resources(implementation.resources)


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


def _quote_hcl_string(value: str) -> str:
    """Quote free text as an HCL string literal.

    Escapes quotes/backslashes/newlines and neutralizes ``${``/``%{`` template
    sequences, so model-authored text (e.g. a composed output description) can
    never break out of the string literal or inject a template expression into
    the generated Terraform.
    """
    escaped = _escape_hcl_template(value).replace("${", "$${").replace("%{", "%%{")
    return f'"{escaped}"'


def _render_outputs(component: ComponentSpec) -> str:
    if not component.outputs:
        return ""
    return "\n".join(
        f'output "{output.name}" {{\n'
        f"  description = {_quote_hcl_string(output.description)}\n"
        f"  value       = {output.value}\n"
        "}\n"
        for output in component.outputs
    )


def apply_composition(spec: InfrastructureSpec, composed: ComposedComponent) -> InfrastructureSpec:
    """Fold a schema-validated composed implementation into the typed spec."""
    component = _component(spec)
    existing_output_names = {output.name for output in component.outputs}
    merged_outputs = component.outputs + [
        output for output in composed.outputs if output.name not in existing_output_names
    ]
    updated_component = component.model_copy(
        update={
            "implementation": ProviderResourcesSpec(resources=composed.resources),
            "outputs": merged_outputs,
        }
    )
    return spec.model_copy(
        update={
            "components": [updated_component, *spec.components[1:]],
            "assumptions": [*spec.assumptions, *composed.assumptions],
            "warnings": [w for w in spec.warnings if w != _STRUCTURE_ONLY_WARNING],
            "rendering_policy": "composed_provider_resources",
        }
    )


def default_spec_composer(logger=None) -> SpecComposer | None:
    """Composer used when none is injected; None disables composition.

    Composition needs a model (``BEDROCK_MODEL_ID``) and can be turned off with
    ``IAC_SMITH_SPEC_COMPOSER=0`` — both cases degrade to the structure-only
    renderer, so offline runs (tests, eval --replay) behave exactly as before.
    """
    if os.getenv("IAC_SMITH_SPEC_COMPOSER") == "0" or not os.getenv("BEDROCK_MODEL_ID"):
        return None
    from iac_smith.spec_composer import SpecComposer

    return SpecComposer(logger=logger)


def _with_warning(spec: InfrastructureSpec, warning: str) -> InfrastructureSpec:
    return spec.model_copy(update={"warnings": [*spec.warnings, warning]})


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _compact_finding(error: str) -> str:
    """One terraform error as a single prompt-safe line (no ANSI, bounded)."""
    return " ".join(_ANSI_RE.sub("", error).split())[:400]


class SpecRendererGenerator:
    """File-generator adapter used by graph.default_file_generator."""

    def __init__(
        self,
        composer: SpecComposer | None = None,
        logger=None,
        *,
        allow_structure_only: bool | None = None,
    ):
        self._composer = composer
        self._logger = logger
        self._allow_structure_only = allow_structure_only
        self._repair_negative_patterns: list[str] = []
        self._last_repo_path = None

    def _structure_only_allowed(self) -> bool:
        if self._allow_structure_only is not None:
            return self._allow_structure_only
        from iac_smith.legitimacy import allow_structure_only

        return allow_structure_only()

    def _log(self, message: str) -> None:
        if self._logger:
            self._logger(message)

    def generate_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset=None,
        target_repo: str,
        repo_path=None,
        blackboard: RunBlackboard | None = None,
    ) -> dict[str, str]:
        self._last_repo_path = repo_path
        spec = build_spec_from_intent(
            intent=intent,
            change_plan=change_plan,
            repo_patterns=repo_patterns,
            target_repo=target_repo,
            repo_path=Path(repo_path) if repo_path else None,
        )
        files = render_spec(spec)
        component = _component(spec)
        needs_composition = (
            bool(_planned_module_paths(change_plan))
            and component.implementation.kind == "provider_resources"
            and not component.implementation.resources
        )
        if not needs_composition:
            return files
        allow_structure_only = self._structure_only_allowed()
        composer = self._composer or default_spec_composer(self._logger)
        if composer is None:
            if not allow_structure_only:
                raise StructureOnlyBlocked(
                    "Spec composition is disabled or has no model, so the planned "
                    "workload modules would contain no provider resources. Structure-"
                    "only PRs are blocked by default; set IAC_SMITH_ALLOW_STRUCTURE_ONLY=1 "
                    "to explicitly allow placeholder output."
                )
            self._log("IaC Smith: spec composition disabled or no model; rendering structure only.")
            return files

        from iac_smith.provider_schema import build_schema_resolver
        from iac_smith.spec_composer import SpecCompositionError

        resolver = build_schema_resolver(files)
        if not resolver.provider_contracts:
            if not allow_structure_only:
                raise StructureOnlyBlocked(
                    "Provider schema harvest was unavailable, so composed resources "
                    "cannot be validated and the workload modules would be placeholders. "
                    "Structure-only PRs are blocked by default; set "
                    "IAC_SMITH_ALLOW_STRUCTURE_ONLY=1 to explicitly allow them."
                )
            self._log("IaC Smith: provider schema harvest unavailable; rendering structure only.")
            spec = _with_warning(
                spec, "Provider schema harvest was unavailable; rendered structure only."
            )
            return render_spec(spec)
        negative_patterns = [
            *(blackboard.negative_patterns if blackboard else []),
            *self._repair_negative_patterns,
        ]
        try:
            composed = composer.compose(
                intent=intent,
                component_name=component.name,
                allowed_inputs=sorted(component.inputs),
                environments=spec.environments,
                provider_contracts=resolver.provider_contracts,
                negative_patterns=negative_patterns or None,
            )
        except (SpecCompositionError, ValueError) as exc:
            if not allow_structure_only:
                self._log(f"IaC Smith: spec composition failed; blocking PR creation: {exc}")
                raise SpecCompositionError(
                    f"Spec composition failed and structure-only fallback is disabled: {exc}"
                ) from exc
            self._log(f"IaC Smith: spec composition failed; rendering structure only: {exc}")
            spec = _with_warning(spec, f"Spec composition failed; rendered structure only: {exc}")
            return render_spec(spec)
        return render_spec(apply_composition(spec, composed))

    def repair_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset=None,
        target_repo: str,
        generated_files: dict[str, str],
        repair_errors: list[str],
        blackboard: RunBlackboard | None = None,
    ) -> dict[str, str]:
        """Runtime repair for spec mode: re-compose with the real Terraform findings.

        The schema gate cannot see everything (e.g. arguments *inside* nested
        blocks — the live issue #70 run passed composition and failed
        ``terraform validate`` on a GSI's inner arguments). Because rendering is
        deterministic, "repair" here means one thing: run composition again with
        the validator's exact errors carried as never-repeat patterns.
        """
        # Accumulate (bounded) across repair rounds: later rounds must not
        # rediscover an error an earlier round already hit.
        self._repair_negative_patterns = [
            *self._repair_negative_patterns,
            *(_compact_finding(error) for error in repair_errors),
        ][-16:]
        return self.generate_files(
            intent=intent,
            change_plan=change_plan,
            repo_patterns=repo_patterns,
            ruleset=ruleset,
            target_repo=target_repo,
            repo_path=self._last_repo_path,
            blackboard=blackboard,
        )
