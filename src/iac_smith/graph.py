from collections.abc import Callable
from inspect import signature
from pathlib import Path
from typing import Protocol

from langgraph.graph import END, StateGraph

from iac_smith.blackboard import (
    ContractResolver,
    RunBlackboard,
    build_blackboard,
    normalize_validation_findings,
    resolve_contracts_for_files,
    validate_generated_contracts,
)
from iac_smith.dynamic_terraform import BedrockTerraformGenerator
from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.rules import Ruleset
from iac_smith.models.validation import ValidationResult, ValidationStatus
from iac_smith.nodes.change_planner import plan_changes
from iac_smith.nodes.intent_parser import parse_intent
from iac_smith.nodes.pr_writer import build_pr_body
from iac_smith.nodes.ruleset_loader import load_ruleset
from iac_smith.nodes.static_review import existing_stack_dirs, static_review_generated_files
from iac_smith.repo_scanner import scan_repo_patterns
from iac_smith.state import IaCSmithState

IntentParser = Callable[[str], InfrastructureIntent]


class FileGenerator(Protocol):
    def __call__(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
        repo_path: Path | None = None,
        blackboard: RunBlackboard | None = None,
    ) -> dict[str, str]: ...


def default_file_generator(
    *,
    intent: InfrastructureIntent,
    change_plan: ChangePlan,
    repo_patterns: RepoPatterns,
    ruleset: Ruleset | None,
    target_repo: str,
    repo_path: Path | None = None,
    blackboard: RunBlackboard | None = None,
) -> dict[str, str]:
    return BedrockTerraformGenerator().generate_files(
        intent=intent,
        change_plan=change_plan,
        repo_patterns=repo_patterns,
        ruleset=ruleset,
        target_repo=target_repo,
        repo_path=repo_path,
        blackboard=blackboard,
    )


def _call_file_generator(file_generator_fn: FileGenerator, **kwargs) -> dict[str, str]:
    params = signature(file_generator_fn).parameters
    if "blackboard" not in params:
        kwargs.pop("blackboard", None)
    return file_generator_fn(**kwargs)


def issue_intake(state: IaCSmithState) -> IaCSmithState:
    if "iac-smith" not in state.get("labels", []):
        return {**state, "status": "ignored", "block_reason": "Missing iac-smith label"}
    return {**state, "status": "accepted"}


def make_intent_parser(intent_parser_fn: IntentParser):
    def intent_parser_node(state: IaCSmithState) -> IaCSmithState:
        if state.get("intent"):
            return {**state, "status": "intent_parsed"}
        intent = intent_parser_fn(state["issue_body"])
        if intent.blocked:
            return {
                **state,
                "intent": intent,
                "status": "blocked",
                "block_reason": intent.block_reason,
                "pr_body": None,
            }
        return {**state, "intent": intent, "status": "intent_parsed"}

    return intent_parser_node


def ruleset_loader(state: IaCSmithState) -> IaCSmithState:
    rules_dir = Path(state.get("target_repo_path") or ".") / "rules"
    return {
        **state,
        "ruleset": load_ruleset(rules_dir if rules_dir.exists() else None),
        "status": "rules_loaded",
    }


def repo_pattern_scanner(state: IaCSmithState) -> IaCSmithState:
    repo_path = Path(state.get("target_repo_path") or ".")
    return {
        **state,
        "repo_patterns": scan_repo_patterns(repo_path),
        "status": "repo_scanned",
    }


def change_planner(state: IaCSmithState) -> IaCSmithState:
    return {
        **state,
        "change_plan": plan_changes(
            state["intent"],
            state["target_repo"],
            repo_patterns=state.get("repo_patterns"),
        ),
        "status": "planned",
    }


def blackboard_planner(state: IaCSmithState) -> IaCSmithState:
    return {
        **state,
        "blackboard": build_blackboard(repo_patterns=state.get("repo_patterns")),
        "status": "blackboard_ready",
    }


def make_code_generator(file_generator_fn: FileGenerator):
    def code_generator_node(state: IaCSmithState) -> IaCSmithState:
        # If we have generated files and we are not in a validation-repair loop, reuse them.
        # However, if have_failed is set on validation, we must re-generate with repair context.
        validation_errors = []
        validation = state.get("validation")
        if validation and validation.status == ValidationStatus.FAILED:
            validation_errors = validation.errors

        # If we are already validated or have no errors but files exist, return.
        if state.get("generated_files") and not validation_errors:
            return {**state, "status": "generated"}

        # Perform Bedrock generation or recovery attempt
        raw_repo_path = state.get("target_repo_path")
        generated_files = _call_file_generator(
            file_generator_fn,
            intent=state["intent"],
            change_plan=state["change_plan"],
            repo_patterns=state["repo_patterns"],
            ruleset=state.get("ruleset"),
            target_repo=state["target_repo"],
            repo_path=Path(raw_repo_path) if raw_repo_path else None,
            blackboard=state.get("blackboard"),
        )
        return {
            **state,
            "generated_files": generated_files,
            "status": "generated",
        }

    return code_generator_node


