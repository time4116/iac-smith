import re

from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent, SupportedIntent

REGION_RE = re.compile(r"\b(?:[a-z]{2}-[a-z]+-\d)\b")
UNSUPPORTED_MVP_REASON = (
    "Unsupported request family for MVP. Supported families are baseline, VPC, "
    "EKS Fargate, and ECS Fargate."
)
UNMAPPED_REASON = "IaC Smith could not map the request to a supported MVP infrastructure family."


def parse_intent(issue_text: str) -> InfrastructureIntent:
    text = issue_text.lower()
    region_match = REGION_RE.search(issue_text)
    region = region_match.group(0) if region_match else "us-west-2"

    warnings: list[str] = []
    assumptions: list[str] = []
    features: list[str] = []

    if not region_match:
        warnings.append("Region defaulted to us-west-2 because the issue did not specify one.")

    if "non-prod only" in text or "non prod only" in text or "non-prod" in text:
        environment_scope = EnvironmentScope.NON_PROD_ONLY
        environments = ["non-prod"]
    elif "prod only" in text or "production only" in text or "production" in text:
        environment_scope = EnvironmentScope.PROD_ONLY
        environments = ["prod"]
    else:
        environment_scope = EnvironmentScope.BOTH
        environments = ["non-prod", "prod"]
        assumptions.append(
            "Generated both non-prod and prod because no environment scope was specified."
        )

    if "rds" in text or "database" in text or "postgres" in text:
        return InfrastructureIntent(
            raw_request=issue_text,
            supported_intent=SupportedIntent.UNSUPPORTED,
            environment_scope=environment_scope,
            environments=environments,
            region=region,
            warnings=warnings,
            blocked=True,
            block_reason=UNSUPPORTED_MVP_REASON,
        )

    if "eks" in text and "fargate" in text:
        supported_intent = SupportedIntent.EKS_FARGATE
    elif "ecs" in text and "fargate" in text:
        supported_intent = SupportedIntent.ECS_FARGATE
    elif "vpc" in text:
        supported_intent = SupportedIntent.VPC_FOUNDATION
    elif "remote state" in text or "backend" in text or "bootstrap" in text:
        supported_intent = SupportedIntent.BASELINE
    else:
        supported_intent = SupportedIntent.UNSUPPORTED

    if supported_intent == SupportedIntent.UNSUPPORTED:
        return InfrastructureIntent(
            raw_request=issue_text,
            supported_intent=supported_intent,
            environment_scope=environment_scope,
            environments=environments,
            region=region,
            warnings=warnings,
            blocked=True,
            block_reason=UNMAPPED_REASON,
        )

    requires_new_vpc = (
        "new vpc" in text
        or "vpc" in text
        or supported_intent
        in {
            SupportedIntent.EKS_FARGATE,
            SupportedIntent.ECS_FARGATE,
            SupportedIntent.VPC_FOUNDATION,
        }
    )
    if requires_new_vpc:
        assumptions.append("Created a new VPC because no existing network was specified.")

    needs_private_subnets = (
        "private subnet" in text
        or "private subnets" in text
        or supported_intent != SupportedIntent.BASELINE
    )
    if needs_private_subnets:
        features.append("private_subnets")
        assumptions.append(
            "Private subnets are preferred unless public access is explicitly requested."
        )
    if "remote state" in text or supported_intent != SupportedIntent.BASELINE:
        features.append("remote_state")
    if "logging" in text or "logs" in text:
        features.append("logging")

    return InfrastructureIntent(
        raw_request=issue_text,
        supported_intent=supported_intent,
        environment_scope=environment_scope,
        environments=environments,
        region=region,
        requires_new_vpc=requires_new_vpc,
        features=sorted(set(features)),
        assumptions=assumptions,
        warnings=warnings,
    )
