from iac_smith.blackboard import (
    ContractResolver,
    RunBlackboard,
    TerraformContract,
    ValidationFinding,
    build_blackboard,
    build_blackboard_prompt_section,
    contracts_from_provider_schema,
    extract_resource_types,
    normalize_validation_findings,
    resolve_contracts_for_files,
    validate_generated_contracts,
)
from iac_smith.models.repo_patterns import RepoPatterns


def _provider_schema() -> dict:
    return {
        "format_version": "1.0",
        "provider_schemas": {
            "registry.terraform.io/hashicorp/aws": {
                "resource_schemas": {
                    "aws_security_group": {
                        "block": {
                            "attributes": {
                                "name": {"type": "string", "optional": True},
                                "description": {"type": "string", "optional": True},
                                "vpc_id": {"type": "string", "required": True},
                            },
                            "block_types": {
                                "ingress": {"nesting_mode": "set"},
                                "egress": {"nesting_mode": "set"},
                            },
                        }
                    },
                    "aws_s3_bucket": {
                        "block": {
                            "attributes": {"bucket": {"type": "string", "required": True}},
                        }
                    },
                }
            }
        },
    }


def _eb_contract() -> TerraformContract:
    return TerraformContract(
        kind="provider_resource",
        name="aws_elastic_beanstalk_environment",
        version="aws-provider-docs",
        allowed_arguments=["name", "application", "setting", "solution_stack_name"],
        source="fixture",
    )


def test_build_blackboard_is_generic_with_no_keyword_selection():
    blackboard = build_blackboard(repo_patterns=RepoPatterns(uses_terragrunt=True))

    assert blackboard.repo_patterns.uses_terragrunt is True
    # No service/language golden paths: nothing is pre-selected from keywords.
    assert blackboard.selected_contracts == []
    assert blackboard.contract_docs == {}
    assert blackboard.required_artifacts == []


def test_resolve_contracts_for_files_uses_generated_resource_types():
    resolver = ContractResolver(
        provider_contracts={"aws_elastic_beanstalk_environment": _eb_contract()}
    )
    files = {
        "modules/app/main.tf": (
            'resource "aws_elastic_beanstalk_environment" "this" {\n  name = "x"\n}\n'
            'resource "aws_s3_bucket" "logs" {\n  bucket = "y"\n}\n'
        )
    }

    resolved = resolve_contracts_for_files(files, resolver)

    # Only the generated resource type the resolver knows about is resolved —
    # the candidate set is the actual output, not a keyword list.
    assert set(resolved) == {"aws_elastic_beanstalk_environment"}


def test_validate_generated_contracts_rejects_unsupported_top_level_argument():
    contract_docs = {"aws_elastic_beanstalk_environment": _eb_contract()}
    files = {
        "modules/app/main.tf": (
            'resource "aws_elastic_beanstalk_environment" "this" {\n'
            '  name = "example"\n'
            '  instance_type = "t3.micro"\n'
            "}\n"
        )
    }

    result = validate_generated_contracts(files, contract_docs)

    assert result.status.value == "failed"
    assert "unsupported argument `instance_type`" in result.errors[0]


def test_validate_generated_contracts_ignores_nested_block_arguments():
    # Regression: nested block keys (the namespace/name/value inside a `setting`
    # block) must NOT be treated as unsupported top-level arguments.
    contract_docs = {"aws_elastic_beanstalk_environment": _eb_contract()}
    files = {
        "modules/app/main.tf": (
            'resource "aws_elastic_beanstalk_environment" "this" {\n'
            '  name        = "example"\n'
            '  application = "app"\n'
            "  setting {\n"
            '    namespace = "aws:autoscaling:launchconfiguration"\n'
            '    name      = "InstanceType"\n'
            '    value     = "t3.micro"\n'
            "  }\n"
            "}\n"
        )
    }

    result = validate_generated_contracts(files, contract_docs)

    assert result.status.value == "passed"


def test_validate_generated_contracts_no_docs_is_pass():
    result = validate_generated_contracts({"modules/a/main.tf": 'resource "x" "y" {}'}, {})
    assert result.status.value == "passed"


