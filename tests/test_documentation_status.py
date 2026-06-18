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

    assert "## Architecture and security model" in readme
    assert "controller repository" in readme
    assert "target infrastructure repository" in readme
    assert "human PR review" in readme
    assert "## Security checks" in readme
    assert "owner-gated workflow trigger" in readme
    assert "repository allowlist" in readme
    assert "Secret-pattern scan" in readme
    assert "dangerous public ingress" in readme
    assert "Terraform/Terragrunt validation" in readme


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
