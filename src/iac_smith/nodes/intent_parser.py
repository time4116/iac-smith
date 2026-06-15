from typing import Protocol

from iac_smith.bedrock_intent import BedrockIntentClient
from iac_smith.models.intent import InfrastructureIntent, SupportedIntent

UNSUPPORTED_MVP_REASON = (
    "Unsupported request family for MVP. Supported families are baseline, VPC, "
    "EKS Fargate, ECS Fargate, and private RDS PostgreSQL."
)
UNMAPPED_REASON = "IaC Smith could not map the request to a supported MVP infrastructure family."
PUBLIC_DATABASE_REASON = "Publicly exposed databases are blocked. Request private RDS instead."


class IntentClient(Protocol):
    def parse_issue(self, issue_text: str) -> InfrastructureIntent: ...


def _requests_public_database_exposure(text: str) -> bool:
    database_terms = ["rds", "database", "postgres", "postgresql", "mysql", "aurora"]
    public_terms = ["open to the internet", "publicly accessible", "public internet", "0.0.0.0/0"]
    return any(term in text for term in database_terms) and any(
        term in text for term in public_terms
    )


def _apply_final_safety_guards(intent: InfrastructureIntent) -> InfrastructureIntent:
    text = intent.raw_request.lower()
    if _requests_public_database_exposure(text):
        return intent.model_copy(
            update={
                "supported_intent": SupportedIntent.UNSUPPORTED,
                "blocked": True,
                "block_reason": PUBLIC_DATABASE_REASON,
            }
        )
    if intent.supported_intent == SupportedIntent.UNSUPPORTED and not intent.blocked:
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
