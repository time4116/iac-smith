import httpx

from iac_smith.services.github import GitHubPullRequestClient


def test_create_pull_request_posts_to_target_repo():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and "/pulls" in request.url.path:
            return httpx.Response(200, json=[])
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
    # The first request is now a GET to check for existing PRs
    assert requests[0].method == "GET"
    q_str = requests[0].url.query.decode()
    assert "head=iac-smith/issue-1-test" in q_str or f"head={q_str}"
    # The second request is the POST
    assert requests[1].method == "POST"
    assert requests[1].url.path == "/repos/o/r/pulls"
    assert requests[1].headers["authorization"] == "Bearer token"
    assert b'"head":"iac-smith/issue-1-test"' in requests[1].content


def test_create_pull_request_returns_existing_when_present():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and "/pulls" in request.url.path:
            return httpx.Response(
                200, json=[{"number": 42, "html_url": "https://github.com/o/r/pull/42"}]
            )
        return httpx.Response(500, json={"message": "Should not be called"})

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

    assert pr.number == 42
    assert pr.url == "https://github.com/o/r/pull/42"
    assert len(requests) == 1
    assert requests[0].method == "GET"


def test_create_pull_request_recovers_existing_pr_after_duplicate_422():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        get_count = sum(1 for item in requests if item.method == "GET")
        if request.method == "GET" and get_count == 1:
            return httpx.Response(200, json=[])
        if request.method == "POST":
            return httpx.Response(422, json={"message": "A pull request already exists"})
        if request.method == "GET":
            return httpx.Response(
                200, json=[{"number": 42, "html_url": "https://github.com/o/r/pull/42"}]
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

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

    assert pr.number == 42
    assert pr.url == "https://github.com/o/r/pull/42"
    assert [request.method for request in requests] == ["GET", "POST", "GET"]
