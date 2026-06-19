import subprocess
from pathlib import Path

from iac_smith.runtime_validation import (
    _force_local_state,
    _strip_backend_config,
    validate_generated_iac,
)


def _scaffold_stack(tmp_path: Path) -> None:
    (tmp_path / "modules" / "foundation").mkdir(parents=True)
    (tmp_path / "modules" / "foundation" / "main.tf").write_text(
        'resource "null_resource" "x" {}\n'
    )
    envdir = tmp_path / "environments"
    (envdir / "non-prod" / "ecs-fargate").mkdir(parents=True)
    (envdir / "non-prod" / "ecs-fargate" / "terragrunt.hcl").write_text(
        'terraform { source = "../../../modules/foundation" }\n'
    )
    (envdir / "terragrunt.hcl").write_text(
        "remote_state {\n"
        '  backend = "s3"\n'
        "  config = {\n"
        '    bucket = "b"\n'
        '    key    = "${path_relative_to_include()}/terraform.tfstate"\n'
        "  }\n"
        "}\n"
    )


def test_validate_generated_iac_runs_terraform_and_terragrunt_plan(monkeypatch, tmp_path: Path):
    (tmp_path / "modules" / "foundation").mkdir(parents=True)
    (tmp_path / "modules" / "foundation" / "main.tf").write_text(
        'resource "null_resource" "x" {}\n'
    )
    (tmp_path / "environments" / "non-prod" / "foundation").mkdir(parents=True)
    (tmp_path / "environments" / "non-prod" / "foundation" / "terragrunt.hcl").write_text(
        'terraform { source = "../../../modules/foundation" }\n'
    )

    monkeypatch.setattr(
        "iac_smith.runtime_validation.shutil.which", lambda command: f"/bin/{command}"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("cwd")))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("iac_smith.runtime_validation.subprocess.run", fake_run)

    result = validate_generated_iac(tmp_path)

    assert result.passed
    commands = [call[0] for call in calls]
    # hclfmt runs in auto-format mode (no --check); command varies by version
    hclfmt_found = any(
        cmd == ["terragrunt", "hclfmt"] or cmd == ["terragrunt", "hcl", "format"]
        for cmd in commands
    )
    assert hclfmt_found, f"No hclfmt/hcl-format command found in {commands}"
    assert ["terraform", "fmt", "-recursive", "modules"] in commands
    assert ["terraform", "init", "-backend=false", "-input=false"] in commands
    assert ["terraform", "validate"] in commands
    # terragrunt init/validate/plan are skipped for stacks — dependencies aren't deployed
    assert not any(
        command[:2]
        in (
            ["terragrunt", "--non-interactive"],
            ["terragrunt", "--terragrunt-non-interactive"],
        )
        and "plan" in command
        for command in commands
    )


def test_validate_generated_iac_fails_before_pr_when_terraform_validate_fails(
    monkeypatch, tmp_path: Path
):
    (tmp_path / "modules" / "ecs-fargate").mkdir(parents=True)
    (tmp_path / "modules" / "ecs-fargate" / "main.tf").write_text(
        'resource "aws_instance" "x" {}\n'
    )

    monkeypatch.setattr(
        "iac_smith.runtime_validation.shutil.which", lambda command: f"/bin/{command}"
    )

    def fake_run(command, **kwargs):
        is_validate = command == ["terraform", "validate"]
        returncode = 1 if is_validate else 0
        return subprocess.CompletedProcess(
            command, returncode, stdout="bad validate" if is_validate else "", stderr=""
        )

    monkeypatch.setattr("iac_smith.runtime_validation.subprocess.run", fake_run)

    result = validate_generated_iac(tmp_path)

    assert not result.passed
    assert "terraform validate" in result.errors[0]
    assert "bad validate" in result.errors[0]


def test_strip_backend_config_removes_remote_state_block():
    content = (
        "locals { x = 1 }\n"
        'remote_state {\n  backend = "s3"\n'
        '  config = { key = "${path_relative_to_include()}/t" }\n}\n'
        "inputs = {}\n"
    )
    out = _strip_backend_config(content)
    assert "remote_state" not in out
    assert 'backend = "s3"' not in out
    assert "locals { x = 1 }" in out
    assert "inputs = {}" in out


