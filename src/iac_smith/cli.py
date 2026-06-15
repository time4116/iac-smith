     1|import os
     2|import re
     3|import subprocess
     4|import tempfile
     5|from collections.abc import Mapping
     6|from dataclasses import dataclass
     7|from pathlib import Path
     8|from typing import Protocol
     9|
    10|from iac_smith.graph import IntentParser, build_graph
    11|from iac_smith.nodes.pr_writer import branch_name_for_issue
    12|from iac_smith.services.github import (
    13|    GitHubIssue,
    14|    GitHubIssueClient,
    15|    GitHubPullRequest,
    16|    GitHubPullRequestClient,
    17|)
    18|from iac_smith.state import IaCSmithState
    19|from iac_smith.workspace import apply_generated_files, commit_generated_files, create_branch
    20|
    21|
    22|class IssueClient(Protocol):
    23|    def fetch_issue(self, repo: str, issue_number: int) -> GitHubIssue: ...
    24|
    25|
    26|class PullRequestClient(Protocol):
    27|    def create_pull_request(
    28|        self,
    29|        repo: str,
    30|        title: str,
    31|        body: str,
    32|        head: str,
    33|        base: str = "main",
    34|    ) -> GitHubPullRequest: ...
    35|
    36|
    37|@dataclass(frozen=True)
    38|class IaCSmithRunResult:
    39|    status: str
    40|    branch: str | None = None
    41|    pr_url: str | None = None
    42|    pr_number: int | None = None
    43|    block_reason: str | None = None
    44|
    45|
    46|_REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    47|
    48|
    49|def validate_repo_name(value: str | None, env_name: str) -> str:
    50|    if not value:
    51|        raise SystemExit(f"{env_name} must be set.")
    52|    if not _REPO_NAME_PATTERN.fullmatch(value):
    53|        raise SystemExit(f"{env_name} must use owner/repo format.")
    54|    return value
    55|
    56|
    57|def validate_allowed_target_repo(env: Mapping[str, str]) -> str:
    58|    target_repo = env.get("IAC_SMITH_TARGET_REPO")
    59|    allowed_target_repo = env.get("IAC_SMITH_ALLOWED_TARGET_REPO")
    60|
    61|    if not allowed_target_repo:
    62|        raise SystemExit("IAC_SMITH_ALLOWED_TARGET_REPO must be set; failing closed.")
    63|    target_repo = validate_repo_name(target_repo, "IAC_SMITH_TARGET_REPO")
    64|    allowed_target_repo = validate_repo_name(allowed_target_repo, "IAC_SMITH_ALLOWED_TARGET_REPO")
    65|    if target_repo != allowed_target_repo:
    66|        raise SystemExit(f"Target repo `{target_repo}` is not allowed.")
    67|    return target_repo
    68|
    69|
    70|def parse_issue_number(env: Mapping[str, str]) -> int:
    71|    value = env.get("IAC_SMITH_ISSUE_NUMBER")
    72|    if not value:
    73|        raise SystemExit("IAC_SMITH_ISSUE_NUMBER must be set.")
    74|    try:
    75|        issue_number = int(value)
    76|    except ValueError as exc:
    77|        raise SystemExit("IAC_SMITH_ISSUE_NUMBER must be an integer.") from exc
    78|    if issue_number <= 0:
    79|        raise SystemExit("IAC_SMITH_ISSUE_NUMBER must be positive.")
    80|    return issue_number
    81|
    82|
    83|def build_initial_state(env: Mapping[str, str], issue_client: IssueClient) -> IaCSmithState:
    84|    target_repo = validate_allowed_target_repo(env)
    85|    source_repo = validate_repo_name(env.get("IAC_SMITH_SOURCE_REPO"), "IAC_SMITH_SOURCE_REPO")
    86|
    87|    issue = issue_client.fetch_issue(source_repo, parse_issue_number(env))
    88|    return {
    89|        "issue_number": issue.number,
    90|        "issue_title": issue.title,
    91|        "issue_body": issue.body,
    92|        "issue_url": issue.url,
    93|        "labels": issue.labels,
    94|        "target_repo": target_repo,
    95|    }
    96|
    97|
    98|def select_github_token(env: Mapping[str, str]) -> str:
    99|    token = env.get("IAC_SMITH_GITHUB_TOKEN") or env.get("GITHUB_TOKEN")
   100|    if not token:
   101|        raise SystemExit("IAC_SMITH_GITHUB_TOKEN or GITHUB_TOKEN must be set.")
   102|    return token
   103|
   104|
   105|def select_target_repo_token(env: Mapping[str, str]) -> str:
   106|    token = env.get("IAC_SMITH_TARGET_REPO_TOKEN") or env.get("GITHUB_TOKEN")
   107|    if not token:
   108|        raise SystemExit("IAC_SMITH_TARGET_REPO_TOKEN or GITHUB_TOKEN must be set.")
   109|    return token
   110|
   111|
   112|def _git_auth_header(token: str) -> str:
   113|    import base64
   114|
   115|    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
   116|    return f"AUTHORIZATION: basic {encoded}"
   117|
   118|
   119|def _run(command: list[str], cwd: Path | None = None) -> None:
   120|    subprocess.run(command, cwd=cwd, check=True)
   121|
   122|
   123|
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
   134|    _run(
   135|        [
   136|            "git",
   137|            "-c",
   138|            f"http.https://github.com/.extraheader={_git_auth_header(token)}",
   139|            "push",
   140|            "-u",
   141|            "origin",
   142|            branch,
   143|        ],
   144|        cwd=repo_path,
   145|    )
   146|
   147|
   148|def _target_repo_path(env: Mapping[str, str], target_repo: str, token: str) -> Path:
   149|    explicit_path = env.get("IAC_SMITH_TARGET_REPO_PATH")
   150|    if explicit_path:
   151|        return Path(explicit_path)
   152|    workdir = Path(env.get("IAC_SMITH_WORKDIR") or tempfile.mkdtemp(prefix="iac-smith-"))
   153|    return clone_target_repo(target_repo, token, workdir)
   154|
   155|
   156|def run_iac_smith(
   157|    env: Mapping[str, str],
   158|    issue_client: IssueClient,
   159|    pr_client: PullRequestClient,
   160|    intent_parser_fn: IntentParser | None = None,
   161|) -> IaCSmithRunResult:
   162|    target_repo = validate_allowed_target_repo(env)
   163|    target_token = env.get("IAC_SMITH_TARGET_REPO_TOKEN") or env.get("GITHUB_TOKEN") or ""
   164|    if env.get("IAC_SMITH_TARGET_REPO_PATH"):
   165|        repo_path = Path(env["IAC_SMITH_TARGET_REPO_PATH"])
   166|    else:
   167|        repo_path = _target_repo_path(env, target_repo, target_token)
   168|
   169|    state = build_initial_state(env, issue_client=issue_client)
   170|    state["target_repo_path"] = str(repo_path)
   171|    graph = build_graph(intent_parser_fn=intent_parser_fn) if intent_parser_fn else build_graph()
   172|    result = graph.invoke(state)
   173|
   174|    if result.get("status") in {"ignored", "blocked"}:
   175|        validation = result.get("validation")
   176|        validation_errors = "; ".join(validation.errors) if validation else ""
   177|        return IaCSmithRunResult(
   178|            status=result["status"],
   179|            block_reason=result.get("block_reason") or validation_errors,
   180|        )
   181|
   182|    branch = branch_name_for_issue(result["issue_number"], result["issue_title"])
   183|    create_branch(repo_path, branch)
   184|    apply_generated_files(repo_path, result["generated_files"])
   185|    commit_message = f"feat: generate IaC for issue #{result['issue_number']}"
   186|    committed = commit_generated_files(repo_path, commit_message)
   187|    if not committed:
   188|        return IaCSmithRunResult(status="no_changes", branch=branch)
   189|
   190|    if env.get("IAC_SMITH_SKIP_PUSH") != "1":
   191|        if not target_token:
   192|            raise SystemExit("IAC_SMITH_TARGET_REPO_TOKEN or GITHUB_TOKEN must be set for push.")
   193|        push_branch(repo_path, branch, target_token)
   194|
   195|    pr = pr_client.create_pull_request(
   196|        repo=target_repo,
   197|        title=f"feat: generate IaC for issue #{result['issue_number']}",
   198|        body=result["pr_body"] or "",
   199|        head=branch,
   200|        base="main",
   201|    )
   202|    return IaCSmithRunResult(status="pr_created", branch=branch, pr_url=pr.url, pr_number=pr.number)
   203|
   204|
   205|def main() -> None:
   206|    env = os.environ
   207|    github_token = select_github_token(env)
   208|    target_token = select_target_repo_token(env)
   209|    result = run_iac_smith(
   210|        env,
   211|        issue_client=GitHubIssueClient(token=github_token),
   212|        pr_client=GitHubPullRequestClient(token=target_token),
   213|    )
   214|    if result.status in {"ignored", "blocked", "no_changes"}:
   215|        message = f"IaC Smith finished with status `{result.status}`: {result.block_reason or ''}"
   216|        raise SystemExit(message)
   217|    print(f"Created PR: {result.pr_url}")
   218|
   219|
   220|if __name__ == "__main__":
   221|    main()
   222|