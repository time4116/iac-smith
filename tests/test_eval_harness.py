from pathlib import Path

from iac_smith.eval import evaluate_fixture
from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent


def test_evaluate_fixture_measures_variance_with_injected_components(tmp_path: Path):
    fixture = tmp_path / "issue.yaml"
    fixture.write_text(
        "issue_number: 59\n"
        "target_repo: time4116/iac-smith-demo-infra\n"
        "issue_body: Create Aurora PostgreSQL in non-prod\n",
        encoding="utf-8",
    )
    calls = {"count": 0}

    def parse_intent(issue_body: str) -> InfrastructureIntent:
        calls["count"] += 1
        resource_type = "aurora_postgres" if calls["count"] == 1 else "aurora_postgresql"
        return InfrastructureIntent(
            raw_request=issue_body,
            resource_type=resource_type,
            environment_scope=EnvironmentScope.NON_PROD_ONLY,
            environments=["non-prod"],
            region="us-west-2",
        )

    def plan_changes(intent: InfrastructureIntent, target_repo: str) -> ChangePlan:
        stack = intent.resource_type.replace("_", "-")
        return ChangePlan(
            stack_name=stack,
            environments=["non-prod"],
            files_to_generate=[f"modules/{stack}/main.tf"],
            backend_resources={"non-prod": BackendResource(bucket="state", lock_table="lock")},
            summary=[f"Generate {stack}"],
        )

    def generate_files(**kwargs) -> dict[str, str]:
        stack = kwargs["change_plan"].stack_name
        return {f"modules/{stack}/main.tf": f"# {stack}\n"}

    report = evaluate_fixture(
        fixture,
        runs=2,
        parse_intent=parse_intent,
        plan_changes=plan_changes,
        generate_files=generate_files,
    )

    assert report.issue_number == 59
    assert report.runs == 2
    assert report.intent_variants == 2
    assert report.render_hash_variants == 2
    assert report.static_pass == 2
    assert report.failure_clusters == []
