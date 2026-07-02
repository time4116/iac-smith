"""LLM-composed typed resource selection for the deterministic spec renderer.

This is the composition layer the spec renderer was missing: the model decides
*what* to build — typed ``ResourceSpec`` selections — and the renderer decides
*how* every file is written. The model never authors HCL text, which removes the
failure classes the freeform generator kept hitting (malformed envelopes,
cross-file contract drift, undeclared references).

Composition is generic by construction: the candidate universe is whatever the
harvested provider schema exposes (``provider_schema.build_schema_resolver``),
and every proposal is deterministically validated against that schema *before*
rendering — hallucinated resource types, unsupported arguments/blocks, missing
required arguments, and references to variables or resources that do not exist
are all rejected and fed back as findings for a bounded JSON-level repair.
Nothing is keyed to a service or provider.
"""

import json
import os
import re
from difflib import get_close_matches
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from iac_smith.blackboard import TerraformContract, validate_generated_contracts
from iac_smith.dynamic_terraform import (
    _BEDROCK_THROTTLE_CODES,
    BedrockRuntime,
    BedrockStreamError,
    _extract_json_object,
    _int_env,
    _read_stream_document,
)
from iac_smith.models.infrastructure_spec import OutputSpec, ResourceSpec
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.validation import ValidationStatus

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VAR_REF_RE = re.compile(r"\bvar\.([A-Za-z_][A-Za-z0-9_]*)")
_FORBIDDEN_ROOT_RE = re.compile(r"\b(local|data|module)\.")
_RESOURCE_REF_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\.([a-z][a-z0-9_]*)\b")
_BLOCK_NAME_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)")


class ComposedComponent(BaseModel):
    resources: list[ResourceSpec]
    outputs: list[OutputSpec] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class SpecCompositionError(RuntimeError):
    """Composition could not produce a schema-valid typed implementation."""


def _reference_errors(
    text: str,
    *,
    scope: str,
    allowed_inputs: list[str],
    known_resource_types: set[str],
    addresses: set[tuple[str, str]],
) -> list[str]:
    errors: list[str] = []
    for root in sorted({m.group(1) for m in _FORBIDDEN_ROOT_RE.finditer(text)}):
        errors.append(
            f"`{scope}` references `{root}.` — `{root}` values do not exist in a "
            f"spec-rendered module. Use a literal, an allowed input variable, or a "
            f"sibling resource attribute instead."
        )
    allowed = set(allowed_inputs)
    for name in sorted({m.group(1) for m in _VAR_REF_RE.finditer(text)}):
        if name not in allowed:
            errors.append(
                f"`{scope}` references undeclared variable `var.{name}`. The only "
                f"input variables are: {', '.join(allowed_inputs)}."
            )
    for match in _RESOURCE_REF_RE.finditer(text):
        rtype, rname = match.group(1), match.group(2)
        if rtype in known_resource_types and (rtype, rname) not in addresses:
            errors.append(
                f"`{scope}` references `{rtype}.{rname}`, but no resource with that "
                f"type and name is composed. Reference a composed sibling resource."
            )
    return errors


