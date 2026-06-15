import os

import pytest

from iac_smith.cli import build_initial_state, select_github_token, validate_allowed_target_repo
from iac_smith.services.github import GitHubIssue


def test_validate_allowed_target_repo_passes_exact_match(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")

    assert validate_allowed_target_repo(os.environ) == "time4116/iac-smith-demo-infra"


def test_validate_allowed_target_repo_fails_closed_on_mismatch(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "attacker/repo")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")

    with pytest.raises(SystemExit, match="not allowed"):
        validate_allowed_target_repo(os.environ)


def test_validate_allowed_target_repo_requires_allowlist(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.delenv("IAC_SMITH_ALLOWED_TARGET_REPO", raising=False)

    with pytest.raises(SystemExit, match="IAC_SMITH_ALLOWED_TARGET_REPO"):
        validate_allowed_target_repo(os.environ)


class FakeIssueClient:
    def __init__(self):
        self.calls = []

    def fetch_issue(self, repo: str, issue_number: int) -> GitHubIssue:
        self.calls.append((repo, issue_number))
        return GitHubIssue(
            number=12,
            title="Create EKS Fargate",
            body="Create a non-prod EKS Fargate setup in us-west-2.",
            url="https://github.com/time4116/iac-smith/issues/12",
            labels=["iac-smith"],
        )


def test_build_initial_state_fetches_source_issue_and_target_repo(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_SOURCE_REPO", "time4116/iac-smith")
    monkeypatch.setenv("IAC_SMITH_ISSUE_NUMBER", "12")
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")
    issue_client = FakeIssueClient()

    state = build_initial_state(os.environ, issue_client=issue_client)

    assert issue_client.calls == [("time4116/iac-smith", 12)]
    assert state["issue_number"] == 12
    assert state["issue_title"] == "Create EKS Fargate"
    assert state["issue_body"] == "Create a non-prod EKS Fargate setup in us-west-2."
    assert state["issue_url"] == "https://github.com/time4116/iac-smith/issues/12"
    assert state["labels"] == ["iac-smith"]
    assert state["target_repo"] == "time4116/iac-smith-demo-infra"


def test_build_initial_state_requires_integer_issue_number(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_SOURCE_REPO", "time4116/iac-smith")
    monkeypatch.setenv("IAC_SMITH_ISSUE_NUMBER", "abc")
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")

    with pytest.raises(SystemExit, match="IAC_SMITH_ISSUE_NUMBER"):
        build_initial_state(os.environ, issue_client=FakeIssueClient())


def test_build_initial_state_rejects_malformed_source_repo(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_SOURCE_REPO", "https://github.com/time4116/iac-smith")
    monkeypatch.setenv("IAC_SMITH_ISSUE_NUMBER", "12")
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")

    with pytest.raises(SystemExit, match="IAC_SMITH_SOURCE_REPO"):
        build_initial_state(os.environ, issue_client=FakeIssueClient())


def test_select_github_token_prefers_project_specific_token():
    token = select_github_token(
        {
            "GITHUB_TOKEN": "ambient-token",
            "IAC_SMITH_GITHUB_TOKEN": "workflow-token",
        }
    )

    assert token == "workflow-token"


def test_push_branch_uses_force_flag(monkeypatch):
    from pathlib import Path

    calls = []

    def fake_run(command, cwd=None):
        calls.append((command, cwd))

    monkeypatch.setattr("iac_smith.cli._run", fake_run)
    from iac_smith.cli import push_branch

    push_branch(Path("/dummy/repo"), "my-branch", "my-token")

    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert "push" in cmd
    assert "-f" in cmd or "--force" in cmd
    assert "my-branch" in cmd
    assert cwd == Path("/dummy/repo")
