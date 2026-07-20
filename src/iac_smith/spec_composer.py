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
from collections.abc import Callable, Iterator
from difflib import get_close_matches
from typing import Any

from pydantic import BaseModel, Field, JsonValue, ValidationError

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
from iac_smith.registry_modules import RegistryModuleContract

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BARE_REFERENCE_HEAD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_]")
_VAR_REF_RE = re.compile(r"\bvar\.([A-Za-z_][A-Za-z0-9_]*)")
_FORBIDDEN_ROOT_RE = re.compile(r"\b(local|data|module)\.")
_RESOURCE_REF_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\.([a-z][a-z0-9_]*)\b")
_NESTED_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMAGE_FIELD_RE = re.compile(r"(^|_)image(_|$)|container_image|image_identifier|image_uri")
_IMAGE_LITERAL_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[A-Fa-f0-9]{64})?$"
)


class ComposedRegistryModule(BaseModel):
    """A community-module call selected against a harvested registry contract."""

    source: str
    # Pinned from the registry contract after validation, never by the model.
    version: str = ""
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)


class ComposedComponent(BaseModel):
    resources: list[ResourceSpec] = Field(default_factory=list)
    registry_module: ComposedRegistryModule | None = None
    outputs: list[OutputSpec] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    # Populated from the provider contracts after validation (never by the
    # model): per resource type, the schema's nested-block names so rendering
    # can emit real blocks. See ProviderResourcesSpec.block_names.
    block_names: dict[str, list[str]] = Field(default_factory=dict)


def normalize_composed_blocks(
    composed: ComposedComponent, provider_contracts: dict[str, TerraformContract]
) -> ComposedComponent:
    """Move block-typed argument entries into ``nested_blocks``.

    The prompt says argument values follow Terraform JSON semantics, and in
    Terraform JSON a nested block is written ``"name": [{...}]`` — so the model
    putting blocks in ``arguments`` is faithful, not wrong (the live issue
    #69/#70 runs did exactly this). Canonicalizing deterministically beats
    asking the model to relearn the split.
    """
    resources = []
    for resource in composed.resources:
        contract = provider_contracts.get(resource.type)
        if contract is None:
            resources.append(resource)
            continue
        block_names = set(contract.block_names)
        attribute_names = set(contract.allowed_arguments) - block_names
        arguments = dict(resource.arguments)
        nested_blocks = {name: list(entries) for name, entries in resource.nested_blocks.items()}
        for key in list(arguments):
            if key not in block_names:
                continue
            value = arguments[key]
            entries = (
                [value]
                if isinstance(value, dict)
                else value
                if isinstance(value, list) and value and all(isinstance(v, dict) for v in value)
                else None
            )
            if entries is None:
                continue
            nested_blocks.setdefault(key, []).extend(entries)
            del arguments[key]
        # The inverse direction too: an attribute placed in nested_blocks (the
        # live issue #70 run put the alarm's `dimensions` map there) must render
        # as an assignment, not a block Terraform will reject.
        for key in list(nested_blocks):
            if key not in attribute_names or key in block_names:
                continue
            entries = nested_blocks.pop(key)
            arguments.setdefault(key, entries[0] if len(entries) == 1 else entries)
        resources.append(
            resource.model_copy(update={"arguments": arguments, "nested_blocks": nested_blocks})
        )
    return composed.model_copy(update={"resources": resources})


class SpecCompositionError(RuntimeError):
    """Composition could not produce a schema-valid typed implementation."""


