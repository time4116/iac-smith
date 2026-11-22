import base64
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from iac_smith.cli import (
    IaCSmithRunResult,
    build_initial_state,
    clone_target_repo,
    run_iac_smith,
    select_github_token,
    validate_allowed_target_repo,
)
from iac_smith.services.github import GitHubIssue


def test_validate_allowed_target_repo_passes_exact_match(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")

    assert validate_allowed_target_repo(os.environ) == "time4116/iac-smith-demo-infra"


def test_validate_allowed_target_repo_fails_closed_on_mismatch(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "attacker/repo")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")

    with pytest.raises(SystemExit, match="not allowed"):
        validate_allowed_target_repo(os.environ)


def test_validate_allowed_target_repo_requires_allowlist(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.delenv("IAC_SMITH_ALLOWED_TARGET_REPO", raising=False)

    with pytest.raises(SystemExit, match="IAC_SMITH_ALLOWED_TARGET_REPO"):
        validate_allowed_target_repo(os.environ)


class FakeIssueClient:
    def __init__(self):
        self.calls = []

    def fetch_issue(self, repo: str, issue_number: int) -> GitHubIssue:
        self.calls.append((repo, issue_number))
        return GitHubIssue(
            number=12,
            title="Create EKS Fargate",
            body="Create a non-prod EKS Fargate setup in us-west-2.",
            url="https://github.com/time4116/iac-smith/issues/12",
            labels=["iac-smith"],
        )


def test_build_initial_state_fetches_source_issue_and_target_repo(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_SOURCE_REPO", "time4116/iac-smith")
    monkeypatch.setenv("IAC_SMITH_ISSUE_NUMBER", "12")
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")
    issue_client = FakeIssueClient()

    state = build_initial_state(os.environ, issue_client=issue_client)

    assert issue_client.calls == [("time4116/iac-smith", 12)]
    assert state["issue_number"] == 12
    assert state["issue_title"] == "Create EKS Fargate"
    assert state["issue_body"] == "Create a non-prod EKS Fargate setup in us-west-2."
    assert state["issue_url"] == "https://github.com/time4116/iac-smith/issues/12"
    assert state["labels"] == ["iac-smith"]
    assert state["target_repo"] == "time4116/iac-smith-demo-infra"


def test_build_initial_state_requires_integer_issue_number(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_SOURCE_REPO", "time4116/iac-smith")
    monkeypatch.setenv("IAC_SMITH_ISSUE_NUMBER", "abc")
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")

    with pytest.raises(SystemExit, match="IAC_SMITH_ISSUE_NUMBER"):
        build_initial_state(os.environ, issue_client=FakeIssueClient())


def test_build_initial_state_rejects_malformed_source_repo(monkeypatch):
    monkeypatch.setenv("IAC_SMITH_SOURCE_REPO", "https://github.com/time4116/iac-smith")
    monkeypatch.setenv("IAC_SMITH_ISSUE_NUMBER", "12")
    monkeypatch.setenv("IAC_SMITH_TARGET_REPO", "time4116/iac-smith-demo-infra")
    monkeypatch.setenv("IAC_SMITH_ALLOWED_TARGET_REPO", "time4116/iac-smith-demo-infra")

    with pytest.raises(SystemExit, match="IAC_SMITH_SOURCE_REPO"):
        build_initial_state(os.environ, issue_client=FakeIssueClient())


def test_select_github_token_prefers_project_specific_token():
    token = select_github_token(
        {
            "GITHUB_TOKEN": "ambient-token",
            "IAC_SMITH_GITHUB_TOKEN": "workflow-token",
        }
    )

    assert token == "workflow-token"


def test_push_branch_keeps_token_out_of_argv_and_uses_non_force_push(tmp_path, monkeypatch):
    from iac_smith.cli import push_branch

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/example/repo.git"],
        check=True,
    )
    captured = []
    real_run = subprocess.run

    def inspect_run(command, **kwargs):
        if command[:2] == ["git", "push"]:
            captured.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, **kwargs)

    monkeypatch.setattr("iac_smith.cli.subprocess.run", inspect_run)
    push_branch(repo, "my-branch", "test-secret")

    assert len(captured) == 1
    command, kwargs = captured[0]
    assert command == ["git", "push", "-u", "origin", "my-branch"]
    assert "test-secret" not in repr(command)
    assert kwargs["cwd"] == repo
    assert kwargs["check"] is True
    env = kwargs["env"]
    entries = [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    ]
    assert entries[-4:-1] == [
        ("http.extraheader", ""),
        ("http.https://github.com/.extraheader", ""),
        ("http.https://github.com/example/repo.git.extraheader", ""),
    ]
    assert entries[-1][0] == "http.https://github.com/example/repo.git.extraheader"
    assert base64.b64decode(entries[-1][1].split()[-1]) == b"x-access-token:test-secret"


def test_push_branch_rejects_non_fast_forward_without_rewriting_remote(tmp_path, monkeypatch):
    from iac_smith.cli import push_branch

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    first = tmp_path / "first"
    subprocess.run(["git", "init", "-q", "-b", "feature", str(first)], check=True)
    subprocess.run(["git", "-C", str(first), "remote", "add", "origin", str(bare)], check=True)

    def commit(repo, value):
        (repo / "change.txt").write_text(value)
        subprocess.run(["git", "-C", str(repo), "add", "change.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "-qm",
                value,
            ],
            check=True,
        )

    commit(first, "base")
    push_branch(first, "feature", "test-secret")
    second = tmp_path / "second"
    subprocess.run(["git", "clone", "-q", "-b", "feature", str(bare), str(second)], check=True)
    commit(second, "remote change")
    push_branch(second, "feature", "test-secret")
    remote_head = subprocess.check_output(
        ["git", "-C", str(bare), "rev-parse", "refs/heads/feature"]
    ).strip()
    commit(first, "local divergent change")
    with pytest.raises(subprocess.CalledProcessError):
        push_branch(first, "feature", "test-secret")
    assert (
        subprocess.check_output(["git", "-C", str(bare), "rev-parse", "refs/heads/feature"]).strip()
        == remote_head
    )
    assert (
        subprocess.check_output(["git", "-C", str(second), "rev-parse", "HEAD"]).strip()
        == remote_head
    )


@pytest.mark.parametrize("inherited_scope", ["global", "environment"])
@pytest.mark.parametrize("header_scope", ["generic", "host", "repository"])
def test_push_http_probe_sends_only_target_authorization(
    tmp_path, monkeypatch, inherited_scope, header_scope
):
    from iac_smith.cli import push_branch

    received = []

    class Probe(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get_all("Authorization") or [])
            self.send_response(401)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Probe)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", "-b", "feature", str(repo)], check=True)
        (repo / "README.md").write_text("fixture\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        url = f"http://127.0.0.1:{server.server_port}/repo.git"
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)
        key = {
            "generic": "http.extraheader",
            "host": f"http.http://127.0.0.1:{server.server_port}/.extraheader",
            "repository": f"http.{url}.extraheader",
        }[header_scope]
        config = tmp_path / "global.gitconfig"
        if inherited_scope == "global":
            subprocess.run(
                [
                    "git",
                    "config",
                    "--file",
                    str(config),
                    "--add",
                    key,
                    "Authorization: Bearer inherited",
                ],
                check=True,
            )
        else:
            monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
            monkeypatch.setenv("GIT_CONFIG_KEY_0", key)
            monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Bearer inherited")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
        with pytest.raises(subprocess.CalledProcessError):
            push_branch(repo, "feature", "target-secret")
        intended = "basic " + base64.b64encode(b"x-access-token:target-secret").decode()
        assert received
        assert received[0] == [intended]
        assert "target-secret" not in (repo / ".git/config").read_text()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_clone_uses_ephemeral_basic_auth_and_does_not_store_token(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "README.md").write_text("fixture\n")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    # Rewrite only the test URL locally so git clone executes without network access.
    config = tmp_path / "global.gitconfig"
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(config),
            f"url.file://{source}.insteadOf",
            "https://github.com/example/repo.git",
        ],
        check=True,
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    token = "test-secret-do-not-persist"
    real_run = subprocess.run
    captured = []

    def inspect_run(command, **kwargs):
        if command[:2] == ["git", "clone"]:
            captured.append((command, kwargs.get("env")))
        return real_run(command, **kwargs)

    monkeypatch.setattr("iac_smith.cli.subprocess.run", inspect_run)
    checkout = clone_target_repo("example/repo", token, tmp_path / "checkout")
    assert (checkout / "README.md").read_text() == "fixture\n"
    assert len(captured) == 1
    command, child_env = captured[0]
    assert token not in " ".join(command)
    assert command[2] == "https://github.com/example/repo.git"
    assert child_env["GIT_CONFIG_COUNT"] == "4"
    assert child_env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert child_env["GIT_CONFIG_VALUE_0"] == ""
    assert child_env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
    assert child_env["GIT_CONFIG_VALUE_1"] == ""
    assert child_env["GIT_CONFIG_KEY_2"] == "http.https://github.com/example/repo.git.extraheader"
    assert child_env["GIT_CONFIG_VALUE_2"] == ""
    assert child_env["GIT_CONFIG_KEY_3"] == "http.https://github.com/example/repo.git.extraheader"
    assert child_env["GIT_CONFIG_VALUE_3"].startswith("AUTHORIZATION: basic ")
    encoded = child_env["GIT_CONFIG_VALUE_3"].split(" ")[-1]
    assert base64.b64decode(encoded).decode() == f"x-access-token:{token}"
    assert token not in (checkout / ".git/config").read_text()
    remote = subprocess.run(
        ["git", "-C", str(checkout), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert token not in remote


@pytest.mark.parametrize("inherited_scope", ["global", "environment"])
@pytest.mark.parametrize("url_scoped", [False, True, "repository"])
def test_clone_http_probe_sends_only_target_authorization(
    tmp_path, monkeypatch, inherited_scope, url_scoped
):
    received = []

    class Probe(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get_all("Authorization") or [])
            self.send_response(401)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Probe)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "global.gitconfig"
        inherited = "Authorization: Bearer inherited-credential"
        probe_url = f"http://127.0.0.1:{server.server_port}/"
        inherited_key = (
            f"http.{probe_url}repo.git.extraheader"
            if url_scoped == "repository"
            else f"http.{probe_url}.extraheader"
            if url_scoped
            else "http.extraheader"
        )
        if inherited_scope == "global":
            subprocess.run(
                ["git", "config", "--file", str(config), "--add", inherited_key, inherited],
                check=True,
            )
        else:
            monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
            monkeypatch.setenv("GIT_CONFIG_KEY_0", inherited_key)
            monkeypatch.setenv("GIT_CONFIG_VALUE_0", inherited)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
        real_run = subprocess.run

        def probe_run(command, **kwargs):
            if command[:2] != ["git", "clone"]:
                return real_run(command, **kwargs)
            assert command[2] == "https://github.com/example/repo.git"
            assert "target-secret" not in " ".join(command)
            # The local HTTP probe has a different URL. Translate the scoped key
            # for the probe only; Git still processes inherited and appended entries.
            env = kwargs["env"].copy()
            for index in range(int(env["GIT_CONFIG_COUNT"])):
                key = f"GIT_CONFIG_KEY_{index}"
                if env[key] == "http.https://github.com/.extraheader":
                    env[key] = f"http.{probe_url}.extraheader"
                elif env[key] == "http.https://github.com/example/repo.git.extraheader":
                    env[key] = f"http.{probe_url}repo.git.extraheader"
            return real_run(
                [*command[:2], f"http://127.0.0.1:{server.server_port}/repo.git", command[3]],
                **{**kwargs, "env": env},
            )

        monkeypatch.setattr("iac_smith.cli.subprocess.run", probe_run)
        with pytest.raises(subprocess.CalledProcessError):
            clone_target_repo("example/repo", "target-secret", tmp_path / "checkout")
        intended = "basic " + base64.b64encode(b"x-access-token:target-secret").decode()
        assert received
        assert received[0] == [intended]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_clone_refuses_existing_caller_owned_repo_without_deleting_it(tmp_path):
    repo = tmp_path / "caller-owned" / "repo"
    (repo / ".git").mkdir(parents=True)
    sentinel = repo / "keep.txt"
    sentinel.write_text("do not delete")
    with pytest.raises(FileExistsError):
        clone_target_repo("example/repo", "target-secret", repo.parent)
    assert sentinel.read_text() == "do not delete"
    assert (repo / ".git").is_dir()


@pytest.mark.parametrize("raises", [False, True])
def test_implicit_checkout_is_removed_after_run(tmp_path, monkeypatch, raises):
    monkeypatch.setattr("iac_smith.cli.tempfile.tempdir", str(tmp_path))
    paths = []

    def fake_core(env, **kwargs):
        path = Path(env["IAC_SMITH_WORKDIR"])
        paths.append(path)
        assert path.is_dir()
        if raises:
            raise RuntimeError("failed run")
        return IaCSmithRunResult(status="ignored")

    monkeypatch.setattr("iac_smith.cli._run_iac_smith_core", fake_core)
    if raises:
        with pytest.raises(RuntimeError, match="failed run"):
            run_iac_smith({}, FakeIssueClient(), None)
    else:
        run_iac_smith({}, FakeIssueClient(), None)
    assert len(paths) == 1
    assert not paths[0].exists()


def test_explicit_checkout_is_not_removed(tmp_path, monkeypatch):
    workdir = tmp_path / "caller-owned"
    workdir.mkdir()
    monkeypatch.setattr(
        "iac_smith.cli._run_iac_smith_core",
        lambda env, **kwargs: IaCSmithRunResult(status="ignored"),
    )
    run_iac_smith({"IAC_SMITH_WORKDIR": str(workdir)}, FakeIssueClient(), None)
    assert workdir.is_dir()
