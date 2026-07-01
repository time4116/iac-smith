from iac_smith.blackboard import (
    ContractResolver,
    TerraformContract,
    extract_resource_types,
    normalize_validation_findings,
    resolve_contracts_for_files,
)


def test_contract_candidates_are_extracted_from_generated_resources_not_service_keywords():
    generated = {
        "modules/custom/main.tf": (
            'resource "customcloud_widget" "this" {\n  name = "example"\n}\n'
            'resource "aws_rds_cluster" "this" {\n  engine = "aurora-postgresql"\n}\n'
        )
    }
    resolver = ContractResolver(
        provider_contracts={
            "customcloud_widget": TerraformContract(
                kind="provider_resource",
                name="customcloud_widget",
                allowed_arguments=["name"],
                source="fixture",
            ),
            "aws_rds_cluster": TerraformContract(
                kind="provider_resource",
                name="aws_rds_cluster",
                allowed_arguments=["engine"],
                source="fixture",
            ),
        }
    )

    assert extract_resource_types(generated.values()) == ["customcloud_widget", "aws_rds_cluster"]
    assert sorted(resolve_contracts_for_files(generated, resolver)) == [
        "aws_rds_cluster",
        "customcloud_widget",
    ]


def test_hallucinated_resource_type_finding_suggests_nearest_valid_types():
    error = (
        "`modules/aurora/main.tf` declares unsupported resource type "
        "`aws_db_proxy_target_group` — the provider does not define it."
    )
    known_types = {
        "aws_db_proxy",
        "aws_db_proxy_default_target_group",
        "aws_db_proxy_endpoint",
        "aws_rds_cluster",
        "aws_kms_key",
    }

    findings = normalize_validation_findings(error.splitlines(), known_resource_types=known_types)

    assert len(findings) == 1
    pattern = findings[0].negative_pattern
    assert "aws_db_proxy_target_group" in pattern
    # The correction points at the real type, not an unrelated one.
    assert "aws_db_proxy_default_target_group" in pattern
    assert "aws_rds_cluster" not in pattern


def test_hallucinated_resource_type_finding_omits_suggestion_without_known_types():
    error = "`modules/x/main.tf` declares unsupported resource type `made_up_thing`."

    findings = normalize_validation_findings(error.splitlines())

    assert len(findings) == 1
    assert "closest resource types" not in findings[0].negative_pattern
