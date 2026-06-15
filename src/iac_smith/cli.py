import os
import re
from collections.abc import Mapping
from typing import Protocol

from iac_smith.services.github import GitHubIssue, GitHubIssueClient
from iac_smith.state import IaCSmithState


class IssueClient(Protocol):
    def fetch_issue(self, repo: str, issue_number: int) -> GitHubIssue: ...


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


def main() -> None:
    state = build_initial_state(
        os.environ,
        issue_client=GitHubIssueClient(token=select_github_token(os.environ)),
    )
    message = (
        "CLI fetched issue #{issue_number} for target repo `{target_repo}`. Full graph execution "
        "is not implemented yet."
    ).format(**state)
    raise SystemExit(message)


if __name__ == "__main__":
    main()
