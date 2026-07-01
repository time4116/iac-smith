from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.spec_renderer import build_spec_from_intent, render_spec


def _intent(resource_type: str = "aurora_postgres") -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request="Create a non-prod Aurora PostgreSQL data platform in us-west-2",
        resource_type=resource_type,
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        requires_new_vpc=True,
        features=["kms", "secret_rotation", "rds_proxy"],
    )


def _plan(stack_name: str = "aurora-postgres") -> ChangePlan:
    return ChangePlan(
        stack_name=stack_name,
        environments=["non-prod"],
        files_to_generate=[
            "README.md",
            ".github/workflows/terraform-pr-check.yml",
            ".github/workflows/terraform-apply.yml",
            "bootstrap/backend/non-prod/main.tf",
            "bootstrap/backend/non-prod/variables.tf",
            "bootstrap/backend/non-prod/outputs.tf",
            "bootstrap/backend/non-prod/README.md",
            "environments/non-prod/root.hcl",
            f"environments/non-prod/{stack_name}/terragrunt.hcl",
            f"environments/non-prod/{stack_name}/README.md",
            f"modules/{stack_name}/main.tf",
            f"modules/{stack_name}/variables.tf",
            f"modules/{stack_name}/outputs.tf",
            f"modules/{stack_name}/versions.tf",
            f"modules/{stack_name}/README.md",
        ],
        backend_resources={
            "non-prod": BackendResource(bucket="iac-smith-state", lock_table="iac-smith-lock")
        },
        summary=["Generate aurora-postgres Terraform/Terragrunt structure"],
    )


def test_build_spec_from_intent_records_components_contracts_and_no_new_foundation():
    spec = build_spec_from_intent(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(existing_stack_paths=["modules/foundation"]),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert spec.stack_name == "aurora-postgres"
    assert [component.name for component in spec.components] == ["aurora-postgres"]
    assert spec.components[0].implementation.kind == "provider_resources"
    assert spec.components[0].implementation.resources == []
    assert spec.dependencies[0].producer == "foundation"
    assert spec.dependencies[0].outputs == ["vpc_id", "private_subnet_ids"]
    assert spec.rendering_policy == "deterministic_structure_only"


def test_render_spec_owns_cross_file_contracts_deterministically():
    spec = build_spec_from_intent(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(existing_stack_paths=["modules/foundation"]),
        target_repo="time4116/iac-smith-demo-infra",
    )

    first = render_spec(spec)
    second = render_spec(spec)

    assert first == second
    assert set(first) == set(_plan().files_to_generate)
    stack_hcl = first["environments/non-prod/aurora-postgres/terragrunt.hcl"]
    variables = first["modules/aurora-postgres/variables.tf"]
    outputs = first["modules/aurora-postgres/outputs.tf"]

    assert 'terraform {\n  source = "../../../modules/aurora-postgres"\n}' in stack_hcl
    assert 'dependency "foundation"' in stack_hcl
    assert "vpc_id = dependency.foundation.outputs.vpc_id" in stack_hcl
    assert 'variable "vpc_id"' in variables
    assert 'variable "private_subnet_ids"' in variables
    assert 'resource "aws_vpc"' not in first["modules/aurora-postgres/main.tf"]
    assert 'output "spec_summary"' in outputs


def test_render_spec_uses_only_planned_paths_when_module_already_exists():
    plan = _plan()
    plan = plan.model_copy(
        update={
            "files_to_generate": [
                path for path in plan.files_to_generate if not path.startswith("modules/")
            ]
        }
    )
    spec = build_spec_from_intent(
        intent=_intent(),
        change_plan=plan,
        repo_patterns=RepoPatterns(existing_stack_paths=["modules/aurora-postgres"]),
        target_repo="time4116/iac-smith-demo-infra",
    )

    files = render_spec(spec)

    assert set(files) == set(plan.files_to_generate)
    assert not any(path.startswith("modules/") for path in files)
    assert (
        'source = "../../../modules/aurora-postgres"'
        in files["environments/non-prod/aurora-postgres/terragrunt.hcl"]
    )