def _iter_string_leaves(value) -> Iterator[str]:
    """Yield every string leaf of a native JSON argument value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for entry in value.values():
            yield from _iter_string_leaves(entry)
    elif isinstance(value, list):
        for entry in value:
            yield from _iter_string_leaves(entry)


def _shape_findings(exc: ValidationError, limit: int = 12) -> list[str]:
    """Turn a pydantic shape mismatch into compact repair findings.

    A wrong response shape must drive a repair round like any other violation,
    not abort composition — and the raw pydantic report for a large document can
    run to dozens of near-identical entries, so it is truncated for the prompt.
    """
    errors = exc.errors()
    findings = [
        (
            f"Response field `{'.'.join(str(part) for part in err['loc'])}`: the raw-HCL "
            "`blocks` channel does not exist. Express every nested block as a structured "
            '`nested_blocks` entry, e.g. {"nested_blocks": {"<block name>": [{"<arg>": '
            "<value>}]}}."
        )
        if err["type"] == "extra_forbidden" and err["loc"] and err["loc"][-1] == "blocks"
        else (
            f"Response field `{'.'.join(str(part) for part in err['loc'])}`: {err['msg']}. "
            "Match the required JSON shape exactly."
        )
        for err in errors[:limit]
    ]
    if len(errors) > limit:
        findings.append(
            f"...and {len(errors) - limit} more fields with the same kinds of shape errors."
        )
    return findings


def _bare_reference_errors(
    leaves: list[str], *, scope: str, known_resource_types: set[str]
) -> list[str]:
    """Reject argument strings that are bare Terraform references.

    Argument strings render as quoted templates, so a bare ``var.foo`` or
    ``aws_kms_key.db.arn`` value would become a *literal string* — syntactically
    valid HCL that silently wires the wrong value. The reference must ride
    inside ``${...}`` interpolation. Blocks and output values are exempt: they
    render as raw HCL where bare references are correct.
    """
    errors: list[str] = []
    for leaf in leaves:
        candidate = leaf.strip()
        match = _BARE_REFERENCE_HEAD_RE.match(candidate)
        if not match:
            continue
        head = match.group(1)
        if head == "var" or head in known_resource_types:
            errors.append(
                f"`{scope}` argument value `{candidate}` is a bare Terraform reference, "
                f"but argument strings render as literal text. Wrap it in interpolation: "
                f'"${{{candidate}}}".'
            )
    return errors


def _container_image_pin_errors(*, scope: str, field_name: str, leaves: list[str]) -> list[str]:
    """Flag unpinned container image literals in image-shaped fields.

    This is intentionally lexical rather than AWS-specific. Generated IaC that
    uses ``nginx`` or ``nginx:latest`` can pass provider schemas and plans while
    remaining non-repeatable at runtime. Variables, interpolations, AMI IDs, and
    digest-pinned images are left alone because this gate cannot prove their
    container-image semantics from a JSON leaf alone.
    """
    if not _IMAGE_FIELD_RE.search(field_name.lower()):
        return []
    errors: list[str] = []
    for leaf in leaves:
        image = leaf.strip()
        if (
            not image
            or "${" in image
            or image.startswith(("var.", "ami-"))
            or "://" in image
            or not _IMAGE_LITERAL_RE.match(image)
        ):
            continue
        if "@sha256:" in image:
            continue
        tag = image.rsplit("/", 1)[-1].rsplit(":", 1)
        if len(tag) == 1 or tag[1] == "latest":
            errors.append(
                f"`{scope}` field `{field_name}` uses unpinned container image `{image}`. "
                "Use an immutable digest or a non-latest version tag so generated "
                "infrastructure is reproducible at runtime."
            )
    return errors


def _hcl_parse_errors(rendered: str, *, scope: str) -> list[str]:
    """Reject rendered HCL that does not parse.

    The contract gate and reference scans are regex-level; an expression the
    model wrote (e.g. an output value) can still be syntactically invalid HCL
    that Terraform would only reject at runtime. Parsing the rendered text
    turns that into a pre-render repair finding.
    """
    import hcl2

    try:
        hcl2.loads(rendered)
    except Exception as exc:  # lark surfaces several exception types
        detail = " ".join(str(exc).split())[:300]
        return [
            f"The {scope} do not parse as valid HCL: {detail} — fix the syntax "
            "(quote string literals, balance braces, close every block)."
        ]
    return []


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
        message = (
            f"`{scope}` references `{root}.` — `{root}` values do not exist in a "
            f"spec-rendered module. Use a literal, an allowed input variable, or a "
            f"sibling resource attribute instead."
        )
        if root == "data":
            # The live #68 run burned every round reaching for
            # data.aws_caller_identity to build a unique bucket name; the
            # finding must name the working alternative, not just reject.
            message += (
                " If this was for a globally unique name, omit the optional name "
                "argument entirely (the provider generates one) or build the name "
                "only from the allowed input variables."
            )
        errors.append(message)
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


def _nested_block_errors(
    *,
    scope: str,
    block_name: str,
    entries: list[dict],
    block_required: dict[str, list[str]],
    block_names: set[str],
) -> list[str]:
    """Validate one nested block's entries, recursing into inner blocks."""
    errors: list[str] = []
    for index, entry in enumerate(entries, start=1):
        for required in block_required.get(block_name, []):
            if required not in entry:
                errors.append(
                    f"`{scope}` nested block `{block_name}` entry {index} is missing "
                    f"required argument `{required}` (required by the provider schema)."
                )
        for key, value in entry.items():
            if not _NESTED_KEY_RE.match(key):
                errors.append(
                    f"`{scope}` nested block `{block_name}` has argument name "
                    f"`{key}` that is not a valid identifier."
                )
            if key in block_names:
                inner_entries = (
                    [value]
                    if isinstance(value, dict)
                    else value
                    if isinstance(value, list) and all(isinstance(v, dict) for v in value)
                    else []
                )
                if inner_entries:
                    errors.extend(
                        _nested_block_errors(
                            scope=scope,
                            block_name=key,
                            entries=inner_entries,
                            block_required=block_required,
                            block_names=block_names,
                        )
                    )
    return errors


