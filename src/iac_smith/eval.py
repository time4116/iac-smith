from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.nodes.change_planner import plan_changes as default_plan_changes
from iac_smith.nodes.static_review import static_review_generated_files
from iac_smith.runtime_validation import validate_generated_iac
from iac_smith.spec_renderer import SpecRendererGenerator
from iac_smith.workspace import apply_generated_files


class EvalRunResult(BaseModel):
    intent_hash: str
    plan_hash: str
    render_hash: str
    static_passed: bool
    terraform_validate_passed: bool | None = None
    terragrunt_validate_passed: bool | None = None
    terragrunt_plan_passed: bool | None = None
    failures: list[str] = Field(default_factory=list)


class EvalReport(BaseModel):
    issue_number: int | None = None
    target_repo: str
    runs: int
    intent_variants: int
    plan_variants: int
    render_hash_variants: int
    static_pass: int
    terraform_validate_pass: int | None = None
    terragrunt_validate_pass: int | None = None
    terragrunt_plan_pass: int | None = None
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


def _load_replay_intents(path: Path | None) -> list[InfrastructureIntent] | None:
    if path is None:
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_intents = data.get("intents") if isinstance(data, dict) else data
    if not isinstance(raw_intents, list) or not raw_intents:
        raise ValueError(f"Replay file {path} must contain a non-empty `intents` list.")
    return [InfrastructureIntent.model_validate(raw) for raw in raw_intents]


def _replay_parser(intents: list[InfrastructureIntent]) -> Callable[[str], InfrastructureIntent]:
    index = 0

    def parse(_issue_body: str) -> InfrastructureIntent:
        nonlocal index
        intent = intents[index % len(intents)]
        index += 1
        return intent

    return parse


def _runtime_result_for(
    generated_files: dict[str, str], *, run_plan: bool
) -> tuple[bool, bool, bool | None, list[str]]:
    with tempfile.TemporaryDirectory(prefix="iac-smith-eval-") as tmp:
        repo_root = Path(tmp) / "repo"
        repo_root.mkdir()
        apply_generated_files(repo_root, generated_files)
        env = {"IAC_SMITH_CHECK_TIMEOUT": "60"}
        if run_plan:
            env["IAC_SMITH_RUNTIME_PLAN"] = "1"
        result = validate_generated_iac(repo_root, env_override=env)
    terraform_steps = [step for step in result.step_results if step.phase == "terraform_validate"]
    terragrunt_steps = [step for step in result.step_results if step.phase == "terragrunt_validate"]
    plan_steps = [step for step in result.step_results if step.phase == "terragrunt_plan"]
    prerequisite_failed = any(
        step.phase == "prerequisite" and not step.passed for step in result.step_results
    )
    terraform_validate = bool(terraform_steps) and all(step.passed for step in terraform_steps)
    terragrunt_validate = bool(terragrunt_steps) and all(step.passed for step in terragrunt_steps)
    if prerequisite_failed:
        return False, False, False if run_plan else None, result.errors
    terragrunt_plan = None
    if run_plan:
        terragrunt_plan = bool(plan_steps) and all(step.passed for step in plan_steps)
    return terraform_validate, terragrunt_validate, terragrunt_plan, result.errors


