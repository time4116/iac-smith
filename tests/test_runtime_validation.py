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
    assert ["terraform", "fmt", "-check", "-recursive", "-diff", "modules"] in commands
    assert ["terraform", "init", "-backend=false", "-input=false"] in commands
    assert ["terraform", "validate"] in commands
    assert any("plan" in command for command in commands)
    assert any(
        command[:2]
        in (
            ["terragrunt", "--non-interactive"],
            ["terragrunt", "--terragrunt-non-interactive"],
        )
        and "plan" in command
        for command in commands
    )


def test_validate_generated_iac_fails_before_pr_when_plan_fails(monkeypatch, tmp_path: Path):
    (tmp_path / "environments" / "non-prod" / "ecs-fargate").mkdir(parents=True)
    (tmp_path / "environments" / "non-prod" / "ecs-fargate" / "terragrunt.hcl").write_text(
        'terraform { source = "../../../modules/ecs-fargate" }\n'
    )

    monkeypatch.setattr(
        "iac_smith.runtime_validation.shutil.which", lambda command: f"/bin/{command}"
    )

    def fake_run(command, **kwargs):
        is_plan = (
            command[:2] == ["terragrunt", "--terragrunt-non-interactive"] and "plan" in command
        )
        returncode = 1 if is_plan else 0
        return subprocess.CompletedProcess(command, returncode, stdout="bad plan", stderr="")

    monkeypatch.setattr("iac_smith.runtime_validation.subprocess.run", fake_run)

    result = validate_generated_iac(tmp_path)

    assert not result.passed
    assert "terragrunt plan" in result.errors[0]
    assert "bad plan" in result.errors[0]
