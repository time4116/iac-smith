import subprocess
from pathlib import Path

from iac_smith.runtime_validation import validate_generated_iac


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