def evaluate_fixture(
    fixture_path: str | Path,
    *,
    runs: int = 3,
    parse_intent: Callable[[str], InfrastructureIntent] | None = None,
    plan_changes: Callable[[InfrastructureIntent, str], ChangePlan] | None = None,
    generate_files: Callable[..., dict[str, str]] | None = None,
    replay_path: str | Path | None = None,
    run_runtime: bool = False,
    run_plan: bool = False,
) -> EvalReport:
    """Run a local variance harness for one issue fixture.

    Use ``replay_path`` to run without live Bedrock. Replay files contain recorded
    structured intents and can be committed as fixtures for deterministic
    regression tests. Runtime validation is opt-in because it shells out to
    terraform/terragrunt and may need provider downloads.
    """

    fixture = _load_fixture(Path(fixture_path))
    replay_intents = _load_replay_intents(Path(replay_path)) if replay_path else None
    parser = parse_intent or (_replay_parser(replay_intents) if replay_intents else None)
    if parser is None:
        raise ValueError("evaluate_fixture requires parse_intent or replay_path.")
    planner = plan_changes or default_plan_changes
    generator = generate_files or SpecRendererGenerator().generate_files
    repo_patterns = RepoPatterns.model_validate(fixture.get("repo_patterns") or {})
    results: list[EvalRunResult] = []
    failures: Counter[str] = Counter()

    for _ in range(runs):
        intent = parser(fixture["issue_body"])
        change_plan = planner(intent, fixture["target_repo"])
        generated_files = generator(
            intent=intent,
            change_plan=change_plan,
            repo_patterns=repo_patterns,
            ruleset=None,
            target_repo=fixture["target_repo"],
            repo_path=fixture.get("repo_path"),
        )
        validation = static_review_generated_files(generated_files)
        run_failures = [*validation.errors, *validation.structural]
        terraform_validate_passed = None
        terragrunt_validate_passed = None
        terragrunt_plan_passed = None
        if run_runtime:
            (
                terraform_validate_passed,
                terragrunt_validate_passed,
                terragrunt_plan_passed,
                runtime_failures,
            ) = _runtime_result_for(generated_files, run_plan=run_plan)
            run_failures.extend(runtime_failures)
        for failure in run_failures:
            failures[failure] += 1
        results.append(
            EvalRunResult(
                intent_hash=_stable_hash(intent.model_dump(mode="json")),
                plan_hash=_stable_hash(change_plan.model_dump(mode="json")),
                render_hash=_stable_hash(generated_files),
                static_passed=not validation.errors and not validation.structural,
                terraform_validate_passed=terraform_validate_passed,
                terragrunt_validate_passed=terragrunt_validate_passed,
                terragrunt_plan_passed=terragrunt_plan_passed,
                failures=run_failures,
            )
        )

    runtime_was_run = any(result.terraform_validate_passed is not None for result in results)
    plan_was_run = any(result.terragrunt_plan_passed is not None for result in results)
    return EvalReport(
        issue_number=fixture.get("issue_number"),
        target_repo=fixture["target_repo"],
        runs=runs,
        intent_variants=len({result.intent_hash for result in results}),
        plan_variants=len({result.plan_hash for result in results}),
        render_hash_variants=len({result.render_hash for result in results}),
        static_pass=sum(1 for result in results if result.static_passed),
        terraform_validate_pass=(
            sum(1 for result in results if result.terraform_validate_passed)
            if runtime_was_run
            else None
        ),
        terragrunt_validate_pass=(
            sum(1 for result in results if result.terragrunt_validate_passed)
            if runtime_was_run
            else None
        ),
        terragrunt_plan_pass=(
            sum(1 for result in results if result.terragrunt_plan_passed) if plan_was_run else None
        ),
        failure_clusters=[failure for failure, _ in failures.most_common()],
        results=results,
    )


def _pass_text(value: int | None, runs: int) -> str:
    if value is None:
        return "not_run"
    return f"{value}/{runs}"


def report_to_text(report: EvalReport) -> str:
    lines = [
        f"issue: {report.issue_number if report.issue_number is not None else 'unknown'}",
        f"target_repo: {report.target_repo}",
        f"runs: {report.runs}",
        f"intent_variants: {report.intent_variants}",
        f"plan_variants: {report.plan_variants}",
        f"render_hash_variants: {report.render_hash_variants}",
        f"static_pass: {report.static_pass}/{report.runs}",
        f"terraform_validate_pass: {_pass_text(report.terraform_validate_pass, report.runs)}",
        f"terragrunt_validate_pass: {_pass_text(report.terragrunt_validate_pass, report.runs)}",
        f"terragrunt_plan_pass: {_pass_text(report.terragrunt_plan_pass, report.runs)}",
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

    parser = argparse.ArgumentParser(description="Run IaC Smith local variance evals.")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--replay", type=Path, help="YAML file containing recorded intents")
    parser.add_argument(
        "--runtime", action="store_true", help="run terraform/terragrunt validation"
    )
    parser.add_argument("--plan", action="store_true", help="include local-state terragrunt plan")
    args = parser.parse_args()

    parse_intent = None if args.replay else BedrockIntentClient().parse_issue
    report = evaluate_fixture(
        args.fixture,
        runs=args.runs,
        parse_intent=parse_intent,
        replay_path=args.replay,
        run_runtime=args.runtime or args.plan,
        run_plan=args.plan,
    )
    print(report_to_text(report), end="")


if __name__ == "__main__":
    main()
