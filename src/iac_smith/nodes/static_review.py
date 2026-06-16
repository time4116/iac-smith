import re

from iac_smith.models.validation import ValidationResult, ValidationStatus

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY"),
    re.compile(r"aws_(access_key_id|secret_access_key)\s*=", re.IGNORECASE),
    re.compile(r"(password|token|secret)\s*=\s*[\"'][^\"']{6,}[\"']", re.IGNORECASE),
]

# Matches both old-style bracket form and newer aws_vpc_security_group_ingress_rule form.
_CIDR_BLOCK_V4 = re.compile(
    r"cidr_blocks\s*=\s*\[[^\]]*0\.0\.0\.0/0"
    r"|cidr_ipv4\s*=\s*[\"']0\.0\.0\.0/0[\"']",
    re.IGNORECASE | re.DOTALL,
)
_CIDR_BLOCK_V6 = re.compile(
    r"ipv6_cidr_blocks\s*=\s*\[[^\]]*::/0"
    r"|cidr_ipv6\s*=\s*[\"']::/0[\"']",
    re.IGNORECASE | re.DOTALL,
)
# Ports where public open ingress is unambiguously dangerous.
_DANGEROUS_PORTS = {22, 3389, 5432, 3306, 1433, 6379, 27017}
_PORT_RE = re.compile(r"(?:from_port|to_port)\s*=\s*(\d+)")
_MODULE_DECL_RE = re.compile(r'\bmodule\s+"([^"]+)"')
_MODULE_REF_RE = re.compile(r"\bmodule\.([A-Za-z0-9_-]+)\.")


def _has_main_or_master_branch_filter(content: str) -> bool:
    lines = content.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)branches\s*:\s*(.*)$", line)
        if not match:
            continue

        branch_indent = len(match.group(1))
        inline_value = match.group(2)
        if re.search(r"\b(main|master)\b", inline_value):
            return True

        for child in lines[index + 1 :]:
            if not child.strip():
                continue
            child_indent = len(child) - len(child.lstrip())
            if child_indent <= branch_indent:
                break
            if re.match(r"^\s*-\s*(main|master)\s*(?:#.*)?$", child):
                return True
    return False


def _apply_workflow_errors(path: str, content: str) -> list[str]:
    if path != ".github/workflows/terraform-apply.yml":
        return []

    errors = []
    if re.search(r"^\s*pull_request\s*:", content, re.MULTILINE):
        errors.append(
            f"Terraform apply workflow `{path}` must not run on pull requests or feature branches."
        )
    if not _has_main_or_master_branch_filter(content):
        errors.append(
            f"Terraform apply workflow `{path}` push trigger must be limited to main or master."
        )
    return errors


