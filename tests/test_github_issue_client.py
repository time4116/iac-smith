import httpx
import pytest

from iac_smith.services.github import GitHubIssue, GitHubIssueClient


def test_github_issue_client_fetches_issue_metadata_without_comments():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://api.github.com/repos/time4116/iac-smith/issues/12"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "number": 12,
                "title": "Create EKS Fargate",
                "body": "Create non-prod EKS Fargate in us-west-2.",
                "html_url": "https://github.com/time4116/iac-smith/issues/12",
                "labels": [{"name": "iac-smith"}, {"name": "infra"}],
            },
        )

    client = GitHubIssueClient(
        token="test-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    issue = client.fetch_issue("time4116/iac-smith", 12)

    assert issue == GitHubIssue(
        number=12,
        title="Create EKS Fargate",
        body="Create non-prod EKS Fargate in us-west-2.",
        url="https://github.com/time4116/iac-smith/issues/12",
        labels=["iac-smith", "infra"],
    )


def test_github_issue_client_rejects_pull_request_payloads():
    client = GitHubIssueClient(
        token="test-token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "number": 7,
                        "title": "PR",
                        "body": "not an issue",
                        "html_url": "https://github.com/time4116/iac-smith/pull/7",
                        "labels": [],
                        "pull_request": {"url": "https://api.github.com/repos/x/y/pulls/7"},
                    },
                )
            )
        ),
    )

    with pytest.raises(ValueError, match="pull request"):
        client.fetch_issue("time4116/iac-smith", 7)
