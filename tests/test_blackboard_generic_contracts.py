from iac_smith.blackboard import (
    ContractResolver,
    TerraformContract,
    extract_resource_types,
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
