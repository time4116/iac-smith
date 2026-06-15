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
            errors.append(f"Dangerous public ingress detected in `{path}`.")

        if (
            path.startswith("modules/")
            and path.endswith("README.md")
            and ("<!-- BEGIN_TF_DOCS -->" not in content or "<!-- END_TF_DOCS -->" not in content)
        ):
            warnings.append(f"Module README `{path}` is missing terraform-docs markers.")

    if errors:
        status = ValidationStatus.FAILED
    elif warnings:
        status = ValidationStatus.PARTIAL
    else:
        status = ValidationStatus.PASSED
        checks.append("Static security review passed.")

    return ValidationResult(status=status, checks=checks, warnings=warnings, errors=errors)