def validate_composed_component(
    composed: ComposedComponent,
    *,
    provider_contracts: dict[str, TerraformContract],
    known_resource_types: set[str],
    allowed_inputs: list[str],
    component_name: str,
) -> list[str]:
    """Deterministically validate a composed implementation before rendering.

    Reuses the contract gate (``validate_generated_contracts``) on the resources
    rendered in isolation, then adds the spec-level checks the gate cannot see:
    required arguments, nested-block names, and reference integrity (variables,
    sibling resources, and forbidden ``local.``/``data.``/``module.`` roots).
    """
    from iac_smith.spec_renderer import render_provider_resources

    errors: list[str] = []
    if not composed.resources:
        return ["Composition must select at least one provider resource."]
    addresses: set[tuple[str, str]] = set()
    for resource in composed.resources:
        scope = f"{resource.type}.{resource.name}"
        if not _IDENTIFIER_RE.match(resource.name):
            errors.append(
                f"Resource name `{resource.name}` on `{resource.type}` must be a "
                f"lowercase snake_case identifier."
            )
        if (resource.type, resource.name) in addresses:
            errors.append(f"Duplicate resource address `{scope}`.")
        addresses.add((resource.type, resource.name))

    rendered = render_provider_resources(composed.resources)
    gate = validate_generated_contracts(
        {f"modules/{component_name}/main.tf": rendered},
        provider_contracts,
        known_resource_types=known_resource_types,
    )
    if gate.status == ValidationStatus.FAILED:
        errors.extend(gate.errors)

    for resource in composed.resources:
        scope = f"{resource.type}.{resource.name}"
        contract = provider_contracts.get(resource.type)
        if contract:
            for required in contract.required_arguments:
                if required not in resource.arguments:
                    errors.append(
                        f"`{scope}` is missing required argument `{required}` "
                        f"(required by {contract.source})."
                    )
            allowed_args = set(contract.allowed_arguments)
            for block in resource.blocks:
                match = _BLOCK_NAME_RE.match(block)
                if not match:
                    errors.append(f"`{scope}` has a nested block without a parseable name.")
                    continue
                if allowed_args and match.group(1) not in allowed_args:
                    errors.append(
                        f"`{scope}` uses unsupported nested block `{match.group(1)}`. "
                        f"Allowed arguments and blocks from {contract.source}: "
                        f"{', '.join(contract.allowed_arguments)}."
                    )
        text = "\n".join([*resource.arguments.values(), *resource.blocks])
        errors.extend(
            _reference_errors(
                text,
                scope=scope,
                allowed_inputs=allowed_inputs,
                known_resource_types=known_resource_types,
                addresses=addresses,
            )
        )
    seen_output_names: set[str] = set()
    for output in composed.outputs:
        if not _IDENTIFIER_RE.match(output.name):
            errors.append(f"Output name `{output.name}` must be a lowercase snake_case identifier.")
        if output.name in seen_output_names:
            errors.append(f"Duplicate output name `{output.name}`.")
        seen_output_names.add(output.name)
        errors.extend(
            _reference_errors(
                output.value,
                scope=f"output {output.name}",
                allowed_inputs=allowed_inputs,
                known_resource_types=known_resource_types,
                addresses=addresses,
            )
        )
    return errors


def _nearest_types_hint(unknown: list[str], known_resource_types: set[str]) -> str:
    lines = []
    for candidate in unknown:
        matches = get_close_matches(candidate, sorted(known_resource_types), n=3, cutoff=0.6)
        if matches:
            lines.append(
                f"- `{candidate}` does not exist; the closest types the provider "
                f"defines are: {', '.join(f'`{m}`' for m in matches)}."
            )
        else:
            lines.append(f"- `{candidate}` does not exist in the provider schema; drop it.")
    return "\n".join(lines)


