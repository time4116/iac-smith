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
