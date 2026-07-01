from pathlib import Path

from iac_smith.cli import _apply_generated_files_for_mode
from iac_smith.eval import evaluate_fixture, report_to_text
from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.spec_renderer import (
    build_spec_from_intent,
    discover_foundation_outputs,
    render_spec,
    validate_spec,
)


def _aurora_intent() -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request=(
            "Create non-prod Aurora PostgreSQL data platform in us-west-2 with "
            "RDS Proxy, KMS CMK, and secret rotation"
        ),
        resource_type="aurora_postgres",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        features=["rds_proxy", "kms", "secret_rotation"],
    )


def _plan() -> ChangePlan:
    stack = "aurora-postgres"
    return ChangePlan(
        stack_name=stack,
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
            f"environments/non-prod/{stack}/terragrunt.hcl",
            f"environments/non-prod/{stack}/README.md",
            f"modules/{stack}/main.tf",
            f"modules/{stack}/variables.tf",
            f"modules/{stack}/outputs.tf",
            f"modules/{stack}/versions.tf",
            f"modules/{stack}/README.md",
        ],
        backend_resources={
            "non-prod": BackendResource(bucket="iac-smith-state", lock_table="iac-smith-lock")
        },
        summary=["Generate aurora-postgres Terraform/Terragrunt structure"],
    )


def test_foundation_outputs_are_discovered_from_repo_not_hardcoded(tmp_path: Path):
    outputs = tmp_path / "modules/foundation/outputs.tf"
    outputs.parent.mkdir(parents=True)
    outputs.write_text(
        'output "network_id" { value = aws_vpc.main.id }\n'
        'output "app_subnet_ids" { value = aws_subnet.private[*].id }\n',
        encoding="utf-8",
    )

    assert discover_foundation_outputs(tmp_path) == ["network_id", "app_subnet_ids"]

    spec = build_spec_from_intent(
        intent=_aurora_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(existing_stack_paths=["modules/foundation"]),
        target_repo="time4116/iac-smith-demo-infra",
        repo_path=tmp_path,
    )

    assert spec.dependencies[0].outputs == ["network_id", "app_subnet_ids"]
    stack_hcl = render_spec(spec)["environments/non-prod/aurora-postgres/terragrunt.hcl"]
    variables = render_spec(spec)["modules/aurora-postgres/variables.tf"]
    assert "network_id = dependency.foundation.outputs.network_id" in stack_hcl
    assert "app_subnet_ids = dependency.foundation.outputs.app_subnet_ids" in stack_hcl
    assert 'variable "network_id"' in variables
    assert 'variable "app_subnet_ids"' in variables


def test_spec_renderer_does_not_keyword_generate_aurora_golden_path():
    spec = build_spec_from_intent(
        intent=_aurora_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    validation = validate_spec(spec)
    files = render_spec(spec)
    main_tf = files["modules/aurora-postgres/main.tf"]
    stack_hcl = files["environments/non-prod/aurora-postgres/terragrunt.hcl"]

    assert validation.errors == []
    assert spec.components[0].implementation.kind == "provider_resources"
    assert spec.components[0].implementation.resources == []
    assert 'resource "aws_rds_cluster"' not in main_tf
    assert "No provider resources were selected" in main_tf
    assert "var." not in stack_hcl
    assert "subnet-1234567890abcdef0" not in stack_hcl


def test_spec_renderer_mode_bypasses_freeform_normalizers(tmp_path: Path):
    result = {
        "generated_files": {
            "environments/non-prod/example/terragrunt.hcl": (
                'include "root" {\n  path = find_in_parent_folders("root.hcl")\n}\n'
                'terraform {\n  source = "../../../modules/example"\n}\n'
            )
        }
    }

    _apply_generated_files_for_mode(tmp_path, result, generation_mode="spec_renderer")

    written = (tmp_path / "environments/non-prod/example/terragrunt.hcl").read_text()
    assert written == result["generated_files"]["environments/non-prod/example/terragrunt.hcl"]


def test_eval_replay_file_runs_without_live_bedrock(tmp_path: Path):
    fixture = tmp_path / "fixture.yaml"
    replay = tmp_path / "replay.yaml"
    fixture.write_text(
        "issue_number: 59\n"
        "target_repo: time4116/iac-smith-demo-infra\n"
        "issue_body: Create Aurora PostgreSQL\n",
        encoding="utf-8",
    )
    replay.write_text(
        "intents:\n"
        "  - raw_request: Create Aurora PostgreSQL\n"
        "    resource_type: aurora_postgres\n"
        "    environment_scope: non_prod_only\n"
        "    environments: [non-prod]\n"
        "    region: us-west-2\n",
        encoding="utf-8",
    )

    report = evaluate_fixture(fixture, runs=3, replay_path=replay)

    assert report.runs == 3
    assert report.intent_variants == 1
    assert report.render_hash_variants == 1
    assert report.static_pass == 3
    assert report.terraform_validate_pass is None
    assert "terraform_validate_pass: not_run" in report_to_text(report)


def test_eval_runtime_columns_are_wired_with_injected_validator(tmp_path: Path, monkeypatch):
    calls = []

    class FakeRuntimeResult:
        passed = True
        checks = ["`terraform validate` in `modules/example`", "`terragrunt plan` in `env`"]
        errors = []
        contract_docs = {}

    def fake_validate(repo_path, env_override=None):
        calls.append((repo_path, env_override))
        return FakeRuntimeResult()

    monkeypatch.setattr("iac_smith.eval.validate_generated_iac", fake_validate)
    fixture = tmp_path / "fixture.yaml"
    replay = tmp_path / "replay.yaml"
    fixture.write_text(
        "issue_number: 59\n"
        "target_repo: time4116/iac-smith-demo-infra\n"
        "issue_body: Create Aurora PostgreSQL\n",
        encoding="utf-8",
    )
    replay.write_text(
        "intents:\n"
        "  - raw_request: Create Aurora PostgreSQL\n"
        "    resource_type: aurora_postgres\n"
        "    environment_scope: non_prod_only\n"
        "    environments: [non-prod]\n"
        "    region: us-west-2\n",
        encoding="utf-8",
    )

    report = evaluate_fixture(fixture, runs=2, replay_path=replay, run_runtime=True, run_plan=True)

    assert len(calls) == 2
    assert report.terraform_validate_pass == 2
    assert report.terragrunt_validate_pass == 2
    assert report.terragrunt_plan_pass == 2
    assert "terragrunt_plan_pass: 2/2" in report_to_text(report)
