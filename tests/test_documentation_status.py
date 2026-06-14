from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_discloses_scaffold_status_before_full_mvp():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "## Current status" in readme
    assert "hardened controller scaffold" in readme
    assert "Full issue-to-PR execution is not implemented yet" in readme


def test_root_setup_points_to_detailed_setup_and_required_configuration():
    setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")

    assert "docs/SETUP.md" in setup
    assert "IAC_SMITH_TARGET_REPO_PAT" in setup
    assert "AWS_BEDROCK_ROLE_ARN" in setup
    assert "AWS_REGION" in setup
    assert "IAC_SMITH_ALLOWED_TARGET_REPO" in setup
