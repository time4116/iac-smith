import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from iac_smith.dynamic_terraform import BedrockTerraformGenerator
from iac_smith.graph import FileGenerator, IntentParser, build_graph
from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.rules import Ruleset
from iac_smith.models.validation import ValidationStatus
from iac_smith.nodes.pr_writer import branch_name_for_issue
from iac_smith.nodes.static_review import static_review_generated_files
from iac_smith.runtime_validation import validate_generated_iac
from iac_smith.services.github import (
    GitHubIssue,
    GitHubIssueClient,
    GitHubPullRequest,
    GitHubPullRequestClient,
)
from iac_smith.state import IaCSmithState
from iac_smith.version_detection import ensure_terraform_terragrunt
from iac_smith.workspace import apply_generated_files, commit_generated_files, create_branch


class IssueClient(Protocol):
    def fetch_issue(self, repo: str, issue_number: int) -> GitHubIssue: ...


class PullRequestClient(Protocol):
    def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> GitHubPullRequest: ...


class RuntimeRepairer(Protocol):
    def repair_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
        generated_files: dict[str, str],
        repair_errors: list[str],
    ) -> dict[str, str]: ...


@dataclass(frozen=True)
class IaCSmithRunResult:
    status: str
    branch: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    block_reason: str | None = None


_REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def validate_repo_name(value: str | None, env_name: str) -> str:
    if not value:
        raise SystemExit(f"{env_name} must be set.")
    if not _REPO_NAME_PATTERN.fullmatch(value):
        raise SystemExit(f"{env_name} must use owner/repo format.")
    return value


def validate_allowed_target_repo(env: Mapping[str, str]) -> str:
    target_repo = env.get("IAC_SMITH_TARGET_REPO")
    allowed_target_repo = env.get("IAC_SMITH_ALLOWED_TARGET_REPO")

    if not allowed_target_repo:
        raise SystemExit("IAC_SMITH_ALLOWED_TARGET_REPO must be set; failing closed.")
    target_repo = validate_repo_name(target_repo, "IAC_SMITH_TARGET_REPO")
    allowed_target_repo = validate_repo_name(allowed_target_repo, "IAC_SMITH_ALLOWED_TARGET_REPO")
    if target_repo != allowed_target_repo:
        raise SystemExit(f"Target repo `{target_repo}` is not allowed.")
    return target_repo


def parse_issue_number(env: Mapping[str, str]) -> int:
    value = env.get("IAC_SMITH_ISSUE_NUMBER")
    if not value:
        raise SystemExit("IAC_SMITH_ISSUE_NUMBER must be set.")
    try:
        issue_number = int(value)
    except ValueError as exc:
        raise SystemExit("IAC_SMITH_ISSUE_NUMBER must be an integer.") from exc
    if issue_number <= 0:
        raise SystemExit("IAC_SMITH_ISSUE_NUMBER must be positive.")
    return issue_number


def build_initial_state(env: Mapping[str, str], issue_client: IssueClient) -> IaCSmithState:
    target_repo = validate_allowed_target_repo(env)
    source_repo = validate_repo_name(env.get("IAC_SMITH_SOURCE_REPO"), "IAC_SMITH_SOURCE_REPO")

    issue = issue_client.fetch_issue(source_repo, parse_issue_number(env))
    return {
        "issue_number": issue.number,
        "issue_title": issue.title,
        "issue_body": issue.body,
        "issue_url": issue.url,
        "labels": issue.labels,
        "target_repo": target_repo,
    }


def select_github_token(env: Mapping[str, str]) -> str:
    token = env.get("IAC_SMITH_GITHUB_TOKEN") or env.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("IAC_SMITH_GITHUB_TOKEN or GITHUB_TOKEN must be set.")
    return token


def select_target_repo_token(env: Mapping[str, str]) -> str:
    token = env.get("IAC_SMITH_TARGET_REPO_TOKEN") or env.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("IAC_SMITH_TARGET_REPO_TOKEN or GITHUB_TOKEN must be set.")
    return token


def _git_auth_header(token: str) -> str:
    import base64

    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {encoded}"


