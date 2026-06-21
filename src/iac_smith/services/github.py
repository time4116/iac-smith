from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str
    url: str
    labels: list[str]


@dataclass(frozen=True)
class GitHubPullRequest:
    number: int
    url: str


class GitHubIssueClient:
    def __init__(self, token: str, http_client: httpx.Client | None = None) -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self._token = token
        self._http_client = http_client or httpx.Client(timeout=30.0)

    def fetch_issue(self, repo: str, issue_number: int) -> GitHubIssue:
        response = self._http_client.get(
            f"https://api.github.com/repos/{repo}/issues/{issue_number}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if "pull_request" in payload:
            raise ValueError("GitHub issue intake must receive an issue, not a pull request")
        return GitHubIssue(
            number=int(payload["number"]),
            title=payload["title"],
            body=payload.get("body") or "",
            url=payload["html_url"],
            labels=[label["name"] for label in payload.get("labels", [])],
        )

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        response = self._http_client.post(
            f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"body": body},
        )
        response.raise_for_status()


class GitHubPullRequestClient:
    def __init__(self, token: str, http_client: httpx.Client | None = None) -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self._token = token
        self._http_client = http_client or httpx.Client(timeout=30.0)

    def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> GitHubPullRequest:
        # Idempotency check: see if a pull request already exists for this branch
        owner = repo.split("/")[0]
        # Query matching open pull requests
        check_response = self._http_client.get(
            f"https://api.github.com/repos/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={"head": f"{owner}:{head}", "state": "open", "base": base},
        )
        if check_response.status_code == 200:
            existing_prs = check_response.json()
            if existing_prs:
                payload = existing_prs[0]
                return GitHubPullRequest(number=int(payload["number"]), url=payload["html_url"])

        response = self._http_client.post(
            f"https://api.github.com/repos/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body, "head": head, "base": base},
        )
        response.raise_for_status()
        payload = response.json()
        return GitHubPullRequest(number=int(payload["number"]), url=payload["html_url"])