_INTERPOLATION_RE = re.compile(r"\$\{([^}]+)\}")


def validate_composed_registry_module(
    module: ComposedRegistryModule,
    *,
    contracts: dict[str, RegistryModuleContract],
    allowed_inputs: list[str],
) -> list[str]:
    """Deterministically validate a module call against its registry contract.

    Mirrors the provider-schema gate: hallucinated modules, inputs the module
    does not define, missing required inputs, unknown outputs, and references
    to anything other than the allowed input variables are all repair findings.
    """
    contract = contracts.get(module.source)
    if contract is None:
        offered = ", ".join(f"`{source}`" for source in sorted(contracts))
        return [f"`{module.source}` is not one of the offered community modules: {offered}."]
    errors: list[str] = []
    unknown = sorted(set(module.inputs) - set(contract.inputs))
    if unknown:
        sample = ", ".join(sorted(contract.inputs)[:40])
        errors.append(
            f"Module `{module.source}` does not define input(s): "
            + ", ".join(f"`{name}`" for name in unknown)
            + f". Its inputs include: {sample}."
        )
    missing = [name for name in contract.required_inputs if name not in module.inputs]
    if missing:
        errors.append(
            f"Module `{module.source}` is missing required input(s): "
            + ", ".join(f"`{name}`" for name in missing)
            + "."
        )
    allowed = set(allowed_inputs)
    for name, value in module.inputs.items():
        scope = f"{module.source} input `{name}`"
        errors.extend(
            _container_image_pin_errors(
                scope=module.source,
                field_name=name,
                leaves=list(_iter_string_leaves(value)),
            )
        )
        for leaf in _iter_string_leaves(value):
            errors.extend(_bare_reference_errors([leaf], scope=scope, known_resource_types=set()))
            for expression in _INTERPOLATION_RE.findall(leaf):
                if _FORBIDDEN_ROOT_RE.search(expression) or _RESOURCE_REF_RE.search(expression):
                    errors.append(
                        f"`{scope}` references `{expression.strip()}`, but a module call "
                        "has no sibling resources or local/data/module values. Only "
                        "literals and the allowed input variables exist here."
                    )
                    continue
                for var_name in _VAR_REF_RE.findall(expression):
                    if var_name not in allowed:
                        errors.append(
                            f"`{scope}` references undeclared variable `var.{var_name}`. "
                            f"The only input variables are: {', '.join(allowed_inputs)}."
                        )
    unknown_outputs = [name for name in module.outputs if name not in contract.outputs]
    if unknown_outputs:
        sample = ", ".join(contract.outputs[:40])
        errors.append(
            f"Module `{module.source}` does not define output(s): "
            + ", ".join(f"`{name}`" for name in unknown_outputs)
            + f". Its outputs include: {sample}."
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

    rendered = render_provider_resources(
        composed.resources,
        {
            rtype: contract.block_names
            for rtype, contract in provider_contracts.items()
            if contract.block_names
        },
    )
    gate = validate_generated_contracts(
        {f"modules/{component_name}/main.tf": rendered},
        provider_contracts,
        known_resource_types=known_resource_types,
    )
    if gate.status == ValidationStatus.FAILED:
        errors.extend(gate.errors)
    errors.extend(_hcl_parse_errors(rendered, scope="rendered module resources"))
    for output in composed.outputs:
        errors.extend(
            _hcl_parse_errors(
                f'output "{output.name}" {{\n  value = {output.value}\n}}\n',
                scope=f"output {output.name}",
            )
        )

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
            for block_name in resource.nested_blocks:
                if allowed_args and block_name not in allowed_args:
                    errors.append(
                        f"`{scope}` uses unsupported nested block `{block_name}`. Use one "
                        f"of the provider-declared names from {contract.source}: "
                        f"{', '.join(contract.allowed_arguments)} — or drop the block if "
                        f"no name fits; never invent one."
                    )
        block_required = contract.block_required_arguments if contract else {}
        deep_block_names = set(contract.block_names) if contract else set()
        for block_name, entries in resource.nested_blocks.items():
            errors.extend(
                _nested_block_errors(
                    scope=scope,
                    block_name=block_name,
                    entries=entries,
                    block_required=block_required,
                    block_names=deep_block_names,
                )
            )
        argument_leaves = [
            leaf for value in resource.arguments.values() for leaf in _iter_string_leaves(value)
        ]
        for name, value in resource.arguments.items():
            errors.extend(
                _container_image_pin_errors(
                    scope=scope,
                    field_name=name,
                    leaves=list(_iter_string_leaves(value)),
                )
            )
        for block_name, entries in resource.nested_blocks.items():
            for entry in entries:
                for name, value in entry.items():
                    errors.extend(
                        _container_image_pin_errors(
                            scope=f"{scope}.{block_name}",
                            field_name=name,
                            leaves=list(_iter_string_leaves(value)),
                        )
                    )
        argument_leaves.extend(
            leaf
            for entries in resource.nested_blocks.values()
            for entry in entries
            for value in entry.values()
            for leaf in _iter_string_leaves(value)
        )
        errors.extend(
            _bare_reference_errors(
                argument_leaves, scope=scope, known_resource_types=known_resource_types
            )
        )
        text = "\n".join(argument_leaves)
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


def _staged_groups(selected: list[str]) -> list[list[str]]:
    """Group selected types by their service token (second underscore segment).

    Purely lexical, so it stays provider-generic: ``aws_rds_cluster`` and
    ``aws_rds_cluster_instance`` land in one stage, ``aws_kms_key`` in another.
    """
    groups: dict[str, list[str]] = {}
    for rtype in selected:
        parts = rtype.split("_")
        service = parts[1] if len(parts) > 1 else parts[0]
        groups.setdefault(service, []).append(rtype)
    return list(groups.values())


def _oversized_request_message(selected: list[str], cap: int) -> str:
    stages = "; ".join(
        f"stage {index}: {', '.join(group)}"
        for index, group in enumerate(_staged_groups(selected), start=1)
    )
    return (
        f"The request needs {len(selected)} provider resource types, above the "
        f"{cap}-type reliability cap (IAC_SMITH_MAX_RESOURCE_TYPES). It is too broad to "
        "implement as one trustworthy PR. File it as staged issues in dependency "
        f"order, one PR each: {stages}."
    )


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
        max_tokens: int = 32768,
        max_repair_rounds: int = 2,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.model_id = model_id or os.getenv("BEDROCK_MODEL_ID", "")
        if not self.model_id:
            raise ValueError("BEDROCK_MODEL_ID must be set to compose an infrastructure spec.")
        self._bedrock_runtime = bedrock_runtime
        self.read_timeout_seconds = _int_env("IAC_SMITH_BEDROCK_READ_TIMEOUT", read_timeout_seconds)
        self.max_attempts = _int_env("IAC_SMITH_BEDROCK_MAX_ATTEMPTS", max_attempts)
        # A platform-sized composition (dozens of resources with full argument
        # maps) is a large JSON document; with temperature 0 the model stops at
        # end_turn, so this cap bounds the worst case, not typical cost. Claude
        # Sonnet/Haiku on Bedrock support 64K output, so 32K leaves headroom on
        # both. Truncation raises instead of parsing a cut-off document.
        self.max_tokens = _int_env("IAC_SMITH_COMPOSER_MAX_TOKENS", max_tokens)
        self.max_repair_rounds = max_repair_rounds
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
                try:
                    return _extract_json_object(text)
                except ValueError as exc:
                    flattened = " ".join(text.split())
                    # The extractor's error carries decode position context, but
                    # a repeated live failure needs the whole document in the run
                    # log to be diagnosable (bounded: compositions are a few KB).
                    self._log(
                        f"IaC Smith: unparseable model response ({len(flattened)} chars): "
                        f"{flattened[:6000]}"
                    )
                    detail = f"Response began: {flattened[:160]!r}"
                    if len(flattened) > 160:
                        detail += f" and ended: {flattened[-160:]!r}"
                    raise ValueError(f"{exc} {detail}") from exc
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
                lines.extend(["", "Your previous selection was invalid:", hint])
            lines.extend(
                [
                    "",
                    'Return ONLY a JSON object {"resource_types": ["<type>", ...]} listing',
                    "every provider resource type needed for a complete, production-",
                    "appropriate implementation of the request. Include only resource types",
                    "you are certain the provider defines.",
                ]
            )
            try:
                payload = self._invoke_json("\n".join(lines))
            except ValueError as exc:
                # Same rule as composition: an unparseable response is repairable,
                # and restating the failure changes the prompt for the retry.
                if attempt == 0:
                    hint = (
                        f"- Your previous response could not be parsed: {exc} Return "
                        'exactly one JSON object of the form {"resource_types":'
                        ' ["<type>", ...]} with no prose and no markdown fences.'
                    )
                    self._log(
                        "IaC Smith: type selection response was not parseable JSON; "
                        f"repairing. ({exc})"
                    )
                    continue
                raise SpecCompositionError(
                    f"Type selection response was not parseable JSON: {exc}"
                ) from exc
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
            if contract.block_names:
                lines.append(
                    f"  names that are nested blocks (use `nested_blocks`): "
                    f"{', '.join(contract.block_names)}"
                )
        return lines

    def _registry_candidate_lines(self, contracts: dict[str, RegistryModuleContract]) -> list[str]:
        lines = [
            "",
            "Community modules available (contracts harvested from the Terraform Registry):",
        ]
        for source in sorted(contracts):
            contract = contracts[source]
            lines.append(f"- {source} (version {contract.version})")
            if contract.description:
                lines.append(f"  {contract.description[:160]}")
            if contract.required_inputs:
                lines.append(f"  required inputs: {', '.join(contract.required_inputs)}")
            optional = sorted(set(contract.inputs) - set(contract.required_inputs))
            if optional:
                lines.append(f"  optional inputs: {', '.join(optional[:40])}")
            if contract.outputs:
                lines.append(f"  outputs: {', '.join(contract.outputs[:40])}")
        return lines

    def _compose_registry_module(
        self,
        *,
        intent: InfrastructureIntent,
        component_name: str,
        allowed_inputs: list[str],
        environments: list[str],
        candidates: list[RegistryModuleContract],
        negative_patterns: list[str] | None,
        previous: ComposedRegistryModule | None = None,
        initial_findings: list[str] | None = None,
    ) -> ComposedRegistryModule | None:
        """Select and fill one community-module call, or None to compose raw resources.

        Fresh composition treats a decline or exhausted repair rounds as "use raw
        provider resources instead" (fail-soft). Repairing an existing module call
        (``previous``) raises on failure: abandoning the implementation mid-repair
        would silently change what the PR contains.
        """
        contracts = {contract.source: contract for contract in candidates}
        findings = list(initial_findings or [])
        repairing = previous is not None
        for round_number in range(1, self.max_repair_rounds + 2):
            previous_lines: list[str] = []
            if previous is not None:
                previous_lines = [
                    "",
                    "Your previous module call is below. It failed the findings at the",
                    "end of this prompt. Apply the SMALLEST change that resolves every",
                    "finding and keep everything else identical:",
                    json.dumps(
                        previous.model_dump(include={"source", "inputs", "outputs"}),
                        separators=(",", ":"),
                    ),
                ]
            lines = [
                *self._context_lines(
                    intent=intent,
                    component_name=component_name,
                    allowed_inputs=allowed_inputs,
                    environments=environments,
                ),
                *self._registry_candidate_lines(contracts),
                *self._negative_pattern_lines(negative_patterns),
                *previous_lines,
                "",
                "Decide whether ONE community module above fully implements the request.",
                "Rules:",
                '- Return ONLY JSON: {"registry_module": {"source": "<candidate source>",',
                '  "inputs": {"<input>": <value>}, "outputs": ["<module output>", ...]}}',
                '- Or return {"registry_module": null} when raw provider resources fit',
                "  the request better (module too narrow, wrong service, or the request",
                "  needs resources no single candidate covers).",
                "- `inputs` values are native JSON (numbers, booleans, lists, objects;",
                "  strings are quoted templates). Wrap variable references in",
                '  interpolation (e.g. "${var.environment}"). No other references exist',
                "  in a module call: never reference resources, data sources, locals, or",
                "  other modules.",
                "- Use only input names the chosen module defines; set every required",
                "  input; rely on the module's defaults otherwise.",
                "- `outputs` lists the module outputs consumers of this stack need,",
                "  chosen from the module's outputs.",
            ]
            if findings:
                lines.extend(
                    [
                        "",
                        "Your previous response failed deterministic validation. Fix every",
                        "finding below without introducing new violations:",
                        *(f"- {finding}" for finding in findings),
                    ]
                )
            try:
                payload = self._invoke_json("\n".join(lines))
            except ValueError as exc:
                findings = [
                    f"Your previous response could not be parsed: {exc} Return exactly "
                    'one JSON object of the form {"registry_module": {...}} or '
                    '{"registry_module": null} with no prose and no markdown fences.'
                ]
                self._log(
                    f"IaC Smith: registry module round {round_number} response was not "
                    f"parseable JSON; repairing. ({exc})"
                )
                continue
            if "registry_module" not in payload:
                findings = [
                    'The response must be {"registry_module": {...}} or '
                    '{"registry_module": null} — the `registry_module` key was missing.'
                ]
                continue
            selection = payload["registry_module"]
            if selection is None:
                if repairing:
                    raise SpecCompositionError(
                        "Runtime repair abandoned the community-module implementation; "
                        "blocking rather than silently changing what the PR contains."
                    )
                self._log("IaC Smith: model declined community modules; composing raw resources.")
                return None
            try:
                module = ComposedRegistryModule.model_validate(selection)
            except ValidationError as exc:
                findings = _shape_findings(exc)
                self._log(
                    f"IaC Smith: registry module round {round_number} response shape was "
                    f"invalid ({len(exc.errors())} field error(s)); repairing."
                )
                continue
            findings = validate_composed_registry_module(
                module, contracts=contracts, allowed_inputs=allowed_inputs
            )
            if not findings:
                contract = contracts[module.source]
                self._log(
                    f"IaC Smith: composed community module call `{module.source}` "
                    f"(version {contract.version}) for `{component_name}`."
                )
                return module.model_copy(update={"version": contract.version})
            previous = module
            self._log(
                f"IaC Smith: registry module round {round_number} failed deterministic "
                f"validation with {len(findings)} finding(s); repairing."
            )
        if repairing:
            raise SpecCompositionError(
                "Runtime repair of the community-module call did not converge after "
                f"{self.max_repair_rounds + 1} attempts: " + "; ".join(findings)
            )
        self._log(
            "IaC Smith: community-module composition did not validate; "
            "falling back to raw provider resources."
        )
        return None

    def _registry_component(self, module: ComposedRegistryModule) -> ComposedComponent:
        return ComposedComponent(
            registry_module=module,
            outputs=[
                OutputSpec(
                    name=name,
                    description=f"`{name}` from `{module.source}`.",
                    value=f"module.this.{name}",
                )
                for name in module.outputs
            ],
            assumptions=[
                f"Implemented via community module `{module.source}` "
                f"pinned to version `{module.version}`."
            ],
        )

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
        previous: ComposedComponent | None = None,
    ) -> dict[str, Any]:
        previous_lines: list[str] = []
        if previous is not None:
            # Iterative repair: regenerating from scratch at temperature 0
            # reproduces the same document, so a failing run never converges
            # (the live issue #70 loop). Showing the prior composition turns
            # "compose again" into "apply the minimal edit".
            previous_lines = [
                "",
                "Your previous composition is below. It failed the findings at the",
                "end of this prompt. Apply the SMALLEST change that resolves every",
                "finding — usually removing or adjusting one entry — and keep every",
                "other resource, argument, and output identical:",
                json.dumps(
                    previous.model_dump(include={"resources", "outputs", "assumptions"}),
                    separators=(",", ":"),
                ),
            ]
        lines = [
            *self._context_lines(
                intent=intent,
                component_name=component_name,
                allowed_inputs=allowed_inputs,
                environments=environments,
            ),
            *self._contract_lines(contracts),
            *self._negative_pattern_lines(negative_patterns),
            *previous_lines,
            "",
            "Compose the resources implementing the request. Rules:",
            "- Return ONLY JSON:",
            '  {"resources": [{"type": "...", "name": "...", "arguments": {"<arg>":',
            '  <value>}, "nested_blocks": {"<block name>": [{"<arg>": <value>}]}},',
            '  ...], "outputs": [{"name": "...", "description": "...",',
            '  "value": "..."}], "assumptions": ["..."]}',
            "- `arguments` values are native JSON following Terraform JSON configuration",
            "  semantics: numbers, booleans, lists, and objects render to HCL as-is;",
            "  strings are quoted string templates — write plain text directly",
            '  (e.g. "IaC Smith") and wrap Terraform expressions in interpolation',
            '  (e.g. "${var.environment}", "${aws_kms_key.this.arn}"). Never write a',
            "  bare reference as a string value — it would render as literal text.",
            "- Use only argument names from a type's allowed list; include every required",
            "  argument.",
            "- `nested_blocks` are structured JSON, never raw HCL: each key must be a",
            "  nested block name from the type's allowed list; each list entry is one",
            "  block instance whose arguments follow the same value semantics as",
            "  `arguments`. Repeatable blocks (e.g. `parameter`) are multiple entries",
            "  in the list. Never pass a block name as a top-level argument and never",
            "  emit a literal `blocks` argument.",
            "- Reference sibling resources as <type>.<name>.<attribute> — inside",
            "  ${...} in argument and nested-block strings; bare in output values.",
            f"- Reference only these input variables: {', '.join(allowed_inputs)}. Never",
            "  reference local., data., or module. values — they do not exist here.",
            "- You may add resource types beyond the contracts above only if you are",
            "  certain the provider defines them; they are validated the same way.",
            "- Resource and output names are lowercase snake_case identifiers.",
            "- Data sources do not exist here. When a resource needs a globally",
            "  unique name (e.g. an S3 bucket), omit its optional name argument so",
            "  the provider generates one, or derive it only from the allowed input",
            "  variables — never from account or caller identity.",
            "- `outputs` expose the identifiers consumers of this stack need; each",
            "  output `value` is a JSON string whose content is a plain Terraform",
            '  expression without ${...} interpolation — e.g. "value":',
            '  "aws_db_instance.this.arn". It must still be a quoted JSON string;',
            "  never emit an unquoted token as a JSON value.",
            "- Format the JSON with normal indentation and one key per line — do NOT",
            "  minify: balanced braces matter far more than token count, and the",
            "  output cap is generous. JSON-escape newlines (\\n) inside string",
            "  values — never emit a raw line break inside a string.",
        ]
        if findings:
            lines.extend(
                [
                    "",
                    "Your previous composition failed deterministic validation. Fix every",
                    "finding below without introducing new violations. Correction",
                    "patterns: an unsupported argument must be removed or moved to the",
                    "resource type that owns it — never renamed blindly; an unsupported",
                    "nested block must use a provider-declared block name via",
                    "`nested_blocks`; behaviour a type does not expose (e.g. network",
                    "placement) is enforced through the resources that own it, not by",
                    "inventing arguments:",
                    *(f"- {finding}" for finding in findings),
                ]
            )
        return self._invoke_json("\n".join(lines))

    def compose(
        self,
        *,
        intent: InfrastructureIntent,
        component_name: str,
        allowed_inputs: list[str],
        environments: list[str],
        provider_contracts: dict[str, TerraformContract],
        negative_patterns: list[str] | None = None,
        previous: ComposedComponent | None = None,
        runtime_findings: list[str] | None = None,
        registry_candidates: list[RegistryModuleContract] | None = None,
    ) -> ComposedComponent:
        known_resource_types = set(provider_contracts)
        if previous is not None and previous.registry_module is not None:
            # Repairing a module call: re-enter registry composition with the
            # runtime findings; its contract must be offered again for validation.
            candidates = list(registry_candidates or [])
            if previous.registry_module.source not in {c.source for c in candidates}:
                raise SpecCompositionError(
                    f"Cannot repair community module `{previous.registry_module.source}`: "
                    "its registry contract is no longer available."
                )
            module = self._compose_registry_module(
                intent=intent,
                component_name=component_name,
                allowed_inputs=allowed_inputs,
                environments=environments,
                candidates=candidates,
                negative_patterns=negative_patterns,
                previous=previous.registry_module,
                initial_findings=list(runtime_findings or []),
            )
            assert module is not None  # repair either converges or raises
            return self._registry_component(module)
        if previous is not None:
            # Repairing an existing composition: keep its type universe instead
            # of re-selecting, and seed the findings with the runtime errors.
            selected = sorted(
                {resource.type for resource in previous.resources} & set(provider_contracts)
            )
            selection_warnings: list[str] = []
            if selected:
                return self._compose_rounds(
                    intent=intent,
                    component_name=component_name,
                    allowed_inputs=allowed_inputs,
                    environments=environments,
                    provider_contracts=provider_contracts,
                    negative_patterns=negative_patterns,
                    selected=selected,
                    selection_warnings=selection_warnings,
                    previous=previous,
                    initial_findings=list(runtime_findings or []),
                )
        if registry_candidates:
            module = self._compose_registry_module(
                intent=intent,
                component_name=component_name,
                allowed_inputs=allowed_inputs,
                environments=environments,
                candidates=registry_candidates,
                negative_patterns=negative_patterns,
            )
            if module is not None:
                return self._registry_component(module)
        selected, selection_warnings = self._select_resource_types(
            intent=intent,
            component_name=component_name,
            allowed_inputs=allowed_inputs,
            environments=environments,
            known_resource_types=known_resource_types,
            negative_patterns=negative_patterns,
        )
        self._log(
            f"IaC Smith: composer selected {len(selected)} resource type(s): " + ", ".join(selected)
        )
        max_types = _int_env("IAC_SMITH_MAX_RESOURCE_TYPES", 12)
        if max_types > 0 and len(selected) > max_types:
            raise SpecCompositionError(_oversized_request_message(selected, max_types))
        return self._compose_rounds(
            intent=intent,
            component_name=component_name,
            allowed_inputs=allowed_inputs,
            environments=environments,
            provider_contracts=provider_contracts,
            negative_patterns=negative_patterns,
            selected=selected,
            selection_warnings=selection_warnings,
            previous=None,
            initial_findings=[],
        )

    def _compose_rounds(
        self,
        *,
        intent: InfrastructureIntent,
        component_name: str,
        allowed_inputs: list[str],
        environments: list[str],
        provider_contracts: dict[str, TerraformContract],
        negative_patterns: list[str] | None,
        selected: list[str],
        selection_warnings: list[str],
        previous: ComposedComponent | None,
        initial_findings: list[str],
    ) -> ComposedComponent:
        known_resource_types = set(provider_contracts)
        contracts = {name: provider_contracts[name] for name in selected}
        findings: list[str] = list(initial_findings)
        # Schema mistakes already rejected this run must not be rediscovered in a
        # later round, so every failed round's findings ride along as negative
        # patterns for the rest of the composition (bounded to keep the prompt sane).
        carried_negatives: list[str] = []
        for round_number in range(1, self.max_repair_rounds + 2):
            try:
                payload = self._compose_once(
                    intent=intent,
                    component_name=component_name,
                    allowed_inputs=allowed_inputs,
                    environments=environments,
                    contracts=contracts,
                    negative_patterns=[*(negative_patterns or []), *carried_negatives],
                    findings=findings,
                    previous=previous,
                )
            except ValueError as exc:
                # An unparseable response is a repairable violation, not a dead
                # end: re-stating the failure changes the prompt, so even at
                # temperature 0 the retry is not a verbatim replay.
                findings = [
                    f"Your previous response could not be parsed: {exc} Return exactly "
                    "one JSON object, formatted with normal indentation so every brace "
                    "balances — no prose, no markdown fences, and JSON-escaped newlines "
                    "(\\n) inside string values."
                ]
                self._log(
                    f"IaC Smith: composition round {round_number} response was not "
                    f"parseable JSON; repairing. ({exc})"
                )
                continue
            try:
                composed = ComposedComponent.model_validate(payload)
            except ValidationError as exc:
                findings = _shape_findings(exc)
                self._log(
                    f"IaC Smith: composition round {round_number} response shape was "
                    f"invalid ({len(exc.errors())} field error(s)); repairing."
                )
                continue
            composed = normalize_composed_blocks(composed, provider_contracts)
            findings = validate_composed_component(
                composed,
                provider_contracts=provider_contracts,
                known_resource_types=known_resource_types,
                allowed_inputs=allowed_inputs,
                component_name=component_name,
            )
            if not findings:
                self._log(
                    f"IaC Smith: composed {len(composed.resources)} provider resource(s) "
                    f"for `{component_name}`."
                )
                composed.assumptions.extend(selection_warnings)
                composed_types = {resource.type for resource in composed.resources}
                return composed.model_copy(
                    update={
                        "block_names": {
                            rtype: provider_contracts[rtype].block_names
                            for rtype in sorted(composed_types)
                            if rtype in provider_contracts and provider_contracts[rtype].block_names
                        }
                    }
                )
            carried_negatives.extend(
                finding for finding in findings if finding not in carried_negatives
            )
            del carried_negatives[24:]
            # Later rounds edit the newest composition rather than the original.
            previous = composed
            self._log(
                f"IaC Smith: composition round {round_number} failed deterministic "
                f"validation with {len(findings)} finding(s); repairing."
            )
        raise SpecCompositionError(
            "Composed resources failed deterministic schema validation after "
            f"{self.max_repair_rounds + 1} attempts: " + "; ".join(findings)
        )
