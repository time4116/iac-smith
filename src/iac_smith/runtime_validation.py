import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RuntimeValidationResult:
    passed: bool
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _run_check(command: list[str], cwd: Path, env: dict[str, str]) -> tuple[bool, str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
    return completed.returncode == 0, output


def _changed_roots(repo_path: Path) -> tuple[list[Path], list[Path]]:
    module_roots = sorted({path.parent for path in (repo_path / "modules").glob("*/*.tf")})
    terragrunt_stacks = sorted(
        path.parent
        for path in (repo_path / "environments").glob("*/*/terragrunt.hcl")
        if path.parent.is_dir()
    )
    return module_roots, terragrunt_stacks


def _terragrunt_hclfmt_cmd(env: dict[str, str]) -> list[str]:
    """Return the correct hclfmt/hcl-format command for the installed terragrunt version.

    Terragrunt v0.71.0+ renamed ``hclfmt`` to ``hcl format``. Older versions
    use ``hclfmt``. Detect once and cache the decision.
    """
    result = subprocess.run(
        ["terragrunt", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    version_out = (result.stdout + result.stderr).strip()
    # Match a dotted semver after 'v' — "terragrunt version v0.71.1" → "0.71.1"
    import re

    match = re.search(r"v?(\d+)\.(\d+)", version_out)
    if match:
        major, minor = int(match.group(1)), int(match.group(2))
        if major >= 1 or (major == 0 and minor >= 71):
            return ["terragrunt", "hcl", "format", "--check", "--diff"]
    return ["terragrunt", "hclfmt", "--check", "--diff"]


def validate_generated_iac(
    repo_path: str | Path, env_override: dict[str, str] | None = None
) -> RuntimeValidationResult:
    """Run local IaC validation before IaC Smith commits and opens a PR.

    This intentionally never applies infrastructure. Terragrunt plan is included so
    generated Terraform/Terragrunt is forced through the same provider/schema path
    as the PR checks while the controller can still fail before publishing a PR.
    """

    root = Path(repo_path)
    checks: list[str] = []
    errors: list[str] = []
    env = {
        **(env_override or os.environ),
        "TF_INPUT": "false",
        "TF_IN_AUTOMATION": "true",
        "CI": (env_override or os.environ).get("CI", "true"),
    }

    required_commands = ["terraform", "terragrunt"]
    missing = [command for command in required_commands if shutil.which(command) is None]
    if missing:
        return RuntimeValidationResult(
            passed=False,
            errors=["Missing required validation command(s): " + ", ".join(missing)],
        )

    command_specs: list[tuple[str, list[str], Path]] = []
    if (root / "environments").exists():
        terragrunt_hclfmt_cmd = _terragrunt_hclfmt_cmd(env)
        command_specs.append(
            (
                "terragrunt hclfmt",
                terragrunt_hclfmt_cmd,
                root / "environments",
            )
        )
    fmt_paths = [path.name for path in [root / "modules", root / "bootstrap"] if path.exists()]
    if fmt_paths:
        command_specs.append(
            (
                "terraform fmt",
                ["terraform", "fmt", "-check", "-recursive", "-diff", *fmt_paths],
                root,
            )
        )

    module_roots, terragrunt_stacks = _changed_roots(root)
    for module_root in module_roots:
        command_specs.append(
            (
                f"terraform init {module_root.relative_to(root)}",
                ["terraform", "init", "-backend=false", "-input=false"],
                module_root,
            )
        )
        command_specs.append(
            (
                f"terraform validate {module_root.relative_to(root)}",
                ["terraform", "validate"],
                module_root,
            )
        )

    for stack in terragrunt_stacks:
        label = str(stack.relative_to(root))
        command_specs.append(
            (
                f"terragrunt init {label}",
                ["terragrunt", "init", "-reconfigure", "--terragrunt-non-interactive"],
                stack,
            )
        )
        command_specs.append(
            (
                f"terragrunt validate {label}",
                ["terragrunt", "validate", "--terragrunt-non-interactive"],
                stack,
            )
        )
        command_specs.append(
            (
                f"terragrunt plan {label}",
                [
                    "terragrunt",
                    "plan",
                    "-input=false",
                    "-lock=false",
                    "-out=tfplan.binary",
                    "--terragrunt-non-interactive",
                ],
                stack,
            )
        )

    for label, command, cwd in command_specs:
        ok, output = _run_check(command, cwd, env)
        if ok:
            checks.append(f"{label} passed.")
            continue
        errors.append(f"{label} failed in `{cwd.relative_to(root)}`:\n{output}")
        break

    return RuntimeValidationResult(passed=not errors, checks=checks, errors=errors)
