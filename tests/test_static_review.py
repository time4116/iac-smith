from iac_smith.nodes.static_review import static_review_generated_files


def test_static_review_blocks_hardcoded_terragrunt_state_key():
    result = static_review_generated_files(
        {
            "live/non-prod/example/terragrunt.hcl": (
                'remote_state { config = { key = "fixed.tfstate" } }'
            )
        }
    )

    assert result.status.value == "failed"
    assert any("path_relative_to_include" in error for error in result.errors)


def test_static_review_blocks_obvious_secret_material():
    fake_access_key = "AKIA" + ("A" * 16)
    result = static_review_generated_files(
        {"modules/example/main.tf": f'variable "x" {{ default = "{fake_access_key}" }}'}
    )

    assert result.status.value == "failed"
    assert any("secret" in error.lower() for error in result.errors)


def test_static_review_warns_when_module_readme_missing_terraform_docs_markers():
    result = static_review_generated_files({"modules/example/README.md": "# Example\n"})

    assert result.status.value == "partial"
    assert any("terraform-docs" in warning for warning in result.warnings)


def test_static_review_passes_safe_minimal_generated_files():
    terragrunt = (
        'remote_state { config = { key = "${path_relative_to_include()}/terraform.tfstate" } }'
    )
    result = static_review_generated_files(
        {
            "live/non-prod/example/terragrunt.hcl": terragrunt,
            "modules/example/README.md": (
                "# Example\n<!-- BEGIN_TF_DOCS -->\n<!-- END_TF_DOCS -->\n"
            ),
        }
    )

    assert result.status.value == "passed"
    assert result.errors == []
