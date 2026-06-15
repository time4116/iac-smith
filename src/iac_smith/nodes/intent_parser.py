from typing import Protocol

from iac_smith.bedrock_intent import BedrockIntentClient
from iac_smith.models.intent import InfrastructureIntent

UNMAPPED_REASON = "IaC Smith could not determine an infrastructure resource type from this issue."


class IntentClient(Protocol):
    def parse_issue(self, issue_text: str) -> InfrastructureIntent: ...


def _apply_final_safety_guards(intent: InfrastructureIntent) -> InfrastructureIntent:
    if not intent.resource_type.strip() and not intent.blocked:
        return intent.model_copy(update={"blocked": True, "block_reason": UNMAPPED_REASON})
    return intent


def parse_intent(
    issue_text: str, intent_client: IntentClient | None = None
) -> InfrastructureIntent:
    client = intent_client or BedrockIntentClient()
    intent = client.parse_issue(issue_text)
    if not intent.raw_request:
        intent = intent.model_copy(update={"raw_request": issue_text})
    return _apply_final_safety_guards(intent)
