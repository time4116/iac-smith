import subprocess
from pathlib import Path

from iac_smith.blackboard import TerraformContract
from iac_smith.cli import (
    _build_escalation_repairer,
    _descriptive_title,
    _repair_runtime_static_issues,
    _select_repair_model,
    run_iac_smith,
)
from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
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


def test_descriptive_title_includes_stack_region_and_env():
    result = {
        "issue_number": 37,
        "change_plan": ChangePlan(
            stack_name="app-runner-open-webui",
            environments=["non-prod"],
            files_to_generate=[],
            backend_resources={},
            summary=[],
        ),
        "intent": _fake_intent_parser("Deploy Open WebUI on App Runner"),
    }

    assert _descriptive_title(result) == "feat: app-runner-open-webui in us-west-2 (non-prod) (#37)"


def test_descriptive_title_falls_back_without_a_stack():
    assert (
        _descriptive_title({"issue_number": 9, "change_plan": None, "intent": None})
        == "feat: generate IaC for issue #9"
    )


class _FakeRepairer:
    def __init__(self, name: str) -> None:
        self.model_id = name

    def repair_files(self, **kwargs):  # pragma: no cover - not invoked in selection tests
        return kwargs["generated_files"]


def test_select_repair_model_without_escalation_always_uses_primary():
    primary = _FakeRepairer("haiku")
    for attempt in range(3):
        repairer, escalated = _select_repair_model(
            repair_attempt=attempt,
            max_runtime_repairs=2,
            primary=primary,
            escalation=None,
        )
        assert repairer is primary
        assert escalated is False


def test_select_repair_model_escalates_penultimate_then_cleans_up_with_primary():
    # Default budget of 3 repairs (indices 0,1,2): Haiku, Sonnet, Haiku-cleanup.
    primary = _FakeRepairer("haiku")
    escalation = _FakeRepairer("sonnet")

    def select(attempt):
        return _select_repair_model(
            repair_attempt=attempt,
            max_runtime_repairs=3,
            primary=primary,
            escalation=escalation,
        )

    assert select(0) == (primary, False)  # first repair stays on the primary model
    assert select(1) == (escalation, True)  # still stuck -> escalate the heavy lift
    assert select(2) == (primary, False)  # final pass cleans up what escalation unlocked


def test_select_repair_model_never_escalates_the_first_repair():
    primary = _FakeRepairer("haiku")
    escalation = _FakeRepairer("sonnet")

    # With only 2 repairs, the penultimate is index 0; escalation must NOT fire
    # as the very first repair, so the whole loop stays on the primary model.
    assert _select_repair_model(
        repair_attempt=0, max_runtime_repairs=2, primary=primary, escalation=escalation
    ) == (primary, False)


def test_build_escalation_repairer_returns_none_without_env():
    assert _build_escalation_repairer({}, "anthropic.claude-haiku") is None


def test_build_escalation_repairer_returns_none_when_same_as_primary():
    env = {"BEDROCK_ESCALATION_MODEL_ID": "anthropic.claude-haiku"}
    assert _build_escalation_repairer(env, "anthropic.claude-haiku") is None


