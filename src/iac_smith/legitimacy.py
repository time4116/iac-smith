"""Deterministic PR legitimacy gates.

A PR that claims infrastructure it does not contain is worse than no PR: the
demo-infra run for issue #59 opened a PR whose only real resources were the
backend bootstrap while the body described an Aurora data platform. These gates
derive a resource inventory from the *rendered* Terraform and block PR creation
when the output cannot satisfy the request — structure-only placeholders,
backend-only inventories, failure banners left in generated files, or required
existing-stack dependencies that were never wired.

Everything here is generic: inventories are parsed from ``resource`` blocks,
"backend bootstrap" is a path classification (``bootstrap/``), and the
requested-class check tokenizes the parsed intent instead of hardcoding any
service knowledge.
"""

import os
import re
from collections.abc import Mapping

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent

_RESOURCE_RE = re.compile(r'^\s*resource\s+"([^"\s]+)"\s+"([^"\s]+)"', re.MULTILINE)

# Phrases that only ever appear when generation degraded. Matched
# case-insensitively against generated files and the PR body.
FAILURE_BANNERS: tuple[str, ...] = (
    "no provider resources were selected",
    "structure-only pr",
    "response was truncated",
    "spec composition failed",
    "composition failed",
)

_BOOTSTRAP_PREFIX = "bootstrap/"


def allow_structure_only(env: Mapping[str, str] | None = None) -> bool:
    """Explicit opt-in for placeholder PRs; disabled by default."""
    source = env if env is not None else os.environ
    return source.get("IAC_SMITH_ALLOW_STRUCTURE_ONLY") == "1"


def resource_inventory(files: Mapping[str, str]) -> dict[str, list[tuple[str, str]]]:
    """``resource "type" "name"`` addresses per rendered ``.tf`` file."""
    inventory: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(files):
        if not path.endswith(".tf"):
            continue
        found = _RESOURCE_RE.findall(files[path])
        if found:
            inventory[path] = found
    return inventory


def workload_resource_addresses(files: Mapping[str, str]) -> list[str]:
    """Provider resource addresses outside the backend bootstrap tree."""
    return [
        f"{rtype}.{rname}"
        for path, records in resource_inventory(files).items()
        if not path.startswith(_BOOTSTRAP_PREFIX)
        for rtype, rname in records
    ]


def backend_resource_addresses(files: Mapping[str, str]) -> list[str]:
    return [
        f"{rtype}.{rname}"
        for path, records in resource_inventory(files).items()
        if path.startswith(_BOOTSTRAP_PREFIX)
        for rtype, rname in records
    ]


def find_failure_banner(text: str) -> str | None:
    lowered = text.lower()
    for banner in FAILURE_BANNERS:
        if banner in lowered:
            return banner
    return None


def _intent_tokens(intent: InfrastructureIntent) -> list[str]:
    """Class tokens derived from the parsed request — never a hardcoded service list."""
    raw = " ".join([intent.resource_type or "", *intent.features]).lower()
    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", raw)
        if len(token) >= 3 or any(char.isdigit() for char in token)
    ]
    return list(dict.fromkeys(tokens))


def _planned_module_dirs(change_plan: ChangePlan) -> list[str]:
    return sorted(
        {
            "/".join(path.split("/")[:2])
            for path in change_plan.files_to_generate
            if path.startswith("modules/")
        }
    )


def check_pr_legitimacy(
    *,
    generated_files: Mapping[str, str],
    change_plan: ChangePlan,
    intent: InfrastructureIntent,
    pr_body: str | None = None,
    structure_only: bool = False,
    allow_structure_only: bool = False,
) -> list[str]:
    """Reasons this output must not become a PR; empty means legitimate.

    With ``allow_structure_only`` (the ``IAC_SMITH_ALLOW_STRUCTURE_ONLY=1``
    opt-in) the placeholder-related gates are skipped — the operator asked for
    skeleton PRs — but a required existing-stack dependency that was silently
    dropped still blocks: opting into placeholders never opts into wiring lies.
    """
    errors: list[str] = []
    planned_modules = _planned_module_dirs(change_plan)
    workload = workload_resource_addresses(generated_files)

    if planned_modules and not allow_structure_only:
        if structure_only:
            errors.append(
                "Structure-only output: no provider resources were composed for the "
                f"planned workload modules ({', '.join(planned_modules)}). Set "
                "IAC_SMITH_ALLOW_STRUCTURE_ONLY=1 to explicitly allow placeholder PRs."
            )
        if not workload:
            backend = backend_resource_addresses(generated_files)
            if backend:
                errors.append(
                    "Only backend bootstrap resources were generated "
                    f"({', '.join(backend)}); the planned workload modules "
                    f"({', '.join(planned_modules)}) contain no provider resources, "
                    "so this PR would not implement the requested infrastructure."
                )
            else:
                errors.append(
                    f"Planned workload modules ({', '.join(planned_modules)}) contain "
                    "no provider resources."
                )
        else:
            tokens = _intent_tokens(intent)
            module_text = "\n".join(
                content for path, content in generated_files.items() if path.startswith("modules/")
            ).lower()
            if tokens and not any(token in module_text for token in tokens):
                errors.append(
                    "None of the requested infrastructure classes appear in the "
                    f"generated workload modules. Request tokens: {', '.join(tokens)}. "
                    f"Generated resources: {', '.join(workload)}."
                )

    if not allow_structure_only:
        for path in sorted(generated_files):
            banner = find_failure_banner(generated_files[path])
            if banner:
                errors.append(f"Generated file `{path}` contains failure banner `{banner}`.")
        if pr_body:
            banner = find_failure_banner(pr_body)
            if banner:
                errors.append(f"The PR body contains failure banner `{banner}`.")

    for producer in intent.depends_on_existing:
        normalized = re.sub(r"[^a-z0-9]+", "-", producer.lower()).strip("-")
        if not normalized:
            continue
        pattern = re.compile(rf'dependency\s+"{re.escape(normalized)}"')
        wired = any(
            pattern.search(content)
            for path, content in generated_files.items()
            if path.endswith("terragrunt.hcl")
        )
        if not wired:
            errors.append(
                f"The issue requires consuming the existing `{producer}` stack, but no "
                f'`dependency "{normalized}"` wiring exists in the generated Terragrunt '
                "files — the dependency must not be silently omitted."
            )
    return errors