def validation_runner(state: IaCSmithState) -> IaCSmithState:
    generated_files = state.get("generated_files", {})
    blackboard = state.get("blackboard")
    if generated_files:
        validation = static_review_generated_files(
            generated_files, known_stack_dirs=existing_stack_dirs(state.get("target_repo_path"))
        )
        if validation.status != ValidationStatus.FAILED:
            # Resolve contracts for the resource types actually generated (generic;
            # the resolver is the injection point for a future provider-schema or
            # registry lookup). Empty until one is wired, so this is a no-op pass.
            contract_docs = resolve_contracts_for_files(generated_files, ContractResolver())
            if blackboard and contract_docs:
                blackboard = blackboard.model_copy(
                    update={
                        "contract_docs": contract_docs,
                        "selected_contracts": sorted(contract_docs),
                    }
                )
            contract_validation = validate_generated_contracts(generated_files, contract_docs)
            if contract_validation.status == ValidationStatus.FAILED:
                validation = contract_validation
    else:
        validation = ValidationResult(
            status=ValidationStatus.FAILED,
            errors=["Generated files are required before static review and PR creation."],
        )

    # Let's read and increment the global retry attempts count if validation failed
    repair_attempts = state.get("repair_attempts", 0)
    if validation.status == ValidationStatus.FAILED:
        repair_attempts += 1

    status = "validated"
    if validation.status == ValidationStatus.FAILED:
        status = "blocked" if repair_attempts >= 3 else "needs_repair"

    if validation.status == ValidationStatus.FAILED and blackboard:
        blackboard = blackboard.with_findings(normalize_validation_findings(validation.errors))

    return {
        **state,
        "validation": validation,
        "blackboard": blackboard,
        "repair_attempts": repair_attempts,
        "status": status,
    }


def pr_writer(state: IaCSmithState) -> IaCSmithState:
    return {
        **state,
        "pr_body": build_pr_body(
            issue_url=state["issue_url"],
            intent=state["intent"],
            change_plan=state["change_plan"],
            validation=state["validation"],
        ),
        "status": "pr_ready",
    }


def route_after_intake(state: IaCSmithState) -> str:
    return "end" if state.get("status") == "ignored" else "intent_parser"


def route_after_intent(state: IaCSmithState) -> str:
    return "end" if state.get("status") == "blocked" else "ruleset_loader"


def route_after_validation(state: IaCSmithState) -> str:
    status = state.get("status")
    if status == "needs_repair":
        return "code_generator"
    if status == "blocked":
        return "end"
    return "pr_writer"


def build_graph(
    intent_parser_fn: IntentParser = parse_intent,
    file_generator_fn: FileGenerator = default_file_generator,
):
    graph = StateGraph(IaCSmithState)
    graph.add_node("issue_intake", issue_intake)
    graph.add_node("intent_parser", make_intent_parser(intent_parser_fn))
    graph.add_node("ruleset_loader", ruleset_loader)
    graph.add_node("repo_pattern_scanner", repo_pattern_scanner)
    graph.add_node("change_planner", change_planner)
    graph.add_node("blackboard_planner", blackboard_planner)
    graph.add_node("code_generator", make_code_generator(file_generator_fn))
    graph.add_node("validation_runner", validation_runner)
    graph.add_node("pr_writer", pr_writer)

    graph.set_entry_point("issue_intake")
    graph.add_conditional_edges(
        "issue_intake",
        route_after_intake,
        {"end": END, "intent_parser": "intent_parser"},
    )
    graph.add_conditional_edges(
        "intent_parser",
        route_after_intent,
        {"end": END, "ruleset_loader": "ruleset_loader"},
    )
    graph.add_edge("ruleset_loader", "repo_pattern_scanner")
    graph.add_edge("repo_pattern_scanner", "change_planner")
    graph.add_edge("change_planner", "blackboard_planner")
    graph.add_edge("blackboard_planner", "code_generator")
    graph.add_edge("code_generator", "validation_runner")
    graph.add_conditional_edges(
        "validation_runner",
        route_after_validation,
        {
            "end": END,
            "pr_writer": "pr_writer",
            "code_generator": "code_generator",
        },
    )
    graph.add_edge("pr_writer", END)
    return graph.compile()
