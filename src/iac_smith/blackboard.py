import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.validation import ValidationResult, ValidationStatus

ContractKind = Literal["provider_resource", "registry_module"]


class TerraformContract(BaseModel):
    kind: ContractKind
    name: str
    version: str | None = None
    allowed_arguments: list[str] = Field(default_factory=list)
    required_arguments: list[str] = Field(default_factory=list)
    source: str


class ValidationFinding(BaseModel):
    scope: str
    finding: str
    source: str
    severity: Literal["hard", "warning"] = "hard"
    negative_pattern: str | None = None


class RunBlackboard(BaseModel):
    """Typed shared workspace for one IaC Smith run.

    This is intentionally scoped to a single issue/run. It coordinates parallel
    generation and repair workers without becoming a service-specific renderer or
    unversioned long-term memory.
    """

    repo_patterns: RepoPatterns = Field(default_factory=RepoPatterns)
    required_artifacts: list[str] = Field(default_factory=list)
    selected_contracts: list[str] = Field(default_factory=list)
    contract_docs: dict[str, TerraformContract] = Field(default_factory=dict)
    validation_findings: list[ValidationFinding] = Field(default_factory=list)
    negative_patterns: list[str] = Field(default_factory=list)
    implementation_decisions: dict[str, str] = Field(default_factory=dict)

    def with_findings(self, findings: list[ValidationFinding]) -> "RunBlackboard":
        if not findings:
            return self
        merged = self.model_copy(deep=True)
        seen_findings = {f.model_dump_json() for f in merged.validation_findings}
        seen_patterns = set(merged.negative_patterns)
        for finding in findings:
            key = finding.model_dump_json()
            if key not in seen_findings:
                merged.validation_findings.append(finding)
                seen_findings.add(key)
            if finding.negative_pattern and finding.negative_pattern not in seen_patterns:
                merged.negative_patterns.append(finding.negative_pattern)
                seen_patterns.add(finding.negative_pattern)
        return merged


class ContractResolver:
    """Resolve docs/schema contracts chosen by the model or planner.

    The resolver is generic: callers pass candidate provider resource/module names,
    and it returns authoritative contracts when available. Tests can inject fixture
    contracts; production can grow this into provider-schema or Terraform Registry
    lookups without changing generation orchestration.
    """

    def __init__(
        self,
        *,
        provider_contracts: dict[str, TerraformContract] | None = None,
        module_contracts: dict[str, TerraformContract] | None = None,
    ) -> None:
        self.provider_contracts = provider_contracts or {}
        self.module_contracts = module_contracts or {}

    def resolve(self, candidates: list[str]) -> dict[str, TerraformContract]:
        resolved: dict[str, TerraformContract] = {}
        for candidate in candidates:
            contract = self.provider_contracts.get(candidate) or self.module_contracts.get(
                candidate
            )
            if contract:
                resolved[candidate] = contract
        return resolved


def build_blackboard(*, repo_patterns: RepoPatterns | None) -> RunBlackboard:
    """Start a run blackboard.

    Deliberately makes no service- or language-specific assumptions: contracts are
    resolved later from the resource types that actually appear in the generated
    Terraform (see ``resolve_contracts_for_files``), and negative patterns are
    learned from real validation failures. Nothing is keyword-matched up front.
    """
    return RunBlackboard(repo_patterns=repo_patterns or RepoPatterns())


def build_blackboard_prompt_section(blackboard: RunBlackboard | None) -> str:
    if not blackboard or not (
        blackboard.required_artifacts
        or blackboard.selected_contracts
        or blackboard.contract_docs
        or blackboard.negative_patterns
    ):
        # Nothing learned/resolved yet (e.g. first generation pass) — inject no
        # boilerplate.
        return ""
    lines = [
        "",
        "Shared run blackboard (authoritative for this run):",
        "- Treat selected contracts and negative patterns as run-level coordination state.",
        "- Do not choose incompatible Terraform strategies in individual file workers.",
    ]
    if blackboard.required_artifacts:
        lines.append("- Required artifacts: " + ", ".join(blackboard.required_artifacts))
    if blackboard.selected_contracts:
        lines.append("- Selected Terraform contracts: " + ", ".join(blackboard.selected_contracts))
    for name, contract in sorted(blackboard.contract_docs.items()):
        if contract.allowed_arguments:
            lines.append(f"- {name} allowed arguments: " + ", ".join(contract.allowed_arguments))
        if contract.required_arguments:
            lines.append(f"- {name} required arguments: " + ", ".join(contract.required_arguments))
        lines.append(f"- {name} contract source: {contract.source}")
    if blackboard.negative_patterns:
        lines.append("- Known invalid patterns from this run:")
        lines.extend(f"  - {pattern}" for pattern in blackboard.negative_patterns)
    return "\n".join(lines) + "\n"


