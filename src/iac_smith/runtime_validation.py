import os
import re
import shutil
import subprocess
import tempfile
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

    # A stack is any terragrunt.hcl nested at least one level below its
    # environment dir (environments/<env>/<stack>/…). This matches
    # static_review._is_stack_terragrunt, so grouped stacks like
    # environments/<env>/<group>/<stack>/terragrunt.hcl are planned too — not just
    # the flat two-level layout. The environment root config
    # (environments/<env>/terragrunt.hcl) is excluded, and cache/hidden dirs are
    # skipped so we never pick up a .terragrunt-cache copy.
    env_dir = repo_path / "environments"
    terragrunt_stacks = sorted(
        path.parent
        for path in env_dir.rglob("terragrunt.hcl")
        if path.parent.is_dir()
        and len((rel := path.relative_to(env_dir)).parts) >= 3
        and not any(part.startswith(".") for part in rel.parts)
    )
    return module_roots, terragrunt_stacks


def _detect_terragrunt(env: dict[str, str]) -> tuple[list[str], str]:
    """Return (hclfmt_cmd, non_interactive_flag) for the installed terragrunt version.

    Terragrunt v0.71.0+ renamed ``hclfmt`` → ``hcl format`` and
    ``--terragrunt-non-interactive`` → ``--non-interactive``.

    hclfmt_cmd runs the formatter in auto-fix mode (no --check flag) so that
    whitespace/indentation issues are silently corrected in place. Syntax errors
    still cause a non-zero exit.
    """
    import re

    result = subprocess.run(
        ["terragrunt", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    version_out = (result.stdout + result.stderr).strip()
    match = re.search(r"v?(\d+)\.(\d+)", version_out)
    is_new = False
    if match:
        major, minor = int(match.group(1)), int(match.group(2))
        is_new = major >= 1 or (major == 0 and minor >= 71)
    hclfmt_cmd = ["terragrunt", "hcl", "format"] if is_new else ["terragrunt", "hclfmt"]
    non_interactive = "--non-interactive" if is_new else "--terragrunt-non-interactive"
    return hclfmt_cmd, non_interactive


def _strip_first_block(content: str, pattern: str) -> tuple[str, bool]:
    """Remove the first ``... { }`` block whose header matches pattern (brace-matched)."""
    m = re.search(pattern, content)
    if not m:
        return content, False
    brace_start = content.index("{", m.start())
    depth = 0
    for i in range(brace_start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return content[: m.start()] + content[i + 1 :], True
    return content, False


def _strip_backend_config(content: str) -> str:
    """Remove every backend-defining block so terragrunt falls back to local state.

    Handles both ``remote_state { ... }`` and ``generate "<name>" { ... }`` blocks
    that emit a Terraform backend. A generate block is only stripped when its body
    declares a backend, so generated provider blocks are preserved.
    """
    while True:
        content, changed = _strip_first_block(content, r"\bremote_state\s*\{")
        if not changed:
            break

    searched_from = 0
    while True:
        m = re.search(r'\bgenerate\s+"[^"]*"\s*\{', content[searched_from:])
        if not m:
            break
        abs_start = searched_from + m.start()
        block_text, _ = _strip_first_block(content[abs_start:], r'\bgenerate\s+"[^"]*"\s*\{')
        removed_len = len(content) - abs_start - len(block_text)
        block_body = content[abs_start : abs_start + removed_len]
        if re.search(r'backend\s*[="]', block_body):
            content = content[:abs_start] + content[abs_start + removed_len :]
        else:
            searched_from = abs_start + 1
    return content


def _force_local_state(scratch_root: Path) -> None:
    """Strip S3/remote backend config from every terragrunt config in the scratch copy.

    The committed PR keeps its real S3 backend; this only mutates a throwaway copy
    so ``terragrunt plan`` can init against local state without the state bucket
    existing yet. remote_state can be declared at any level of the hierarchy
    (root, environment, or stack), so every ``terragrunt.hcl`` / ``root.hcl`` is
    rewritten — not just the top-level one.
    """
    for cfg in [*scratch_root.rglob("terragrunt.hcl"), *scratch_root.rglob("root.hcl")]:
        content = cfg.read_text()
        stripped = _strip_backend_config(content)
        if stripped != content:
            cfg.write_text(stripped)


def _run_local_state_plans(
    root: Path, env: dict[str, str], non_interactive: str
) -> tuple[list[str], list[str]]:
    """Run ``terragrunt plan`` per stack against local state.

    Returns ``(passed_checks, errors)`` where each passed check names the literal
    command and stack it ran in.

    Plan-only — never apply — so no infrastructure is created.  Stacks are copied
    into a temp tree whose root remote_state is rewritten to a local backend, so
    plan can auto-init without the S3 backend.  Dependent stacks resolve their
    inputs through ``mock_outputs`` (which terragrunt allows for ``plan``), so the
    foundation stack does not need to be applied first.
    """
    _, terragrunt_stacks = _changed_roots(root)
    if not terragrunt_stacks:
        return [], []

    plan_command = ["terragrunt", "plan", non_interactive, "-input=false"]
    checks: list[str] = []
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="iac-smith-plan-") as tmp:
        scratch = Path(tmp) / "repo"
        shutil.copytree(
            root,
            scratch,
            ignore=shutil.ignore_patterns(
                ".git", ".terraform", ".terragrunt-cache", "*.tfstate", "*.tfstate.*"
            ),
        )
        _force_local_state(scratch)
        for stack in terragrunt_stacks:
            rel = stack.relative_to(root).as_posix()
            stack_dir = scratch / stack.relative_to(root)
            ok, output = _run_check(plan_command, stack_dir, env)
            if ok:
                checks.append(f"`{' '.join(plan_command)}` in `{rel}` (local state)")
                continue
            errors.append(f"terragrunt plan {rel} failed in `{rel}`:\n{output}")
            break
    return checks, errors


def validate_generated_iac(
    repo_path: str | Path, env_override: dict[str, str] | None = None
) -> RuntimeValidationResult:
    """Run local IaC validation before IaC Smith commits and opens a PR.

    This intentionally never applies infrastructure. When ``IAC_SMITH_RUNTIME_PLAN``
    is set, a real ``terragrunt plan`` runs per stack against a local-state copy of
    the tree (dependencies resolved through ``mock_outputs``), so generated
    Terraform/Terragrunt is forced through the actual provider/plan path before a
    PR is opened — failures feed the self-healing repair loop. Without the flag,
    validation stops at module-level ``terraform validate`` (schema-only).
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

    terragrunt_hclfmt_cmd, non_interactive = _detect_terragrunt(env)
    command_specs: list[tuple[str, list[str], Path]] = []
    if (root / "environments").exists():
        command_specs.append(
            (
                "terragrunt hclfmt",
                terragrunt_hclfmt_cmd,
                root / "environments",
            )
        )
    fmt_paths = [path.name for path in [root / "modules", root / "bootstrap"] if path.exists()]
    if fmt_paths:
        # Auto-fix rather than check-and-fail: minor alignment differences are silently
        # corrected in place so repair attempts are reserved for real schema errors.
        command_specs.append(
            (
                "terraform fmt",
                ["terraform", "fmt", "-recursive", *fmt_paths],
                root,
            )
        )

    module_roots, _ = _changed_roots(root)
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

    for label, command, cwd in command_specs:
        ok, output = _run_check(command, cwd, env)
        where = cwd.relative_to(root).as_posix()
        location = "repo root" if where == "." else f"`{where}`"
        if ok:
            checks.append(f"`{' '.join(command)}` in {location}")
            continue
        errors.append(f"{label} failed in `{cwd.relative_to(root)}`:\n{output}")
        break

    # Schema-valid modules then go through a real terragrunt plan (local state,
    # mock_outputs for dependencies) so apply-path errors are caught and fed to
    # the repair loop before a PR is opened. Opt-in: the controller's AWS role
    # must have read/describe permissions for the providers being planned.
    if not errors and env.get("IAC_SMITH_RUNTIME_PLAN") == "1":
        plan_checks, plan_errors = _run_local_state_plans(root, env, non_interactive)
        if plan_errors:
            errors.extend(plan_errors)
        else:
            checks.extend(plan_checks)

    return RuntimeValidationResult(passed=not errors, checks=checks, errors=errors)
