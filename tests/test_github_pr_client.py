import httpx

from iac_smith.services.github import GitHubPullRequestClient


def test_create_pull_request_posts_to_target_repo():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/o/r/pull/7"})

    client = GitHubPullRequestClient(
        token="token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    pr = client.create_pull_request(
        repo="o/r",
        title="feat: test",
        body="body",
        head="iac-smith/issue-1-test",
        base="main",
    )

    assert pr.number == 7
    assert pr.url == "https://github.com/o/r/pull/7"
    assert requests[0].url.path == "/repos/o/r/pulls"
    assert requests[0].headers["authorization"] == "Bearer token"
    assert b'"head":"iac-smith/issue-1-test"' in requests[0].content