def _run(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _log(message: str) -> None:
    print(message, flush=True)


def clone_target_repo(target_repo: str, token: str, destination: Path) -> Path:
    repo_path = destination / target_repo.split("/")[-1]
    if repo_path.exists():
        import shutil

        shutil.rmtree(repo_path)

    # Use URL-based auth for headless reliability
    repo_url = f"https://x-access-token:{token}@github.com/{target_repo}.git"
    _run(["git", "clone", repo_url, str(repo_path)])
    return repo_path


def push_branch(repo_path: Path, branch: str, token: str) -> None:
    _run(
        [
            "git",
            "-c",
            f"http.https://github.com/.extraheader={_git_auth_header(token)}",
            "push",
            "-f",
            "-u",
            "origin",
            branch,
        ],
        cwd=repo_path,
    )


def _target_repo_path(env: Mapping[str, str], target_repo: str, token: str) -> Path:
    explicit_path = env.get("IAC_SMITH_TARGET_REPO_PATH")
    if explicit_path:
        return Path(explicit_path)
    workdir = Path(env.get("IAC_SMITH_WORKDIR") or tempfile.mkdtemp(prefix="iac-smith-"))
    return clone_target_repo(target_repo, token, workdir)


def _runtime_repair_attempts(env: Mapping[str, str]) -> int:
    value = env.get("IAC_SMITH_RUNTIME_REPAIR_ATTEMPTS", "2")
    try:
        attempts = int(value)
    except ValueError as exc:
        raise SystemExit("IAC_SMITH_RUNTIME_REPAIR_ATTEMPTS must be an integer.") from exc
    return max(0, attempts)


def _repair_generated_files(
    *,
    repairer: RuntimeRepairer,
    result: IaCSmithState,
    repair_errors: list[str],
) -> dict[str, str]:
    return repairer.repair_files(
        intent=result["intent"],
        change_plan=result["change_plan"],
        repo_patterns=result["repo_patterns"],
        ruleset=result.get("ruleset"),
        target_repo=result["target_repo"],
        generated_files=result["generated_files"],
        repair_errors=repair_errors,
    )


def run_iac_smith(
    env: Mapping[str, str],
    issue_client: IssueClient,
    pr_client: PullRequestClient,
    intent_parser_fn: IntentParser | None = None,
    file_generator_fn: FileGenerator | None = None,
) -> IaCSmithRunResult:
    target_repo = validate_allowed_target_repo(env)
    _log(f"IaC Smith: target repo allowed: {target_repo}")
    target_token = env.get("IAC_SMITH_TARGET_REPO_TOKEN") or env.get("GITHUB_TOKEN") or ""
    if env.get("IAC_SMITH_TARGET_REPO_PATH"):
        repo_path = Path(env["IAC_SMITH_TARGET_REPO_PATH"])
    else:
        _log(f"IaC Smith: cloning target repo {target_repo}.")
        repo_path = _target_repo_path(env, target_repo, target_token)
    _log(f"IaC Smith: using target repo path {repo_path}.")

    state = build_initial_state(env, issue_client=issue_client)
    _log(f"IaC Smith: fetched issue #{state.get('issue_number')}: {state.get('issue_title')}")
    state["target_repo_path"] = str(repo_path)
    runtime_repairer: RuntimeRepairer | None = None
    if file_generator_fn:
        selected_file_generator = file_generator_fn
        if hasattr(file_generator_fn, "repair_files"):
            runtime_repairer = file_generator_fn  # type: ignore[assignment]
    else:
        generator = BedrockTerraformGenerator(logger=_log)
        selected_file_generator = generator.generate_files
        runtime_repairer = generator
    graph = (
        build_graph(
            intent_parser_fn=intent_parser_fn,
            file_generator_fn=selected_file_generator,
        )
        if intent_parser_fn
        else build_graph(file_generator_fn=selected_file_generator)
    )
    _log("IaC Smith: running graph.")
    result = cast(IaCSmithState, graph.invoke(state))
    _log(f"IaC Smith: graph finished with status {result.get('status')}.")

    if result.get("status") in {"ignored", "blocked"}:
        validation = result.get("validation")
        validation_errors = "; ".join(validation.errors) if validation else ""
        return IaCSmithRunResult(
            status=result["status"],
            block_reason=result.get("block_reason") or validation_errors,
        )

    branch = branch_name_for_issue(result["issue_number"], result["issue_title"])
    _log(f"IaC Smith: creating branch {branch}.")
    create_branch(repo_path, branch)
    _log(f"IaC Smith: writing {len(result['generated_files'])} generated file(s).")
    apply_generated_files(repo_path, result["generated_files"])

    if env.get("IAC_SMITH_SKIP_RUNTIME_VALIDATION") != "1":
        _log("IaC Smith: ensuring terraform/terragrunt versions for target repo.")
        version_env = ensure_terraform_terragrunt(repo_path)

        max_runtime_repairs = _runtime_repair_attempts(env)
        for repair_attempt in range(max_runtime_repairs + 1):
            _log("IaC Smith: running Terraform/Terragrunt validation and plan before commit.")
            runtime_validation = validate_generated_iac(repo_path, env_override=version_env)
            if runtime_validation.passed:
                if repair_attempt:
                    _log(
                        "IaC Smith: Terraform/Terragrunt validation and plan passed "
                        "after runtime repair."
                    )
                else:
                    _log("IaC Smith: Terraform/Terragrunt validation and plan passed.")
                break

            block_reason = "; ".join(runtime_validation.errors)
            _log(f"IaC Smith: runtime validation failed: {block_reason}")
            if repair_attempt >= max_runtime_repairs or runtime_repairer is None:
                return IaCSmithRunResult(status="blocked", branch=branch, block_reason=block_reason)

            _log(
                "IaC Smith: asking Bedrock to repair Terraform/Terragrunt output "
                f"from runtime failure ({repair_attempt + 1}/{max_runtime_repairs})."
            )
            repair_errors = list(runtime_validation.errors)
            repaired_files = _repair_generated_files(
                repairer=runtime_repairer,
                result=result,
                repair_errors=repair_errors,
            )
            static_check = static_review_generated_files(repaired_files)
            if static_check.status == ValidationStatus.FAILED:
                _log(
                    "IaC Smith: static review failed after runtime repair: "
                    + "; ".join(static_check.errors)
                )
                repaired_files = _repair_generated_files(
                    repairer=runtime_repairer,
                    result=result,
                    repair_errors=[*repair_errors, *static_check.errors],
                )
            result["generated_files"] = repaired_files
            apply_generated_files(repo_path, repaired_files)

    commit_message = f"feat: generate IaC for issue #{result['issue_number']}"
    _log("IaC Smith: committing generated files.")
    committed = commit_generated_files(repo_path, commit_message)
    if not committed:
        _log("IaC Smith: no generated file changes to commit.")
        return IaCSmithRunResult(status="no_changes", branch=branch)

    if env.get("IAC_SMITH_SKIP_PUSH") != "1":
        if not target_token:
            raise SystemExit("IAC_SMITH_TARGET_REPO_TOKEN or GITHUB_TOKEN must be set for push.")
        _log(f"IaC Smith: pushing branch {branch}.")
        push_branch(repo_path, branch, target_token)

    _log("IaC Smith: opening pull request.")
    pr = pr_client.create_pull_request(
        repo=target_repo,
        title=f"feat: generate IaC for issue #{result['issue_number']}",
        body=result["pr_body"] or "",
        head=branch,
        base="main",
    )
    return IaCSmithRunResult(status="pr_created", branch=branch, pr_url=pr.url, pr_number=pr.number)


def main() -> None:
    env = os.environ
    github_token = select_github_token(env)
    target_token = select_target_repo_token(env)
    result = run_iac_smith(
        env,
        issue_client=GitHubIssueClient(token=github_token),
        pr_client=GitHubPullRequestClient(token=target_token),
    )
    if result.status in {"ignored", "blocked", "no_changes"}:
        message = f"IaC Smith finished with status `{result.status}`: {result.block_reason or ''}"
        raise SystemExit(message)
    print(f"Created PR: {result.pr_url}")


if __name__ == "__main__":
    main()
