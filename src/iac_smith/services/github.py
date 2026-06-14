from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str
    url: str
    labels: list[str]


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