def test_blackboard_prompt_section_emits_contracts_and_negative_patterns():
    blackboard = RunBlackboard(
        selected_contracts=["aws_elastic_beanstalk_environment"],
        contract_docs={"aws_elastic_beanstalk_environment": _eb_contract()},
        negative_patterns=[
            "Do not use top-level instance_type on aws_elastic_beanstalk_environment."
        ],
    )

    section = build_blackboard_prompt_section(blackboard)

    assert "Shared run blackboard" in section
    assert "authoritative" in section
    assert "aws_elastic_beanstalk_environment" in section
    assert "allowed arguments: name, application, setting, solution_stack_name" in section
    assert "Do not use top-level instance_type" in section


def test_blackboard_prompt_section_empty_when_nothing_resolved():
    # A first-pass blackboard with nothing learned yet injects no boilerplate.
    assert build_blackboard_prompt_section(RunBlackboard()) == ""
    assert build_blackboard_prompt_section(None) == ""


def test_extract_resource_types_is_ordered_and_deduplicated():
    contents = [
        'resource "aws_s3_bucket" "a" {\n  bucket = "a"\n}\n'
        'resource "aws_security_group" "b" {\n  vpc_id = "v"\n}\n',
        'resource "aws_s3_bucket" "c" {\n  bucket = "c"\n}\n',
    ]

    assert extract_resource_types(contents) == ["aws_s3_bucket", "aws_security_group"]


def test_contracts_from_provider_schema_builds_allowed_and_required_arguments():
    contracts = contracts_from_provider_schema(_provider_schema())

    assert set(contracts) == {"aws_security_group", "aws_s3_bucket"}
    sg = contracts["aws_security_group"]
    assert sg.kind == "provider_resource"
    # Allowed = attributes + nested block names, sorted.
    assert sg.allowed_arguments == ["description", "egress", "ingress", "name", "vpc_id"]
    # Only schema-required attributes are required.
    assert sg.required_arguments == ["vpc_id"]
    assert "hashicorp/aws" in sg.source


def test_contracts_from_provider_schema_scopes_to_requested_resource_types():
    contracts = contracts_from_provider_schema(_provider_schema(), resource_types={"aws_s3_bucket"})

    assert set(contracts) == {"aws_s3_bucket"}


def test_contracts_from_provider_schema_handles_empty_schema():
    assert contracts_from_provider_schema({}) == {}
    assert contracts_from_provider_schema({"provider_schemas": {}}) == {}


def test_harvested_contracts_drive_real_validation():
    # End-to-end: schema -> contracts -> proactive validation flags a bad argument.
    contracts = contracts_from_provider_schema(_provider_schema())
    files = {
        "modules/net/main.tf": (
            'resource "aws_security_group" "this" {\n'
            "  vpc_id = var.vpc_id\n"
            '  bogus_argument = "x"\n'
            "}\n"
        )
    }

    result = validate_generated_contracts(files, contracts)

    assert result.status.value == "failed"
    assert "unsupported argument `bogus_argument`" in result.errors[0]


def test_normalize_validation_findings_extracts_negative_schema_patterns():
    errors = [
        "terraform validate modules/example failed:\n"
        "│ Error: Unsupported argument\n"
        '│   on main.tf line 21, in resource "aws_elastic_beanstalk_environment" "dotnet_env":\n'
        "│   21:   instance_type = var.instance_type\n"
        '│ An argument named "instance_type" is not expected here.'
    ]

    findings = normalize_validation_findings(errors)

    assert findings == [
        ValidationFinding(
            scope="aws_elastic_beanstalk_environment",
            finding="Unsupported argument instance_type",
            source="terraform validation",
            severity="hard",
            negative_pattern=(
                "Do not use argument `instance_type` with `aws_elastic_beanstalk_environment`; "
                "it is not in that contract."
            ),
        )
    ]


def test_normalize_validation_findings_extracts_unsupported_block():
    errors = [
        "terraform validate bootstrap/backend/non-prod failed:\n"
        "│ Error: Unsupported block type\n"
        '│   on main.tf line 49, in resource "aws_dynamodb_table" "terraform_locks":\n'
        "│   49:   point_in_time_recovery_specification {\n"
        '│ Blocks of type "point_in_time_recovery_specification" are not expected here.'
    ]

    findings = normalize_validation_findings(errors)

    assert findings == [
        ValidationFinding(
            scope="aws_dynamodb_table",
            finding="Unsupported block point_in_time_recovery_specification",
            source="terraform validation",
            severity="hard",
            negative_pattern=(
                "Do not use a `point_in_time_recovery_specification` block in "
                "`aws_dynamodb_table`; it is not in that contract."
            ),
        )
    ]
