import re
from pathlib import Path

from iac_smith.models.repo_patterns import RepoPatterns

MODULE_SOURCE_RE = re.compile(r"source\s*=\s*\"([^\"]+)\"")
KNOWN_ENV_NAMES = {
    "dev",
    "development",
    "test",
    "stage",
    "staging",
    "non-prod",
    "nonprod",
    "prod",
    "production",
}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def _discover_environments(root: Path) -> list[str]:
    live = root / "live"
    if not live.exists():
        return []
    envs = []
    for child in live.iterdir():
        is_env_dir = child.is_dir() and (
            (child / "terragrunt.hcl").exists() or child.name in KNOWN_ENV_NAMES
        )
        if is_env_dir:
            envs.append(child.name)
    return sorted(set(envs))


def _discover_module_sources(root: Path) -> list[str]:
    sources: set[str] = set()
    for path in root.rglob("*.tf"):
        text = _read_text(path)
        sources.update(MODULE_SOURCE_RE.findall(text))
    for path in root.rglob("terragrunt.hcl"):
        text = _read_text(path)
        sources.update(MODULE_SOURCE_RE.findall(text))
    return sorted(sources)


def _discover_existing_stack_paths(root: Path) -> list[str]:
    paths = []
    live = root / "live"
    if not live.exists():
        return paths
    for path in live.rglob("terragrunt.hcl"):
        rel = path.relative_to(root).as_posix()
        if rel != "live/terragrunt.hcl" and path.parent != live:
            paths.append(path.parent.relative_to(root).as_posix())
    return sorted(set(paths))


def scan_repo_patterns(root: str | Path) -> RepoPatterns:
    repo_root = Path(root)
    terragrunt_files = list(repo_root.rglob("terragrunt.hcl"))
    terraform_files = list(repo_root.rglob("*.tf"))
    environments = _discover_environments(repo_root)
    module_sources = _discover_module_sources(repo_root)
    all_hcl_text = "\n".join(_read_text(path) for path in terragrunt_files)

    uses_terragrunt = bool(terragrunt_files)
    uses_terraform = bool(terraform_files)
    if (repo_root / "live").exists():
        preferred_layout = "terragrunt_live_modules"
    else:
        preferred_layout = "iac_smith_default"

    warnings = []
    if uses_terraform and not uses_terragrunt:
        warnings.append(
            "Existing Terraform files found without Terragrunt; generated changes will not assume "
            "Terragrunt conventions outside IaC Smith defaults."
        )

    return RepoPatterns(
        uses_terraform=uses_terraform,
        uses_terragrunt=uses_terragrunt,
        environments=environments,
        default_environment_names=environments or ["non-prod", "prod"],
        module_sources=module_sources,
        preferred_layout=preferred_layout,
        remote_state_uses_path_relative_to_include="path_relative_to_include()" in all_hcl_text,
        existing_stack_paths=_discover_existing_stack_paths(repo_root),
        warnings=warnings,
    )
