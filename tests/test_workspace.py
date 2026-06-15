import subprocess
from pathlib import Path

from iac_smith.workspace import apply_generated_files, commit_generated_files


def test_apply_generated_files_writes_inside_repo_and_rejects_escape(tmp_path: Path):
    apply_generated_files(tmp_path, {"live/non-prod/main.tf": "terraform {}\n"})

    assert (tmp_path / "live" / "non-prod" / "main.tf").read_text() == "terraform {}\n"

    try:
        apply_generated_files(tmp_path, {"../escape.tf": "bad"})
    except ValueError as exc:
        assert "outside repository" in str(exc)
    else:
        raise AssertionError("path traversal should fail")


def test_commit_generated_files_creates_commit_when_changes_exist(tmp_path: Path):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

    apply_generated_files(tmp_path, {"README.md": "# demo\n"})
    committed = commit_generated_files(tmp_path, "feat: generate infra")

    assert committed is True
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    assert "feat: generate infra" in log


def test_commit_generated_files_returns_false_when_no_changes(tmp_path: Path):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

    assert commit_generated_files(tmp_path, "feat: generate infra") is False
