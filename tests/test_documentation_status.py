from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_discloses_mvp_status_and_bedrock_requirement():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Bedrock" in readme
    assert "Terraform" in readme
    assert "BEDROCK_MODEL_ID" in readme
    assert "SETUP.md" in readme


def test_readme_documents_architecture_security_model_and_checks():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lower = readme.lower()

    # Structural anchors: these sections must exist. Heading match is
    # case-insensitive so capitalization tweaks don't break the guard.
    assert "## architecture and security model" in lower
    assert "## security checks" in lower

    # Topics the README must still cover. Each is matched case-insensitively
    # against a stable keyword rather than an exact sentence, so rewording or
    # reformatting the prose doesn't trip the test — only dropping the topic
    # entirely does. Keep keywords short and specific.
    required_topics = {
        "two-repo model (controller)": "controller repositor",
        "two-repo model (target)": "target infrastructure repositor",
        "human approval gate": "pr review",
        "owner-gated trigger": "owner-gated",
        "target repo allowlist": "allowlist",
        "secret scanning": "secret",
        "dangerous public ingress check": "ingress",
        "terraform/terragrunt validation": "validation",
    }
    missing = sorted(name for name, keyword in required_topics.items() if keyword not in lower)
    assert not missing, f"README no longer documents: {missing}"


def test_root_setup_points_to_detailed_setup_and_required_configuration():
    setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")

    assert "docs/SETUP.md" in setup
    assert "IAC_SMITH_TARGET_REPO_PAT" in setup
    assert "BEDROCK_MODEL_ID" in setup
    assert "AWS_ROLE_ARN_NON_PROD" in setup
    assert "AWS_ROLE_ARN_PROD" in setup
    assert "AWS_BEDROCK_ROLE_ARN" not in setup
    assert "IAC_SMITH_ALLOWED_TARGET_REPO" in setup


def test_controller_workflow_standardizes_on_environment_role_secrets():
    workflow = (ROOT / ".github/workflows/issue-to-pr.yml").read_text(encoding="utf-8")

    assert "secrets.AWS_ROLE_ARN_NON_PROD" in workflow
    assert "vars.AWS_BEDROCK_ROLE_ARN" not in workflow
