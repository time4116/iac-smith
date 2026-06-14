from pydantic import BaseModel, Field


class BackendResource(BaseModel):
    bucket: str
    lock_table: str


class ChangePlan(BaseModel):
    stack_name: str
    environments: list[str]
    files_to_generate: list[str]
    backend_resources: dict[str, BackendResource]
    summary: list[str] = Field(default_factory=list)