class SpecComposer:
    """Compose a typed provider-resource implementation for one component."""

    def __init__(
        self,
        model_id: str | None = None,
        bedrock_runtime: BedrockRuntime | None = None,
        *,
        read_timeout_seconds: int = 180,
        max_attempts: int = 2,
        max_tokens: int = 8192,
        max_repair_rounds: int = 2,
    ) -> None:
        self.model_id = model_id or os.getenv("BEDROCK_MODEL_ID", "")
        if not self.model_id:
            raise ValueError("BEDROCK_MODEL_ID must be set to compose an infrastructure spec.")
        self._bedrock_runtime = bedrock_runtime
        self.read_timeout_seconds = _int_env("IAC_SMITH_BEDROCK_READ_TIMEOUT", read_timeout_seconds)
        self.max_attempts = _int_env("IAC_SMITH_BEDROCK_MAX_ATTEMPTS", max_attempts)
        self.max_tokens = _int_env("IAC_SMITH_COMPOSER_MAX_TOKENS", max_tokens)
        self.max_repair_rounds = max_repair_rounds

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
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
        return self._bedrock_runtime

    def _invoke_json(self, prompt: str) -> dict[str, Any]:
        from botocore.exceptions import (
            ClientError,
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )

        transient = (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        last_error: Exception | None = None
        for _attempt in range(1, self.max_attempts + 1):
            try:
                response = self.bedrock_runtime.invoke_model_with_response_stream(
                    modelId=self.model_id,
                    contentType="application/json",
                    accept="application/json",
                    body=body,
                )
                text, stop_reason = _read_stream_document(response)
                if stop_reason == "max_tokens":
                    raise SpecCompositionError(
                        "Spec composition response was truncated at the output token cap; "
                        "raise IAC_SMITH_COMPOSER_MAX_TOKENS."
                    )
                return _extract_json_object(text)
            except transient as exc:
                last_error = exc
            except BedrockStreamError as exc:
                if not exc.transient:
                    raise
                last_error = exc
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in _BEDROCK_THROTTLE_CODES:
                    raise
                last_error = exc
        assert last_error is not None
        raise last_error

    def _context_lines(
        self,
        *,
        intent: InfrastructureIntent,
        component_name: str,
        allowed_inputs: list[str],
        environments: list[str],
    ) -> list[str]:
        lines = [
            "You are IaC Smith's infrastructure spec composer. You select typed Terraform",
            "resources; a deterministic renderer writes every file. Never write HCL files",
            "or prose outside the requested JSON.",
            "",
            "Request from the repository issue:",
            intent.raw_request,
            "",
            "Deployment context:",
            f"- Region: {intent.region}",
            f"- Environments: {', '.join(environments)}",
            f"- Terraform module: modules/{component_name}",
            f"- Input variables available to the module: {', '.join(allowed_inputs)}",
            "- The module must be self-sufficient: anything the request depends on that",
            "  is not provided by an input variable (for example networking) must be",
            "  created by resources inside this module.",
        ]
        if intent.features:
            lines.append(f"- Requested features: {', '.join(intent.features)}")
        return lines

    def _negative_pattern_lines(self, negative_patterns: list[str] | None) -> list[str]:
        if not negative_patterns:
            return []
        return [
            "",
            "Known invalid patterns from earlier validation of this run (never repeat them):",
            *(f"- {pattern}" for pattern in negative_patterns),
        ]

    def _select_resource_types(
        self,
        *,
        intent: InfrastructureIntent,
        component_name: str,
        allowed_inputs: list[str],
        environments: list[str],
        known_resource_types: set[str],
        negative_patterns: list[str] | None,
    ) -> tuple[list[str], list[str]]:
        provider_prefixes = sorted({rtype.split("_", 1)[0] for rtype in known_resource_types})
        hint = ""
        known: list[str] = []
        unknown: list[str] = []
        for attempt in range(2):
            lines = [
                *self._context_lines(
                    intent=intent,
                    component_name=component_name,
                    allowed_inputs=allowed_inputs,
                    environments=environments,
                ),
                f"- Provider resource prefixes available: {', '.join(provider_prefixes)}",
                *self._negative_pattern_lines(negative_patterns),
            ]
            if hint:
                lines.extend(["", "Your previous selection contained invalid types:", hint])
            lines.extend(
                [
                    "",
                    'Return ONLY a JSON object {"resource_types": ["<type>", ...]} listing',
                    "every provider resource type needed for a complete, production-",
                    "appropriate implementation of the request. Include only resource types",
                    "you are certain the provider defines.",
                ]
            )
            payload = self._invoke_json("\n".join(lines))
            proposed = payload.get("resource_types")
            if not isinstance(proposed, list) or not all(isinstance(t, str) for t in proposed):
                raise SpecCompositionError(
                    'Type selection response must be {"resource_types": [<strings>]}.'
                )
            deduped = list(dict.fromkeys(proposed))
            known = [t for t in deduped if t in known_resource_types]
            unknown = [t for t in deduped if t not in known_resource_types]
            if known and not unknown:
                return known, []
            if attempt == 0 and unknown:
                hint = _nearest_types_hint(unknown, known_resource_types)
        if known:
            dropped = ", ".join(f"`{t}`" for t in unknown)
            return known, [
                f"Spec composition dropped resource types the provider does not define: {dropped}."
            ]
        raise SpecCompositionError(
            "Type selection produced no resource types the provider schema defines: "
            + ", ".join(f"`{t}`" for t in unknown)
        )

    def _contract_lines(self, contracts: dict[str, TerraformContract]) -> list[str]:
        lines = [
            "",
            "Authoritative provider schema contracts (from `terraform providers schema -json`):",
        ]
        for name in sorted(contracts):
            contract = contracts[name]
            lines.append(f"- {name}")
            if contract.required_arguments:
                lines.append(f"  required arguments: {', '.join(contract.required_arguments)}")
            lines.append(
                f"  allowed arguments and nested blocks: {', '.join(contract.allowed_arguments)}"
            )
        return lines

    def _compose_once(
        self,
        *,
        intent: InfrastructureIntent,
        component_name: str,
        allowed_inputs: list[str],
        environments: list[str],
        contracts: dict[str, TerraformContract],
        negative_patterns: list[str] | None,
        findings: list[str],
    ) -> ComposedComponent:
        lines = [
            *self._context_lines(
                intent=intent,
                component_name=component_name,
                allowed_inputs=allowed_inputs,
                environments=environments,
            ),
            *self._contract_lines(contracts),
            *self._negative_pattern_lines(negative_patterns),
            "",
            "Compose the resources implementing the request. Rules:",
            "- Return ONLY JSON:",
            '  {"resources": [{"type": "...", "name": "...", "arguments": {"<arg>":',
            '  "<expression>"}, "blocks": ["<nested block HCL>"]}, ...],',
            '  "outputs": [{"name": "...", "description": "...", "value": "..."}],',
            '  "assumptions": ["..."]}',
            "- Every `arguments` value is a Terraform expression rendered verbatim into",
            '  HCL: quote string literals (e.g. "\\"example\\""), leave numbers, booleans,',
            "  lists, and references bare.",
            "- Use only argument names from a type's allowed list; include every required",
            "  argument.",
            "- `blocks` entries are complete nested HCL blocks; each must start with a",
            "  nested block name from the type's allowed list.",
            "- Reference sibling resources as <type>.<name>.<attribute>.",
            f"- Reference only these input variables: {', '.join(allowed_inputs)}. Never",
            "  reference local., data., or module. values — they do not exist here.",
            "- You may add resource types beyond the contracts above only if you are",
            "  certain the provider defines them; they are validated the same way.",
            "- Resource and output names are lowercase snake_case identifiers.",
            "- `outputs` expose the identifiers consumers of this stack need.",
        ]
        if findings:
            lines.extend(
                [
                    "",
                    "Your previous composition failed deterministic validation. Fix every",
                    "finding below without introducing new violations:",
                    *(f"- {finding}" for finding in findings),
                ]
            )
        payload = self._invoke_json("\n".join(lines))
        try:
            return ComposedComponent.model_validate(payload)
        except ValidationError as exc:
            raise SpecCompositionError(
                f"Composition response did not match the expected JSON shape: {exc}"
            ) from exc

    def compose(
        self,
        *,
        intent: InfrastructureIntent,
        component_name: str,
        allowed_inputs: list[str],
        environments: list[str],
        provider_contracts: dict[str, TerraformContract],
        negative_patterns: list[str] | None = None,
    ) -> ComposedComponent:
        known_resource_types = set(provider_contracts)
        selected, selection_warnings = self._select_resource_types(
            intent=intent,
            component_name=component_name,
            allowed_inputs=allowed_inputs,
            environments=environments,
            known_resource_types=known_resource_types,
            negative_patterns=negative_patterns,
        )
        contracts = {name: provider_contracts[name] for name in selected}
        findings: list[str] = []
        for _ in range(self.max_repair_rounds + 1):
            composed = self._compose_once(
                intent=intent,
                component_name=component_name,
                allowed_inputs=allowed_inputs,
                environments=environments,
                contracts=contracts,
                negative_patterns=negative_patterns,
                findings=findings,
            )
            findings = validate_composed_component(
                composed,
                provider_contracts=provider_contracts,
                known_resource_types=known_resource_types,
                allowed_inputs=allowed_inputs,
                component_name=component_name,
            )
            if not findings:
                composed.assumptions.extend(selection_warnings)
                return composed
        raise SpecCompositionError(
            "Composed resources failed deterministic schema validation after "
            f"{self.max_repair_rounds + 1} attempts: " + "; ".join(findings)
        )