_RESOURCE_BLOCK_RE = re.compile(
    r'resource\s+"(?P<type>[^"]+)"\s+"[^"]+"\s*{(?P<body>.*?)^}',
    re.MULTILINE | re.DOTALL,
)
_ARGUMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _top_level_arguments(body: str) -> set[str]:
    """Return only the depth-0 argument names of a resource block.

    Brace depth is tracked so arguments inside nested blocks (``setting {}``,
    ``tag {}``, ``ingress {}``, ...) are NOT treated as top-level arguments — that
    would otherwise flag every nested key as an unsupported argument.
    """
    args: set[str] = set()
    depth = 0
    for line in body.splitlines():
        if depth == 0:
            match = _ARGUMENT_RE.match(line.strip())
            if match:
                args.add(match.group(1))
        depth += line.count("{") - line.count("}")
        depth = max(depth, 0)
    return args


def extract_resource_types(contents: Iterable[str]) -> list[str]:
    """Return the distinct ``resource "<type>"`` names across the given file bodies.

    Order-preserving and deduplicated. Generic: the candidate set is whatever the
    model actually produced, never a curated keyword list.
    """
    resource_types: list[str] = []
    seen: set[str] = set()
    for content in contents:
        for match in _RESOURCE_BLOCK_RE.finditer(content):
            rtype = match.group("type")
            if rtype not in seen:
                seen.add(rtype)
                resource_types.append(rtype)
    return resource_types


def resolve_contracts_for_files(
    generated_files: dict[str, str], resolver: ContractResolver
) -> dict[str, TerraformContract]:
    """Resolve contracts for the resource types that actually appear in the output.

    Fully generic: the candidate set is whatever ``resource "<type>"`` blocks the
    model produced, not a curated keyword list, so any provider resource a real
    resolver knows about is validated.
    """
    return resolver.resolve(extract_resource_types(generated_files.values()))


def contracts_from_provider_schema(
    schema: dict,
    *,
    resource_types: set[str] | None = None,
    source: str = "terraform providers schema -json",
) -> dict[str, TerraformContract]:
    """Build resource contracts from ``terraform providers schema -json`` output.

    Generic across providers: every resource type the installed providers expose
    becomes a contract whose allowed arguments are the schema's top-level
    attribute and nested-block names, and whose required arguments are the
    attributes the schema marks required. No provider- or resource-specific
    knowledge is encoded — this is the authoritative resolver behind the otherwise
    inert ``ContractResolver`` hook.

    When ``resource_types`` is given, only those types are built — callers scope the
    harvest to the resources actually generated so the resolved set (and any prompt
    injection) stays small instead of carrying a provider's full catalog.
    """
    contracts: dict[str, TerraformContract] = {}
    provider_schemas = schema.get("provider_schemas") or {}
    for provider_uri, provider_schema in provider_schemas.items():
        provider_name = provider_uri.rsplit("/", 2)[-2:]
        provider_label = "/".join(provider_name) if len(provider_name) == 2 else provider_uri
        resource_schemas = (provider_schema or {}).get("resource_schemas") or {}
        for resource_type, resource_schema in resource_schemas.items():
            if resource_types is not None and resource_type not in resource_types:
                continue
            block = (resource_schema or {}).get("block") or {}
            attributes = block.get("attributes") or {}
            block_types = block.get("block_types") or {}
            allowed = sorted({*attributes.keys(), *block_types.keys()})
            required = sorted(
                name
                for name, spec in attributes.items()
                if isinstance(spec, dict) and spec.get("required")
            )
            contracts[resource_type] = TerraformContract(
                kind="provider_resource",
                name=resource_type,
                allowed_arguments=allowed,
                required_arguments=required,
                source=f"{source} ({provider_label})",
            )
    return contracts


