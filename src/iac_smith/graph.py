from collections.abc import Callable
from pathlib import Path

from langgraph.graph import END, StateGraph

from iac_smith.generator import generate_files
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.validation import ValidationResult, ValidationStatus
from iac_smith.nodes.change_planner import plan_changes
from iac_smith.nodes.intent_parser import parse_intent
from iac_smith.nodes.pr_writer import build_pr_body
from iac_smith.nodes.ruleset_loader import load_ruleset
from iac_smith.nodes.static_review import static_review_generated_files
from iac_smith.repo_scanner import scan_repo_patterns
from iac_smith.state import IaCSmithState

IntentParser = Callable[[str], InfrastructureIntent]


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


def code_generator(state: IaCSmithState) -> IaCSmithState:
    if state.get("generated_files"):
        return {**state, "status": "generated"}
    return {
        **state,
        "generated_files": generate_files(
            intent=state["intent"],
            change_plan=state["change_plan"],
            repo_patterns=state["repo_patterns"],
            target_repo=state["target_repo"],
        ),
        "status": "generated",
    }


def validation_runner(state: IaCSmithState) -> IaCSmithState:
    generated_files = state.get("generated_files", {})
    if generated_files:
        validation = static_review_generated_files(generated_files)
    else:
        validation = ValidationResult(
            status=ValidationStatus.FAILED,
            errors=["Generated files are required before static review and PR creation."],
        )
    status = "blocked" if validation.status == ValidationStatus.FAILED else "validated"
    return {**state, "validation": validation, "status": status}


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
    validation = state.get("validation")
    return "end" if validation and validation.status == ValidationStatus.FAILED else "pr_writer"


def build_graph(intent_parser_fn: IntentParser = parse_intent):
    graph = StateGraph(IaCSmithState)
    graph.add_node("issue_intake", issue_intake)
    graph.add_node("intent_parser", make_intent_parser(intent_parser_fn))
    graph.add_node("ruleset_loader", ruleset_loader)
    graph.add_node("repo_pattern_scanner", repo_pattern_scanner)
    graph.add_node("change_planner", change_planner)
    graph.add_node("code_generator", code_generator)
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
    graph.add_edge("change_planner", "code_generator")
    graph.add_edge("code_generator", "validation_runner")
    graph.add_conditional_edges(
        "validation_runner",
        route_after_validation,
        {"end": END, "pr_writer": "pr_writer"},
    )
    graph.add_edge("pr_writer", END)
    return graph.compile()
