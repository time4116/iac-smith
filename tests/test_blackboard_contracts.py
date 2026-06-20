from iac_smith.blackboard import (
    ContractResolver,
    RunBlackboard,
    TerraformContract,
    ValidationFinding,
    build_blackboard,
    build_blackboard_prompt_section,
    normalize_validation_findings,
)
from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns


def _intent(
    raw_request: str, resource_type: str = "elastic_beanstalk_dotnet"
) -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request=raw_request,
        resource_type=resource_type,
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        features=["dotnet", "web", "https"],
    )


def _plan(files: list[str] | None = None) -> ChangePlan:
    return ChangePlan(
        stack_name="elastic-beanstalk-dotnet",
        environments=["non-prod"],
        files_to_generate=files
        or [
            "src/elastic-beanstalk-dotnet/Program.cs",
            "modules/elastic-beanstalk-dotnet/main.tf",
        ],
        backend_resources={"non-prod": BackendResource(bucket="state", lock_table="lock")},
        summary=["Generate Elastic Beanstalk .NET application"],
    )


def test_build_blackboard_records_required_src_artifact_and_selected_contracts():
    resolver = ContractResolver(
        provider_contracts={
            "aws_elastic_beanstalk_environment": TerraformContract(
                kind="provider_resource",
                name="aws_elastic_beanstalk_environment",
                version="aws-provider-docs",
                allowed_arguments=["name", "application", "setting", "solution_stack_name"],
                source="fixture",
            )
        }
    )

    blackboard = build_blackboard(
        intent=_intent("Create a dotnet web app in Elastic Beanstalk with a src directory"),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(uses_terragrunt=True),
        resolver=resolver,
    )

    assert blackboard.required_artifacts == ["src/"]
    assert "aws_elastic_beanstalk_environment" in blackboard.selected_contracts
    assert blackboard.contract_docs["aws_elastic_beanstalk_environment"].allowed_arguments == [
        "name",
        "application",
        "setting",
        "solution_stack_name",
    ]


def test_blackboard_prompt_section_makes_contracts_and_negative_patterns_authoritative():
    blackboard = RunBlackboard(
        required_artifacts=["src/"],
        selected_contracts=["aws_elastic_beanstalk_environment"],
        contract_docs={
            "aws_elastic_beanstalk_environment": TerraformContract(
                kind="provider_resource",
                name="aws_elastic_beanstalk_environment",
                version="aws-provider-docs",
                allowed_arguments=["name", "application", "setting"],
                source="terraform registry docs",
            )
        },
        negative_patterns=[
            "Do not use top-level instance_type on aws_elastic_beanstalk_environment."
        ],
    )

    section = build_blackboard_prompt_section(blackboard)

    assert "Shared run blackboard" in section
    assert "src/" in section
    assert "aws_elastic_beanstalk_environment" in section
    assert "allowed arguments: name, application, setting" in section
    assert "Do not use top-level instance_type" in section
    assert "authoritative" in section


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


def test_validate_generated_contracts_rejects_unsupported_resource_arguments():
    from iac_smith.blackboard import validate_generated_contracts

    blackboard = RunBlackboard(
        contract_docs={
            "aws_elastic_beanstalk_environment": TerraformContract(
                kind="provider_resource",
                name="aws_elastic_beanstalk_environment",
                allowed_arguments=["name", "application", "setting"],
                source="fixture",
            )
        }
    )

    result = validate_generated_contracts(
        {
            "modules/example/main.tf": (
                'resource "aws_elastic_beanstalk_environment" "this" {\n'
                '  name = "example"\n'
                '  instance_type = "t3.micro"\n'
                "}\n"
            )
        },
        blackboard,
    )

    assert result.status.value == "failed"
    assert "unsupported argument `instance_type`" in result.errors[0]