def _module_root(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "modules" and path.endswith(".tf"):
        return "/".join(parts[:2])
    return None


def _find_undeclared_module_references(generated_files: dict[str, str]) -> list[str]:
    declared_by_root: dict[str, set[str]] = {}
    referenced_by_root: dict[str, set[str]] = {}

    for path, content in generated_files.items():
        root = _module_root(path)
        if not root:
            continue
        declared_by_root.setdefault(root, set()).update(_MODULE_DECL_RE.findall(content))
        referenced_by_root.setdefault(root, set()).update(_MODULE_REF_RE.findall(content))

    errors = []
    for root, references in sorted(referenced_by_root.items()):
        declared = declared_by_root.get(root, set())
        for module_name in sorted(references - declared):
            errors.append(
                f"Generated module `{root}` references `module.{module_name}` "
                f'but does not declare `module "{module_name}"`.'
            )
    return errors


_VAR_DECL_RE = re.compile(r'\bvariable\s+"([^"]+)"')
_OUTPUT_DECL_RE = re.compile(r'\boutput\s+"([^"]+)"')
_REQUIRED_PROVIDERS_RE = re.compile(r"required_providers\s*{")


def _suggest_keep_file(locations: list[str], preferred: str) -> str:
    """Suggest which file to keep based on Terraform conventions.

    Compares the basename or suffix since locations are full module paths
    like ``modules/ecs-fargate/variables.tf`` and preferred is a short
    name like ``variables.tf``.
    """
    keep = locations[0]
    for loc in locations:
        if loc.endswith(f"/{preferred}") or loc == preferred:
            keep = loc
            break
    remove = [loc for loc in locations if loc != keep]
    if remove:
        return f"Remove from {remove[0]}, keep in {keep}."
    return "Keep one declaration only."


def _find_cross_file_duplicates(generated_files: dict[str, str]) -> list[str]:
    """Detect variable/output/provider declarations duplicated across files in the same module.

    Terraform modules must have unique variable names, output names, and at most one
    required_providers block. If a variable is declared in both ``main.tf`` and
    ``variables.tf`` of the same module, that will fail ``terraform init`` with
    "Duplicate variable declaration". Same for outputs and required_providers.
    """
    var_by_root: dict[str, dict[str, list[str]]] = {}
    output_by_root: dict[str, dict[str, list[str]]] = {}
    prov_by_root: dict[str, list[str]] = {}
    errors = []

    for path, content in generated_files.items():
        root = _module_root(path)
        if not root:
            continue

        for m in _VAR_DECL_RE.finditer(content):
            name = m.group(1)
            var_by_root.setdefault(root, {}).setdefault(name, []).append(path)

        for m in _OUTPUT_DECL_RE.finditer(content):
            name = m.group(1)
            output_by_root.setdefault(root, {}).setdefault(name, []).append(path)

        if _REQUIRED_PROVIDERS_RE.search(content):
            prov_by_root.setdefault(root, []).append(path)

    for root, vars_by_name in sorted(var_by_root.items()):
        for name, locations in sorted(vars_by_name.items()):
            if len(locations) > 1:
                hint = _suggest_keep_file(locations, preferred="variables.tf")
                errors.append(
                    f'Variable "{name}" declared in multiple files of module '
                    f"`{root}`: {', '.join(locations)}. {hint}"
                )

    for root, outputs_by_name in sorted(output_by_root.items()):
        for name, locations in sorted(outputs_by_name.items()):
            if len(locations) > 1:
                hint = _suggest_keep_file(locations, preferred="outputs.tf")
                errors.append(
                    f'Output "{name}" declared in multiple files of module '
                    f"`{root}`: {', '.join(locations)}. {hint}"
                )

    for root, locations in sorted(prov_by_root.items()):
        if len(locations) > 1:
            hint = _suggest_keep_file(locations, preferred="versions.tf")
            errors.append(
                f"required_providers block found in multiple files of module "
                f"`{root}`: {', '.join(locations)}. {hint}"
            )

    return errors


def _contains_dangerous_public_ingress(content: str) -> bool:
    has_public_cidr = _CIDR_BLOCK_V4.search(content) or _CIDR_BLOCK_V6.search(content)
    if not has_public_cidr:
        return False
    ports = {int(m.group(1)) for m in _PORT_RE.finditer(content)}
    return bool(ports & _DANGEROUS_PORTS)


def static_review_generated_files(generated_files: dict[str, str]) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []

    for path, content in generated_files.items():
        errors.extend(_apply_workflow_errors(path, content))

        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                errors.append(f"Potential hardcoded secret detected in `{path}`.")
                break

        if path.endswith("terragrunt.hcl") and "key" in content:
            has_remote_state = "remote_state" in content or "terraform.tfstate" in content
            if has_remote_state and "path_relative_to_include()" not in content:
                errors.append(
                    f"Terragrunt state key in `{path}` must use path_relative_to_include()."
                )

        if _contains_dangerous_public_ingress(content):
            warnings.append(f"Dangerous public ingress detected in `{path}` for PR review.")

        if (
            path.startswith("modules/")
            and path.endswith("README.md")
            and ("<!-- BEGIN_TF_DOCS -->" not in content or "<!-- END_TF_DOCS -->" not in content)
        ):
            warnings.append(f"Module README `{path}` is missing terraform-docs markers.")

    errors.extend(_find_undeclared_module_references(generated_files))
    errors.extend(_find_cross_file_duplicates(generated_files))

    if errors:
        status = ValidationStatus.FAILED
    elif warnings:
        status = ValidationStatus.PARTIAL
    else:
        status = ValidationStatus.PASSED
        checks.append("Static security review passed.")

    return ValidationResult(status=status, checks=checks, warnings=warnings, errors=errors)
