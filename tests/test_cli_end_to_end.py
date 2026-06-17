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


def _fake_file_generator(
    *, intent, change_plan, repo_patterns, ruleset, target_repo, repo_path=None
):
    main_tf = 'resource "aws_vpc" "this" { cidr_block = "10.0.0.0/16" }\n'
    readme = "# vpc-foundation\n<!-- BEGIN_TF_DOCS -->\n<!-- END_TF_DOCS -->\n"
    return {
        "modules/vpc-foundation/main.tf": main_tf,
        "modules/vpc-foundation/variables.tf": "",
        "modules/vpc-foundation/outputs.tf": "",
        "modules/vpc-foundation/versions.tf": "",
        "modules/vpc-foundation/README.md": readme,
    }


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
            "IAC_SMITH_SKIP_RUNTIME_VALIDATION": "1",
        },
        issue_client=FakeIssueClient(),
        pr_client=pr_client,
        intent_parser_fn=_fake_intent_parser,
        file_generator_fn=_fake_file_generator,
    )

    assert result.pr_url == "https://github.com/time4116/iac-smith-demo-infra/pull/9"
    assert result.branch.startswith("iac-smith/issue-42-create-non-prod-vpc")
    assert (tmp_path / "modules" / "vpc-foundation" / "main.tf").exists()
    assert pr_client.calls[0]["repo"] == "time4116/iac-smith-demo-infra"
    assert pr_client.calls[0]["head"] == result.branch


def test_run_iac_smith_logs_progress_to_stdout(tmp_path: Path, capsys):
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

    run_iac_smith(
        env={
            "IAC_SMITH_SOURCE_REPO": "time4116/iac-smith",
            "IAC_SMITH_ISSUE_NUMBER": "42",
            "IAC_SMITH_TARGET_REPO": "time4116/iac-smith-demo-infra",
            "IAC_SMITH_ALLOWED_TARGET_REPO": "time4116/iac-smith-demo-infra",
            "IAC_SMITH_TARGET_REPO_PATH": str(tmp_path),
            "IAC_SMITH_SKIP_PUSH": "1",
            "IAC_SMITH_SKIP_RUNTIME_VALIDATION": "1",
        },
        issue_client=FakeIssueClient(),
        pr_client=FakePullRequestClient(),
        intent_parser_fn=_fake_intent_parser,
        file_generator_fn=_fake_file_generator,
    )

    out = capsys.readouterr().out
    assert "IaC Smith: using target repo path" in out
    assert "IaC Smith: fetched issue #42: Create non-prod VPC" in out
    assert "IaC Smith: running graph." in out
    assert "IaC Smith: graph finished with status pr_ready." in out
    assert "IaC Smith: creating branch" in out
    assert "IaC Smith: opening pull request." in out


def test_run_iac_smith_repairs_runtime_validation_failures_before_pr(tmp_path: Path, monkeypatch):
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

    class ValidationResult:
        def __init__(self, passed: bool, errors: list[str] | None = None):
            self.passed = passed
            self.errors = errors or []

    validation_results = [
        ValidationResult(False, ["terraform validate failed: invalid cidr_block"]),
        ValidationResult(True),
    ]

    def fake_validate(repo_path, **kwargs):
        return validation_results.pop(0)

    class RepairingFileGenerator:
        def __init__(self):
            self.repair_errors: list[str] = []

        def __call__(
            self, *, intent, change_plan, repo_patterns, ruleset, target_repo, repo_path=None
        ):
            return _fake_file_generator(
                intent=intent,
                change_plan=change_plan,
                repo_patterns=repo_patterns,
                ruleset=ruleset,
                target_repo=target_repo,
            )

        def repair_files(
            self,
            *,
            intent,
            change_plan,
            repo_patterns,
            ruleset,
            target_repo,
            generated_files,
            repair_errors,
        ):
            self.repair_errors = repair_errors
            repaired = dict(generated_files)
            repaired["modules/vpc-foundation/main.tf"] = (
                'resource "aws_vpc" "this" { cidr_block = "10.1.0.0/16" }\n'
            )
            return repaired

    generator = RepairingFileGenerator()
    monkeypatch.setattr("iac_smith.cli.validate_generated_iac", fake_validate)

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
        file_generator_fn=generator,
    )

    assert result.status == "pr_created"
    assert generator.repair_errors == ["terraform validate failed: invalid cidr_block"]
    assert pr_client.calls
    assert (
        tmp_path / "modules" / "vpc-foundation" / "main.tf"
    ).read_text() == 'resource "aws_vpc" "this" { cidr_block = "10.1.0.0/16" }\n'


def test_run_iac_smith_blocks_when_runtime_validation_fails(tmp_path: Path, monkeypatch):
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

    class FailedValidation:
        passed = False
        errors = ["terragrunt plan failed"]

    monkeypatch.setattr(
        "iac_smith.cli.validate_generated_iac", lambda repo_path, **kwargs: FailedValidation()
    )

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
        file_generator_fn=_fake_file_generator,
    )

    assert result.status == "blocked"
    assert result.block_reason == "terragrunt plan failed"
    assert pr_client.calls == []
