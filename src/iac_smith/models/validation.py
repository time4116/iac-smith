from enum import StrEnum

from pydantic import BaseModel, Field


class ValidationStatus(StrEnum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class ValidationResult(BaseModel):
    status: ValidationStatus
    checks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # Structural/semantic issues (undeclared refs, duplicate declarations,
    # missing required Terragrunt inputs, etc.). These do NOT block PR creation:
    # they are surfaced for review and fed to the bounded autofix loop, while the
    # real terraform/terragrunt validation in cli.py is the authoritative gate.
    # Only `errors` (security/safety) block.
    structural: list[str] = Field(default_factory=list)
