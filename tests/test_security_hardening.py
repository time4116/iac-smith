from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _workflow(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_ci_workflow_uses_least_privilege_permissions_and_locked_install():
    workflow_text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow = _workflow(".github/workflows/ci.yml")
    steps = workflow["jobs"]["test"]["steps"]

    assert workflow["permissions"] == {"contents": "read"}
    assert "uv sync --locked" in workflow_text
    assert any(step.get("with", {}).get("python-version") == "3.12" for step in steps)


def test_issue_workflow_has_privilege_boundary_before_secrets_and_oidc():
    workflow_text = (ROOT / ".github/workflows/issue-to-pr.yml").read_text(encoding="utf-8")
    workflow = _workflow(".github/workflows/issue-to-pr.yml")
    job = workflow["jobs"]["iac-smith"]
    steps = job["steps"]

    assert workflow["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "issues": "read",
    }
    assert workflow["concurrency"]["group"] == "iac-smith-issue-${{ github.event.issue.number }}"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "github.actor == 'time4116'" in job["if"]
    assert "IAC_SMITH_ALLOWED_TARGET_REPO" in workflow_text
    assert "IAC_SMITH_TARGET_REPO_TOKEN" in workflow_text
    assert "GITHUB_TOKEN: ${{ secrets.IAC_SMITH_TARGET_REPO_PAT }}" not in workflow_text
    assert "uv sync --locked" in workflow_text
    assert any(step.get("with", {}).get("python-version") == "3.12" for step in steps)


def test_issue_workflow_pins_third_party_actions_to_commit_shas():
    workflow = _workflow(".github/workflows/issue-to-pr.yml")
    uses_values = [
        step["uses"] for step in workflow["jobs"]["iac-smith"]["steps"] if "uses" in step
    ]

    for value in uses_values:
        ref = value.rsplit("@", 1)[1]
        assert len(ref) == 40
        assert all(char in "0123456789abcdef" for char in ref.lower())