def test_strip_backend_config_removes_generate_backend_block_but_keeps_provider():
    content = (
        'generate "backend" {\n'
        '  path     = "backend.tf"\n'
        "  contents = <<EOF\n"
        'terraform { backend "s3" {} }\n'
        "EOF\n"
        "}\n"
        'generate "provider" {\n'
        '  path     = "provider.tf"\n'
        "  contents = <<EOF\n"
        'provider "aws" { region = "us-west-2" }\n'
        "EOF\n"
        "}\n"
    )
    out = _strip_backend_config(content)
    assert 'generate "backend"' not in out
    assert 'backend "s3"' not in out
    assert 'generate "provider"' in out
    assert 'provider "aws"' in out


def test_force_local_state_strips_backend_at_every_hierarchy_level(tmp_path: Path):
    # remote_state can live at the env level, not only the top root.
    envdir = tmp_path / "environments"
    (envdir / "non-prod").mkdir(parents=True)
    (envdir / "terragrunt.hcl").write_text('locals { region = "us-west-2" }\n')
    (envdir / "non-prod" / "terragrunt.hcl").write_text(
        'remote_state {\n  backend = "s3"\n  config = { bucket = "b" }\n}\n'
        "include { path = find_in_parent_folders() }\n"
    )

    _force_local_state(tmp_path)

    assert 'backend = "s3"' not in (envdir / "non-prod" / "terragrunt.hcl").read_text()
    assert "remote_state" not in (envdir / "non-prod" / "terragrunt.hcl").read_text()
    assert "include" in (envdir / "non-prod" / "terragrunt.hcl").read_text()


def test_runtime_plan_runs_terragrunt_plan_against_local_state_when_enabled(
    monkeypatch, tmp_path: Path
):
    _scaffold_stack(tmp_path)
    monkeypatch.setenv("IAC_SMITH_RUNTIME_PLAN", "1")
    monkeypatch.setattr(
        "iac_smith.runtime_validation.shutil.which", lambda command: f"/bin/{command}"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("cwd")))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("iac_smith.runtime_validation.subprocess.run", fake_run)

    result = validate_generated_iac(tmp_path)

    assert result.passed
    commands = [call[0] for call in calls]
    assert any(command[:2] == ["terragrunt", "plan"] for command in commands)
    assert any("terragrunt plan (local state) passed" in check for check in result.checks)
    # The plan must run in a scratch copy, never the real repo path.
    plan_cwd = next(c[1] for c in calls if c[0][:2] == ["terragrunt", "plan"])
    assert str(tmp_path) not in str(plan_cwd)


def test_runtime_plan_failure_blocks_pr_and_reports_error(monkeypatch, tmp_path: Path):
    _scaffold_stack(tmp_path)
    monkeypatch.setenv("IAC_SMITH_RUNTIME_PLAN", "1")
    monkeypatch.setattr(
        "iac_smith.runtime_validation.shutil.which", lambda command: f"/bin/{command}"
    )

    def fake_run(command, **kwargs):
        if command[:2] == ["terragrunt", "plan"]:
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="Error: invalid resource reference"
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("iac_smith.runtime_validation.subprocess.run", fake_run)

    result = validate_generated_iac(tmp_path)

    assert not result.passed
    assert any(
        "terragrunt plan" in error and "invalid resource reference" in error
        for error in result.errors
    )


def test_runtime_plan_skipped_when_flag_unset(monkeypatch, tmp_path: Path):
    _scaffold_stack(tmp_path)
    monkeypatch.delenv("IAC_SMITH_RUNTIME_PLAN", raising=False)
    monkeypatch.setattr(
        "iac_smith.runtime_validation.shutil.which", lambda command: f"/bin/{command}"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("iac_smith.runtime_validation.subprocess.run", fake_run)

    result = validate_generated_iac(tmp_path)

    assert result.passed
    assert not any(command[:2] == ["terragrunt", "plan"] for command in calls)
