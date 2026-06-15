from pydantic import BaseModel, Field


class RepoPatterns(BaseModel):
    uses_terraform: bool = False
    uses_terragrunt: bool = False
    environments: list[str] = Field(default_factory=list)
    default_environment_names: list[str] = Field(default_factory=lambda: ["non-prod", "prod"])
    module_sources: list[str] = Field(default_factory=list)
    preferred_layout: str = "iac_smith_default"
    remote_state_uses_path_relative_to_include: bool = False
    existing_stack_paths: list[str] = Field(default_factory=list)
    representative_files: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
