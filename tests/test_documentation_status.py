from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_discloses_mvp_status_and_bedrock_requirement():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Bedrock" in readme
    assert "Terraform" in readme
    assert "BEDROCK_MODEL_ID" in readme
    assert "SETUP.md" in readme


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
