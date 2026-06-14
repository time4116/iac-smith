import os

import pytest

from iac_smith.cli import validate_allowed_target_repo


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
