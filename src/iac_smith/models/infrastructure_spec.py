from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


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
    # Unknown fields are rejected, not ignored: the removed raw-HCL `blocks`
    # channel (or any hallucinated key) must surface as a repair finding instead
    # of being silently dropped from the rendered resource.
    model_config = ConfigDict(extra="forbid")

    type: str
    name: str
    # Values are native JSON: strings are verbatim Terraform expressions; numbers,
    # booleans, lists, and objects are rendered to HCL deterministically. Demanding
    # stringified HCL for everything made the model's natural (and unambiguous)
    # JSON typing a schema violation.
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    # Structured nested blocks following Terraform JSON configuration semantics:
    # each key is a provider-declared nested block name, each list entry is one
    # block instance rendered deterministically to `name { ... }` HCL. This keeps
    # the model out of raw-HCL authoring entirely.
    nested_blocks: dict[str, list[dict[str, JsonValue]]] = Field(default_factory=dict)


class ProviderResourcesSpec(BaseModel):
    kind: Literal["provider_resources"] = "provider_resources"
    resources: list[ResourceSpec] = Field(default_factory=list)
    # Per resource type: names that are nested blocks (at any depth) in the
    # provider schema, so the renderer can emit `name { ... }` instead of an
    # attribute assignment. Carried on the spec because rendering happens after
    # composition, where the schema contracts are no longer in scope.
    block_names: dict[str, list[str]] = Field(default_factory=dict)
    contract_source: str = "provider-schema"


class RegistryModuleSpec(BaseModel):
    kind: Literal["registry_module"] = "registry_module"
    source: str
    version: str | None = None
    # Same native-JSON value semantics as ResourceSpec.arguments: the renderer
    # writes the module call, the model never authors HCL text.
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
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
    rendering_policy: Literal[
        "deterministic_structure_only",
        "composed_provider_resources",
        "composed_registry_module",
    ] = "deterministic_structure_only"
