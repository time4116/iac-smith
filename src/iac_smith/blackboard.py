import re
from typing import Literal

from pydantic import BaseModel, Field

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
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


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _requires_source_artifact(intent: InfrastructureIntent, change_plan: ChangePlan) -> bool:
    haystack = " ".join(
        [intent.raw_request, intent.resource_type, *intent.features, *change_plan.files_to_generate]
    ).lower()
    return "src/" in haystack or any(
        token in haystack for token in ["dotnet", ".net", "web app", "application source"]
    )


def _contract_candidates(intent: InfrastructureIntent) -> list[str]:
    haystack = " ".join([intent.raw_request, intent.resource_type, *intent.features]).lower()
    candidates: list[str] = []
    if "elastic beanstalk" in haystack or "beanstalk" in haystack:
        candidates.extend(
            [
                "aws_elastic_beanstalk_application",
                "aws_elastic_beanstalk_application_version",
                "aws_elastic_beanstalk_environment",
            ]
        )
    if "https" in haystack or "cert" in haystack or "certificate" in haystack:
        candidates.append("aws_acm_certificate")
    if "route53" in haystack or "dns" in haystack:
        candidates.append("aws_route53_record")
    return _dedupe(candidates)


def build_blackboard(
    *,
    intent: InfrastructureIntent,
    change_plan: ChangePlan,
    repo_patterns: RepoPatterns | None,
    resolver: ContractResolver | None = None,
) -> RunBlackboard:
    selected_contracts = _contract_candidates(intent)
    required_artifacts = ["src/"] if _requires_source_artifact(intent, change_plan) else []
    resolver = resolver or ContractResolver()
    return RunBlackboard(
        repo_patterns=repo_patterns or RepoPatterns(),
        required_artifacts=required_artifacts,
        selected_contracts=selected_contracts,
        contract_docs=resolver.resolve(selected_contracts),
        implementation_decisions={
            "contract_selection": "frozen-before-parallel-generation"
            if selected_contracts
            else "no-contracts-selected"
        },
    )


def build_blackboard_prompt_section(blackboard: RunBlackboard | None) -> str:
    if not blackboard:
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
_ARGUMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)
_UNSUPPORTED_ARG_RE = re.compile(
    r'resource\s+"(?P<scope>[^"]+)"[\s\S]*?'
    r'An argument named "(?P<arg>[^"]+)" is not expected here\.',
    re.MULTILINE,
)
_INVALID_RESOURCE_RE = re.compile(
    r'resource\s+"(?P<scope>[^"]+)"[\s\S]*?does not support resource type\s+"(?P=scope)"',
    re.MULTILINE,
)


def validate_generated_contracts(
    generated_files: dict[str, str], blackboard: RunBlackboard | None
) -> ValidationResult:
    if not blackboard or not blackboard.contract_docs:
        return ValidationResult(
            status=ValidationStatus.PASSED, checks=["No contract docs available."]
        )
    errors: list[str] = []
    for path, content in generated_files.items():
        for match in _RESOURCE_BLOCK_RE.finditer(content):
            resource_type = match.group("type")
            contract = blackboard.contract_docs.get(resource_type)
            if (
                not contract
                or contract.kind != "provider_resource"
                or not contract.allowed_arguments
            ):
                continue
            allowed = set(contract.allowed_arguments)
            for argument in sorted(set(_ARGUMENT_RE.findall(match.group("body")))):
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


def normalize_validation_findings(errors: list[str]) -> list[ValidationFinding]:
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
    return findings
