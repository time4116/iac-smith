from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.nodes.change_planner import plan_changes as default_plan_changes
from iac_smith.nodes.static_review import static_review_generated_files


class EvalRunResult(BaseModel):
    intent_hash: str
    plan_hash: str
    render_hash: str
    static_passed: bool
    failures: list[str] = Field(default_factory=list)


class EvalReport(BaseModel):
    issue_number: int | None = None
    target_repo: str
    runs: int
    intent_variants: int
    plan_variants: int
    render_hash_variants: int
    static_pass: int
    failure_clusters: list[str]
    results: list[EvalRunResult]


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_fixture(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Eval fixture {path} must contain a YAML mapping.")
    if not data.get("issue_body"):
        raise ValueError(f"Eval fixture {path} must include issue_body.")
    if not data.get("target_repo"):
        raise ValueError(f"Eval fixture {path} must include target_repo.")
    return data


def evaluate_fixture(
    fixture_path: str | Path,
    *,
    runs: int = 3,
    parse_intent: Callable[[str], InfrastructureIntent],
    plan_changes: Callable[[InfrastructureIntent, str], ChangePlan] | None = None,
    generate_files: Callable[..., dict[str, str]],
) -> EvalReport:
    """Run a deterministic local variance harness for one issue fixture.

    Callers inject parser/generator functions so tests can use recorded responses,
    while live commands can supply Bedrock-backed functions. The report measures
    exactly the instability that made issue-level live runs impossible to reason
    about: intent, plan, rendered files, and validation failures.
    """

    fixture = _load_fixture(Path(fixture_path))
    planner = plan_changes or default_plan_changes
    results: list[EvalRunResult] = []
    failures: Counter[str] = Counter()

    for _ in range(runs):
        intent = parse_intent(fixture["issue_body"])
        change_plan = planner(intent, fixture["target_repo"])
        generated_files = generate_files(
            intent=intent,
            change_plan=change_plan,
            repo_patterns=fixture.get("repo_patterns"),
            ruleset=None,
            target_repo=fixture["target_repo"],
        )
        validation = static_review_generated_files(generated_files)
        run_failures = [*validation.errors, *validation.structural]
        for failure in run_failures:
            failures[failure] += 1
        results.append(
            EvalRunResult(
                intent_hash=_stable_hash(intent.model_dump(mode="json")),
                plan_hash=_stable_hash(change_plan.model_dump(mode="json")),
                render_hash=_stable_hash(generated_files),
                static_passed=not validation.errors and not validation.structural,
                failures=run_failures,
            )
        )

    return EvalReport(
        issue_number=fixture.get("issue_number"),
        target_repo=fixture["target_repo"],
        runs=runs,
        intent_variants=len({result.intent_hash for result in results}),
        plan_variants=len({result.plan_hash for result in results}),
        render_hash_variants=len({result.render_hash for result in results}),
        static_pass=sum(1 for result in results if result.static_passed),
        failure_clusters=[failure for failure, _ in failures.most_common()],
        results=results,
    )


def report_to_text(report: EvalReport) -> str:
    lines = [
        f"issue: {report.issue_number if report.issue_number is not None else 'unknown'}",
        f"target_repo: {report.target_repo}",
        f"runs: {report.runs}",
        f"intent_variants: {report.intent_variants}",
        f"plan_variants: {report.plan_variants}",
        f"render_hash_variants: {report.render_hash_variants}",
        f"static_pass: {report.static_pass}/{report.runs}",
        "failure_clusters:",
    ]
    if report.failure_clusters:
        lines.extend(
            f"  {index}. {failure}" for index, failure in enumerate(report.failure_clusters, 1)
        )
    else:
        lines.append("  none")
    return "\n".join(lines) + "\n"


def main() -> None:
    import argparse

    from iac_smith.bedrock_intent import BedrockIntentClient
    from iac_smith.graph import default_file_generator

    parser = argparse.ArgumentParser(description="Run IaC Smith local variance evals.")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    client = BedrockIntentClient()
    report = evaluate_fixture(
        args.fixture,
        runs=args.runs,
        parse_intent=client.parse_issue,
        generate_files=default_file_generator,
    )
    print(report_to_text(report), end="")


if __name__ == "__main__":
    main()
