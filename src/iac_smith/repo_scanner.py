import re
from pathlib import Path

from iac_smith.models.repo_patterns import RepoPatterns

MODULE_SOURCE_RE = re.compile(r"source\s*=\s*\"([^\"]+)\"")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def _discover_environments(root: Path) -> list[str]:
    """Discover environment directories under live/ heuristically.

    Any immediate subdirectory of live/ that either contains a terragrunt.hcl
    file or contains further subdirectories (i.e. isn't a leaf stack dir) is
    treated as an environment. This avoids the old hard-coded name allowlist.
    """
    live = root / "live"
    if not live.exists():
        return []
    envs = []
    for child in sorted(live.iterdir()):
        if not child.is_dir():
            continue
        has_hcl = (child / "terragrunt.hcl").exists()
        has_subdirs = any(sub.is_dir() for sub in child.iterdir())
        if has_hcl or has_subdirs:
            envs.append(child.name)
    return envs


def _discover_module_sources(root: Path) -> list[str]:
    sources: set[str] = set()
    for path in root.rglob("*.tf"):
        sources.update(MODULE_SOURCE_RE.findall(_read_text(path)))
    for path in root.rglob("terragrunt.hcl"):
        sources.update(MODULE_SOURCE_RE.findall(_read_text(path)))
    return sorted(sources)


def _discover_existing_stack_paths(root: Path) -> list[str]:
    """Return relative paths of stack directories already in the repo.

    Includes both live/{env}/{stack} paths and modules/{stack} paths so the
    change planner can skip regenerating module scaffolds that already exist.
    """
    paths: set[str] = set()
    live = root / "live"
    if live.exists():
        for path in live.rglob("terragrunt.hcl"):
            rel = path.relative_to(root).as_posix()
            if rel != "live/terragrunt.hcl" and path.parent != live:
                paths.add(path.parent.relative_to(root).as_posix())
    modules = root / "modules"
    if modules.exists():
        for child in modules.iterdir():
            if child.is_dir():
                paths.add(child.relative_to(root).as_posix())
    return sorted(paths)


def _discover_representative_files(
    root: Path, limit: int = 12, max_chars: int = 4000
) -> dict[str, str]:
    """Capture bounded examples so the model can follow existing repo conventions."""
    candidates: list[Path] = []
    for pattern in ["live/**/terragrunt.hcl", "modules/**/*.tf", "modules/**/README.md"]:
        candidates.extend(sorted(root.glob(pattern)))

    samples: dict[str, str] = {}
    for path in candidates:
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        text = _read_text(path).strip()
        if not text:
            continue
        samples[rel] = text[:max_chars]
        if len(samples) >= limit:
            break
    return samples


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
        representative_files=_discover_representative_files(repo_root),
        warnings=warnings,
    )
