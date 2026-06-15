import subprocess
from pathlib import Path

from iac_smith.cli import run_iac_smith
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.services.github import GitHubIssue, GitHubPullRequest


class FakeIssueClient:
    def fetch_issue(self, repo: str, issue_number: int) -> GitHubIssue:
        return GitHubIssue(
            number=42,
            title="Create non-prod VPC",
            body="Create a non-prod VPC foundation in us-west-2.",
            url="https://github.com/time4116/iac-smith/issues/42",
            labels=["iac-smith"],
        )


class FakePullRequestClient:
    def __init__(self):
        self.calls = []

    def create_pull_request(self, repo: str, title: str, body: str, head: str, base: str = "main"):
        self.calls.append({"repo": repo, "title": title, "body": body, "head": head, "base": base})
        return GitHubPullRequest(
            number=9, url="https://github.com/time4116/iac-smith-demo-infra/pull/9"
        )


def _fake_intent_parser(issue_text: str) -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request=issue_text,
        resource_type="vpc_foundation",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        requires_new_vpc=True,
        features=["remote_state", "private_subnets"],
    )


def test_run_iac_smith_generates_commits_and_opens_pr(tmp_path: Path):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# existing\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    pr_client = FakePullRequestClient()

    result = run_iac_smith(
        env={
            "IAC_SMITH_SOURCE_REPO": "time4116/iac-smith",
            "IAC_SMITH_ISSUE_NUMBER": "42",
            "IAC_SMITH_TARGET_REPO": "time4116/iac-smith-demo-infra",
            "IAC_SMITH_ALLOWED_TARGET_REPO": "time4116/iac-smith-demo-infra",
            "IAC_SMITH_TARGET_REPO_PATH": str(tmp_path),
            "IAC_SMITH_SKIP_PUSH": "1",
        },
        issue_client=FakeIssueClient(),
        pr_client=pr_client,
        intent_parser_fn=_fake_intent_parser,
    )

    assert result.pr_url == "https://github.com/time4116/iac-smith-demo-infra/pull/9"
    assert result.branch.startswith("iac-smith/issue-42-create-non-prod-vpc")
    assert (tmp_path / "modules" / "vpc-foundation" / "main.tf").exists()
    assert pr_client.calls[0]["repo"] == "time4116/iac-smith-demo-infra"
    assert pr_client.calls[0]["head"] == result.branch
