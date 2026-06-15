from pathlib import Path

from iac_smith.repo_scanner import scan_repo_patterns


def test_scan_empty_repo_uses_iac_smith_defaults(tmp_path: Path):
    patterns = scan_repo_patterns(tmp_path)

    assert patterns.uses_terragrunt is False
    assert patterns.environments == []
    assert patterns.module_sources == []
    assert patterns.default_environment_names == ["non-prod", "prod"]
    assert patterns.preferred_layout == "iac_smith_default"


def test_scan_existing_terragrunt_repo_detects_envs_module_sources_and_backend(tmp_path: Path):
    root = tmp_path
    (root / "live" / "dev" / "vpc").mkdir(parents=True)
    (root / "live" / "prod" / "vpc").mkdir(parents=True)
    (root / "live" / "terragrunt.hcl").write_text(
        'remote_state {\n  backend = "s3"\n  config = {\n'
        '    key = "${path_relative_to_include()}/terraform.tfstate"\n  }\n}\n'
    )
    (root / "live" / "dev" / "vpc" / "terragrunt.hcl").write_text(
        'terraform { source = "../../../modules/network" }\n'
    )
    (root / "modules" / "network").mkdir(parents=True)
    (root / "modules" / "network" / "main.tf").write_text(
        'module "vpc" { source = "terraform-aws-modules/vpc/aws" version = "~> 5.0" }\n'
    )

    patterns = scan_repo_patterns(root)

    assert patterns.uses_terragrunt is True
    assert patterns.environments == ["dev", "prod"]
    assert patterns.default_environment_names == ["dev", "prod"]
    assert patterns.preferred_layout == "terragrunt_live_modules"
    assert "terraform-aws-modules/vpc/aws" in patterns.module_sources
    assert patterns.remote_state_uses_path_relative_to_include is True
    assert "modules/network/main.tf" in patterns.representative_files
    assert (
        "terraform-aws-modules/vpc/aws" in patterns.representative_files["modules/network/main.tf"]
    )