def test_build_escalation_repairer_builds_generator_for_distinct_model():
    env = {"BEDROCK_ESCALATION_MODEL_ID": "anthropic.claude-sonnet-4-6"}
    repairer = _build_escalation_repairer(env, "anthropic.claude-haiku")
    assert repairer is not None
    assert repairer.model_id == "anthropic.claude-sonnet-4-6"


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
        def __init__(
            self,
            passed: bool,
            errors: list[str] | None = None,
            checks: list[str] | None = None,
            contract_docs: dict | None = None,
        ):
            self.passed = passed
            self.errors = errors or []
            self.checks = checks or []
            self.contract_docs = contract_docs or {}

    unsupported_arg_error = (
        "terraform validate modules/vpc-foundation failed:\n"
        "│ Error: Unsupported argument\n"
        '│   on main.tf line 5, in resource "aws_vpc" "this":\n'
        '│ An argument named "instance_type" is not expected here.'
    )
    harvested_contract = TerraformContract(
        kind="provider_resource",
        name="aws_vpc",
        allowed_arguments=["cidr_block", "tags"],
        source="terraform providers schema -json (hashicorp/aws)",
    )
    validation_results = [
        ValidationResult(
            False, [unsupported_arg_error], contract_docs={"aws_vpc": harvested_contract}
        ),
        ValidationResult(True, checks=["terraform validate modules/vpc-foundation passed."]),
    ]

    def fake_validate(repo_path, **kwargs):
        return validation_results.pop(0)

    class RepairingFileGenerator:
        def __init__(self):
            self.repair_errors: list[str] = []
            self.blackboard = None

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
            blackboard=None,
        ):
            self.repair_errors = repair_errors
            self.blackboard = blackboard
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
    assert generator.repair_errors == [unsupported_arg_error]
    # The runtime failure was learned into the blackboard and handed to the repair
    # step as a negative pattern, so the model is told not to repeat it.
    assert generator.blackboard is not None
    assert any(
        "instance_type" in pattern and "aws_vpc" in pattern
        for pattern in generator.blackboard.negative_patterns
    )
    # The authoritative contract harvested from the initialized provider is also
    # handed to repair, so the prompt gets the real allowed arguments — not just
    # "don't repeat this".
    assert "aws_vpc" in generator.blackboard.contract_docs
    assert generator.blackboard.contract_docs["aws_vpc"].allowed_arguments == [
        "cidr_block",
        "tags",
    ]
    assert pr_client.calls
    assert (
        tmp_path / "modules" / "vpc-foundation" / "main.tf"
    ).read_text() == 'resource "aws_vpc" "this" { cidr_block = "10.1.0.0/16" }\n'


def test_runtime_static_repair_rechecks_until_module_stack_contract_converges(tmp_path: Path):
    files = {
        "environments/non-prod/app-runner-open-webui/terragrunt.hcl": (
            'terraform {\n  source = "../../../modules//app-runner-open-webui"\n}\n'
            "inputs = {\n  app_port = 8080\n}\n"
        ),
        "modules/app-runner-open-webui/main.tf": (
            'resource "null_resource" "open_webui" {\n  triggers = { image = var.image_uri }\n}\n'
        ),
        "modules/app-runner-open-webui/variables.tf": ('variable "app_port" { type = number }\n'),
        "modules/app-runner-open-webui/outputs.tf": "",
        "modules/app-runner-open-webui/versions.tf": "",
    }

    class ContractRepairer:
        def __init__(self):
            self.seen_errors: list[list[str]] = []

        def repair_files(self, **kwargs):
            self.seen_errors.append(list(kwargs["repair_errors"]))
            repaired = dict(kwargs["generated_files"])
            joined_errors = "\n".join(kwargs["repair_errors"])
            if 'Add variable "image_uri"' in joined_errors:
                repaired["modules/app-runner-open-webui/variables.tf"] = (
                    'variable "app_port" { type = number }\n'
                    'variable "image_uri" { type = string }\n'
                )
            if "does not pass required input `image_uri`" in joined_errors:
                repaired["environments/non-prod/app-runner-open-webui/terragrunt.hcl"] = (
                    'terraform {\n  source = "../../../modules//app-runner-open-webui"\n}\n'
                    "inputs = {\n"
                    "  app_port = 8080\n"
                    '  image_uri = "public.ecr.aws/example/open-webui:latest"\n'
                    "}\n"
                )
            return repaired

    result = {
        "intent": _fake_intent_parser("Deploy Open WebUI on App Runner"),
        "change_plan": ChangePlan(
            stack_name="app-runner-open-webui",
            environments=["non-prod"],
            files_to_generate=list(files),
            backend_resources={"non-prod": BackendResource(bucket="state", lock_table="lock")},
            summary=["Generate App Runner stack"],
        ),
        "repo_patterns": RepoPatterns(),
        "ruleset": None,
        "target_repo": "time4116/iac-smith-demo-infra",
        "generated_files": files,
    }

    repaired = _repair_runtime_static_issues(
        repairer=ContractRepairer(),
        result=result,
        repo_path=tmp_path,
        repaired_files=files,
        repair_errors=["terraform validate modules/app-runner-open-webui failed"],
    )

    assert 'variable "image_uri"' in repaired["modules/app-runner-open-webui/variables.tf"]
    assert "image_uri" in repaired["environments/non-prod/app-runner-open-webui/terragrunt.hcl"]


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