_UNSUPPORTED_ARG_RE = re.compile(
    r'resource\s+"(?P<scope>[^"]+)"[\s\S]*?'
    r'An argument named "(?P<arg>[^"]+)" is not expected here\.',
    re.MULTILINE,
)
_INVALID_RESOURCE_RE = re.compile(
    r'resource\s+"(?P<scope>[^"]+)"[\s\S]*?does not support resource type\s+"(?P=scope)"',
    re.MULTILINE,
)
_UNSUPPORTED_BLOCK_RE = re.compile(
    r'resource\s+"(?P<scope>[^"]+)"[\s\S]*?'
    r'Blocks of type "(?P<block>[^"]+)" are not expected here\.',
    re.MULTILINE,
)
# Plan-time provider value constraints — these only surface at `terraform plan`,
# not `terraform validate`, so they reach the runtime repair loop as raw text.
_VALUE_REGEX_RE = re.compile(
    r"expected (?:value of )?(?P<attr>[A-Za-z0-9_.\[\]-]+) to match regular expression "
    r'"(?P<regex>[^"]*)", got (?P<got>.+)'
)
_VALUE_RANGE_RE = re.compile(
    r"expected (?P<attr>[A-Za-z0-9_.\[\]-]+) to be in the range "
    r"\((?P<range>[^)]*)\), got (?P<got>.+)"
)
_MISSING_REQUIRED_VAR_RE = re.compile(
    r'The root module input variable "(?P<var>[^"]+)" is not set, and has no default value'
)
# A value rejected by a Terraform custom `validation {}` block on a variable —
# the model's *own* declared constraint, surfaced only at plan time (e.g.
# `var.health_check_interval is 30` / "must be between 1 and 20 seconds").
_INVALID_VAR_VALUE_RE = re.compile(
    r"var\.(?P<var>[A-Za-z0-9_]+) is (?P<got>[^\n]+)"
    r"(?P<message>[\s\S]*?)This was checked by the validation rule"
)


def validate_generated_contracts(
    generated_files: dict[str, str], contract_docs: dict[str, TerraformContract]
) -> ValidationResult:
    """Check generated resources against resolved contract docs.

    ``contract_docs`` is the dynamically resolved set (see
    ``resolve_contracts_for_files``). When empty — the default in production until
    a schema/registry resolver is wired — this is a no-op pass.
    """
    if not contract_docs:
        return ValidationResult(
            status=ValidationStatus.PASSED, checks=["No contract docs available."]
        )
    errors: list[str] = []
    for path, content in generated_files.items():
        for match in _RESOURCE_BLOCK_RE.finditer(content):
            resource_type = match.group("type")
            contract = contract_docs.get(resource_type)
            if (
                not contract
                or contract.kind != "provider_resource"
                or not contract.allowed_arguments
            ):
                continue
            allowed = set(contract.allowed_arguments)
            for argument in sorted(_top_level_arguments(match.group("body"))):
                if argument not in allowed:
                    errors.append(
                        f"`{path}` uses unsupported argument `{argument}` on `{resource_type}`. "
                        f"Allowed arguments from {contract.source}: "
                        f"{', '.join(contract.allowed_arguments)}."
                    )
    if errors:
        return ValidationResult(status=ValidationStatus.FAILED, errors=errors)
    return ValidationResult(
        status=ValidationStatus.PASSED, checks=["Generated Terraform matches resolved contracts."]
    )


def _allowed_arguments_hint(scope: str, contract_docs: dict[str, TerraformContract] | None) -> str:
    """Couple a schema error to its fix.

    When the authoritative contract for the offending resource was harvested, the
    negative pattern carries the resource's real allowed arguments/blocks inline,
    so the repair model replaces the rejected name with a valid one instead of
    guessing again. Returns an empty string when no contract is available — the
    negative pattern then degrades to the bare "do not use X" form.
    """
    if not contract_docs:
        return ""
    contract = contract_docs.get(scope)
    if not contract or not contract.allowed_arguments:
        return ""
    return (
        f" The valid arguments and blocks for `{scope}` are: "
        f"{', '.join(contract.allowed_arguments)} — replace it with the matching one."
    )


