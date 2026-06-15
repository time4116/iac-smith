from enum import StrEnum

from pydantic import BaseModel, Field


class EnvironmentScope(StrEnum):
    NON_PROD_ONLY = "non_prod_only"
    PROD_ONLY = "prod_only"
    BOTH = "both"


class InfrastructureIntent(BaseModel):
    raw_request: str
    # Free-form AWS resource type as interpreted by Bedrock, e.g. "vpc_foundation",
    # "eks_fargate", "rds_postgres", "s3_bucket", "lambda_function", "baseline".
    resource_type: str
    environment_scope: EnvironmentScope
    environments: list[str]
    region: str = "us-west-2"
    requires_new_vpc: bool = False
    features: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None
