from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_discloses_mvp_status_and_bedrock_requirement():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "## Current status" in readme
    assert "issue-to-PR MVP path is implemented" in readme
    assert "use Bedrock to parse infrastructure intent" in readme
    assert "does not hardcode AWS account IDs" in readme


def test_root_setup_points_to_detailed_setup_and_required_configuration():
    setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")

    assert "docs/SETUP.md" in setup
    assert "IAC_SMITH_TARGET_REPO_PAT" in setup
    assert "BEDROCK_MODEL_ID" in setup
    assert "AWS_BEDROCK_ROLE_ARN" in setup
    assert "IAC_SMITH_ALLOWED_TARGET_REPO" in setup
