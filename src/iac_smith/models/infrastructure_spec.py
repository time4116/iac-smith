from typing import Literal

from pydantic import BaseModel, Field


class ValueExpression(BaseModel):
    """A renderer-safe value expression for generated Terragrunt/Terraform glue."""

    expression: str


class BackendSpec(BaseModel):
    environment: str
    bucket: str
    lock_table: str
    region: str


class DependencySpec(BaseModel):
    consumer: str
    producer: str
    outputs: list[str]


class OutputSpec(BaseModel):
    name: str
    description: str
    value: str


class ResourceSpec(BaseModel):
    type: str
    name: str
    arguments: dict[str, str] = Field(default_factory=dict)
    blocks: list[str] = Field(default_factory=list)


class ProviderResourcesSpec(BaseModel):
    kind: Literal["provider_resources"] = "provider_resources"
    resources: list[ResourceSpec] = Field(default_factory=list)
    contract_source: str = "provider-schema"


class RegistryModuleSpec(BaseModel):
    kind: Literal["registry_module"] = "registry_module"
    source: str
    version: str | None = None
    inputs: dict[str, ValueExpression] = Field(default_factory=dict)
    outputs: list[OutputSpec] = Field(default_factory=list)


ImplementationSpec = ProviderResourcesSpec | RegistryModuleSpec


class ComponentSpec(BaseModel):
    name: str
    kind: Literal["foundation", "workload", "data", "security", "observability"]
    implementation: ImplementationSpec
    inputs: dict[str, ValueExpression] = Field(default_factory=dict)
    outputs: list[OutputSpec] = Field(default_factory=list)


class InfrastructureSpec(BaseModel):
    """Typed contract the deterministic renderer compiles into repo files.

    This is intentionally not a service template. The model/spec layer may choose
    arbitrary components and contracts; the renderer owns structural consistency:
    paths, Terragrunt envelopes, variable declarations, outputs, dependencies, and
    workflow files.
    """

    raw_request: str
    target_repo: str
    stack_name: str
    environments: list[str]
    region: str
    backends: list[BackendSpec]
    components: list[ComponentSpec]
    dependencies: list[DependencySpec] = Field(default_factory=list)
    files_to_generate: list[str]
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rendering_policy: Literal["deterministic_structure_only"] = "deterministic_structure_only"
