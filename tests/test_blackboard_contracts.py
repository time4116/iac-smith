from iac_smith.blackboard import (
    ContractResolver,
    RunBlackboard,
    TerraformContract,
    ValidationFinding,
    build_blackboard,
    build_blackboard_prompt_section,
    normalize_validation_findings,
    resolve_contracts_for_files,
    validate_generated_contracts,
)
from iac_smith.models.repo_patterns import RepoPatterns


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