def normalize_validation_findings(
    errors: list[str], contract_docs: dict[str, TerraformContract] | None = None
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    seen: set[tuple[str, str]] = set()
    for error in errors:
        for match in _UNSUPPORTED_ARG_RE.finditer(error):
            scope = match.group("scope")
            arg = match.group("arg")
            key = (scope, arg)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                ValidationFinding(
                    scope=scope,
                    finding=f"Unsupported argument {arg}",
                    source="terraform validation",
                    negative_pattern=(
                        f"Do not use argument `{arg}` with `{scope}`; it is not in that contract."
                        + _allowed_arguments_hint(scope, contract_docs)
                    ),
                )
            )
        for match in _UNSUPPORTED_BLOCK_RE.finditer(error):
            scope = match.group("scope")
            block = match.group("block")
            key = (scope, f"block:{block}")
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                ValidationFinding(
                    scope=scope,
                    finding=f"Unsupported block {block}",
                    source="terraform validation",
                    negative_pattern=(
                        f"Do not use a `{block}` block in `{scope}`; it is not in that contract."
                        + _allowed_arguments_hint(scope, contract_docs)
                    ),
                )
            )
        for match in _INVALID_RESOURCE_RE.finditer(error):
            scope = match.group("scope")
            key = (scope, "resource_type")
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                ValidationFinding(
                    scope=scope,
                    finding="Unsupported resource type",
                    source="terraform validation",
                    negative_pattern=f"Do not use unsupported Terraform resource type `{scope}`.",
                )
            )
        for match in _VALUE_REGEX_RE.finditer(error):
            attr = match.group("attr")
            got = match.group("got").strip()
            key = (attr, f"regex:{got}")
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                ValidationFinding(
                    scope=attr,
                    finding=f"Value {got} rejected by provider pattern",
                    source="terraform plan",
                    negative_pattern=(
                        f"`{attr}` must match the provider pattern `{match.group('regex')}`; "
                        f"the value `{got}` is invalid — pick a conforming value (or generate a "
                        f"resource/input that produces one) rather than reusing it."
                    ),
                )
            )
        for match in _VALUE_RANGE_RE.finditer(error):
            attr = match.group("attr")
            got = match.group("got").strip()
            key = (attr, f"range:{got}")
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                ValidationFinding(
                    scope=attr,
                    finding=f"Value {got} out of range",
                    source="terraform plan",
                    negative_pattern=(
                        f"`{attr}` must be within ({match.group('range')}); "
                        f"the value `{got}` is out of range."
                    ),
                )
            )
        for match in _MISSING_REQUIRED_VAR_RE.finditer(error):
            var = match.group("var")
            key = ("variable", var)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                ValidationFinding(
                    scope=var,
                    finding=f"Required variable {var} has no value",
                    source="terraform plan",
                    negative_pattern=(
                        f"Required variable `{var}` has no value and no default; pass it in the "
                        f"stack `inputs` block or give it a safe default — never leave it unset."
                    ),
                )
            )
        for match in _INVALID_VAR_VALUE_RE.finditer(error):
            var = match.group("var")
            got = match.group("got").strip()
            rule = " ".join(match.group("message").split()).strip(" .")
            key = (var, f"validation:{got}")
            if key in seen:
                continue
            seen.add(key)
            constraint = f" ({rule})" if rule else ""
            findings.append(
                ValidationFinding(
                    scope=var,
                    finding=f"Value {got} fails variable validation",
                    source="terraform plan",
                    negative_pattern=(
                        f"`{var}` value `{got}` violates its own `validation` rule{constraint}; "
                        f"set a value that satisfies the declared constraint (or correct the rule "
                        f"if the rule itself is wrong) — do not keep the rejected value."
                    ),
                )
            )
    return findings
