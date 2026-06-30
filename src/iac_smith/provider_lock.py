import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Lock for the platforms the generated repo is realistically applied from (CI
# runners and developer machines). Cross-platform hashes let a colleague on a
# different OS run `terraform init` without a checksum mismatch.
_DEFAULT_PLATFORMS = ("linux_amd64", "darwin_amd64")
_DEFAULT_TIMEOUT = 240
_MAX_WORKERS = 4


# Committed alongside the lockfiles so the generated repo ignores Terraform's
# working artifacts (the lockfile itself is intentionally NOT ignored).
_TERRAFORM_GITIGNORE = """\
# Terraform
.terraform/
.terragrunt-cache/
*.tfstate
*.tfstate.*
crash.log
crash.*.log
override.tf
override.tf.json
*_override.tf
*_override.tf.json
.terraformrc
terraform.rc

# The dependency lock file is committed on purpose — do not ignore it:
# .terraform.lock.hcl
"""


def ensure_terraform_gitignore(repo_path: Path) -> bool:
    """Write a Terraform `.gitignore` when the repo has none. Returns True if written.

    Never clobbers an existing one — a cloned target repo may already have its own.
    """
    gitignore = repo_path / ".gitignore"
    if gitignore.exists():
        return False
    gitignore.write_text(_TERRAFORM_GITIGNORE)
    return True


def _terraform_path(env: Mapping[str, str]) -> str | None:
    return shutil.which("terraform", path=env.get("PATH", os.environ.get("PATH")))


def _module_dirs(repo_path: Path) -> list[Path]:
    """Terraform roots that should ship a lockfile: each `modules/<x>/` with a versions.tf."""
    modules_root = repo_path / "modules"
    if not modules_root.is_dir():
        return []
    return sorted(d for d in modules_root.iterdir() if d.is_dir() and (d / "versions.tf").is_file())


def _cleanup(module_dir: Path) -> None:
    # The lockfile is the only artifact worth keeping; the .terraform plugin dir and
    # any state must never be committed.
    shutil.rmtree(module_dir / ".terraform", ignore_errors=True)
    for junk in module_dir.glob(".terraform.tfstate*"):
        junk.unlink(missing_ok=True)


def _lock_one(
    module_dir: Path,
    *,
    platforms: tuple[str, ...],
    run_env: Mapping[str, str],
    timeout: int,
    terraform: str,
) -> Path | None:
    # `providers lock` needs the module's providers resolved first (a module call
    # like terraform-aws-modules/vpc contributes provider requirements), so init
    # before locking. -backend=false avoids needing any backend/state.
    common = {
        "cwd": module_dir,
        "env": dict(run_env),
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "check": True,
    }
    try:
        subprocess.run([terraform, "init", "-backend=false", "-input=false", "-no-color"], **common)
        subprocess.run(
            [terraform, "providers", "lock", "-no-color", *[f"-platform={p}" for p in platforms]],
            **common,
        )
    finally:
        _cleanup(module_dir)
    lock = module_dir / ".terraform.lock.hcl"
    return lock if lock.is_file() else None


def generate_provider_locks(
    repo_path: Path,
    *,
    env: Mapping[str, str],
    platforms: tuple[str, ...] = _DEFAULT_PLATFORMS,
    timeout: int = _DEFAULT_TIMEOUT,
    log: Callable[[str], None] | None = None,
) -> list[Path]:
    """Write a multi-platform `.terraform.lock.hcl` in each generated module (best-effort).

    Pinned provider versions plus cross-platform checksums make the generated repo's
    applies reproducible and detect a tampered provider — the supply-chain control
    the `~>` version constraints alone do not give. Modules are locked in parallel so
    wall time stays close to a single provider-download cycle regardless of count.
    A module that fails to lock is logged and skipped; lockfiles are a hardening
    nicety and must never block the PR.
    """
    emit = log or (lambda _message: None)
    terraform = _terraform_path(env)
    if terraform is None:
        emit("IaC Smith: terraform not on PATH; skipping provider lockfile generation.")
        return []
    module_dirs = _module_dirs(repo_path)
    if not module_dirs:
        return []
    run_env = {**os.environ, **env}

    def lock(module_dir: Path) -> Path | None:
        try:
            return _lock_one(
                module_dir,
                platforms=platforms,
                run_env=run_env,
                timeout=timeout,
                terraform=terraform,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            emit(f"IaC Smith: could not lock providers for {module_dir.name}: {exc}")
            _cleanup(module_dir)
            return None

    workers = min(_MAX_WORKERS, len(module_dirs))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        written = [path for path in executor.map(lock, module_dirs) if path is not None]
    return sorted(written)
