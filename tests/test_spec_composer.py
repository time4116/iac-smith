import json

import pytest

from iac_smith.blackboard import ContractResolver, TerraformContract
from iac_smith.models.change_plan import BackendResource, ChangePlan
from iac_smith.models.infrastructure_spec import OutputSpec, ResourceSpec
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.spec_composer import (
    ComposedComponent,
    SpecComposer,
    SpecCompositionError,
    validate_composed_component,
)
from iac_smith.spec_renderer import SpecRendererGenerator, render_provider_resources

CONTRACTS = {
    "customcloud_network": TerraformContract(
        kind="provider_resource",
        name="customcloud_network",
        allowed_arguments=["cidr_block", "name"],
        required_arguments=["cidr_block"],
        source="fixture schema",
    ),
    "customcloud_database": TerraformContract(
        kind="provider_resource",
        name="customcloud_database",
        allowed_arguments=["engine", "name", "network_ref", "settings"],
        required_arguments=["engine"],
        source="fixture schema",
    ),
}
ALLOWED_INPUTS = ["aws_region", "environment"]


def _intent() -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request="Create a non-prod managed database platform in us-west-2",
        resource_type="database_platform",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
    )


def _plan(stack_name: str = "database-platform") -> ChangePlan:
    return ChangePlan(
        stack_name=stack_name,
        environments=["non-prod"],
        files_to_generate=[
            "README.md",
            "environments/non-prod/root.hcl",
            f"environments/non-prod/{stack_name}/terragrunt.hcl",
            f"modules/{stack_name}/main.tf",
            f"modules/{stack_name}/variables.tf",
            f"modules/{stack_name}/outputs.tf",
            f"modules/{stack_name}/versions.tf",
        ],
        backend_resources={
            "non-prod": BackendResource(bucket="iac-smith-state", lock_table="iac-smith-lock")
        },
        summary=["Generate database-platform Terraform/Terragrunt structure"],
    )


class FakeStreamRuntime:
    """Bedrock runtime double replaying canned JSON payloads over the stream shape."""

    def __init__(self, payloads: list[dict]):
        self.payloads = [json.dumps(payload) for payload in payloads]
        self.prompts: list[str] = []

    def invoke_model(self, **kwargs):
        raise NotImplementedError

    def invoke_model_with_response_stream(self, **kwargs):
        body = json.loads(kwargs["body"])
        self.prompts.append(body["messages"][0]["content"])
        text = self.payloads.pop(0)
        return {
            "body": [
                {
                    "chunk": {
                        "bytes": json.dumps(
                            {"type": "content_block_delta", "delta": {"text": text}}
                        ).encode()
                    }
                },
                {
                    "chunk": {
                        "bytes": json.dumps(
                            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
                        ).encode()
                    }
                },
            ]
        }


def _composer(payloads: list[dict], **kwargs) -> tuple[SpecComposer, FakeStreamRuntime]:
    runtime = FakeStreamRuntime(payloads)
    composer = SpecComposer(model_id="fixture-model", bedrock_runtime=runtime, **kwargs)
    return composer, runtime


_VALID_SELECTION = {"resource_types": ["customcloud_network", "customcloud_database"]}
_VALID_COMPOSITION = {
    "resources": [
        {
            "type": "customcloud_network",
            "name": "this",
            "arguments": {"cidr_block": '"10.0.0.0/16"', "name": "var.environment"},
        },
        {
            "type": "customcloud_database",
            "name": "this",
            "arguments": {
                "engine": '"postgres"',
                "network_ref": "customcloud_network.this.id",
            },
            "blocks": ['settings {\n  tier = "small"\n}'],
        },
    ],
    "outputs": [
        {
            "name": "database_ref",
            "description": "Identifier of the managed database.",
            "value": "customcloud_database.this.id",
        }
    ],
    "assumptions": ["Sized for non-production workloads."],
}


def test_compose_returns_schema_valid_typed_resources():
    composer, runtime = _composer([_VALID_SELECTION, _VALID_COMPOSITION])

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    assert [r.type for r in composed.resources] == [
        "customcloud_network",
        "customcloud_database",
    ]
    assert composed.outputs[0].value == "customcloud_database.this.id"
    assert "customcloud_network" in runtime.prompts[1]
    assert "required arguments: cidr_block" in runtime.prompts[1]


def test_compose_retries_type_selection_with_nearest_valid_type_hint():
    composer, runtime = _composer(
        [
            {"resource_types": ["customcloud_databse"]},
            _VALID_SELECTION,
            _VALID_COMPOSITION,
        ]
    )

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    assert len(composed.resources) == 2
    assert "`customcloud_databse` does not exist" in runtime.prompts[1]
    assert "`customcloud_database`" in runtime.prompts[1]


def test_compose_repairs_unsupported_argument_with_gate_finding():
    bad = {
        "resources": [
            {
                "type": "customcloud_database",
                "name": "this",
                "arguments": {"engine": '"postgres"', "publicly_visible": "true"},
            }
        ],
        "outputs": [],
        "assumptions": [],
    }
    composer, runtime = _composer([_VALID_SELECTION, bad, _VALID_COMPOSITION])

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    assert len(composed.resources) == 2
    assert "unsupported argument `publicly_visible`" in runtime.prompts[2]


def test_compose_raises_after_bounded_repair_rounds():
    bad = {
        "resources": [{"type": "customcloud_database", "name": "this", "arguments": {}}],
        "outputs": [],
        "assumptions": [],
    }
    composer, _ = _composer([_VALID_SELECTION, bad, bad], max_repair_rounds=1)

    with pytest.raises(SpecCompositionError, match="missing required argument `engine`"):
        composer.compose(
            intent=_intent(),
            component_name="database-platform",
            allowed_inputs=ALLOWED_INPUTS,
            environments=["non-prod"],
            provider_contracts=CONTRACTS,
        )


def _validate(composed: ComposedComponent) -> list[str]:
    return validate_composed_component(
        composed,
        provider_contracts=CONTRACTS,
        known_resource_types=set(CONTRACTS),
        allowed_inputs=ALLOWED_INPUTS,
        component_name="database-platform",
    )


def test_validation_flags_hallucinated_type_argument_and_block():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(type="customcloud_cluster", name="this", arguments={"size": "3"}),
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={"engine": '"postgres"'},
                blocks=["replication {\n  copies = 2\n}"],
            ),
        ]
    )

    errors = _validate(composed)

    assert any("unsupported resource type `customcloud_cluster`" in e for e in errors)
    assert any("unsupported nested block `replication`" in e for e in errors)


def test_validation_flags_reference_violations():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={
                    "engine": '"postgres"',
                    "name": "var.vpc_id",
                    "network_ref": "customcloud_network.missing.id",
                },
            )
        ],
        outputs=[
            OutputSpec(name="ref", description="Ref.", value="local.stack_name"),
        ],
    )

    errors = _validate(composed)

    assert any("undeclared variable `var.vpc_id`" in e for e in errors)
    assert any("`customcloud_network.missing`" in e for e in errors)
    assert any("references `local.`" in e for e in errors)


def test_validation_flags_invalid_and_duplicate_output_names():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(type="customcloud_database", name="db", arguments={"engine": '"postgres"'})
        ],
        outputs=[
            OutputSpec(name="bad-name", description="Ref.", value="customcloud_database.db.id"),
            OutputSpec(name="ref", description="Ref.", value="customcloud_database.db.id"),
            OutputSpec(name="ref", description="Again.", value="customcloud_database.db.id"),
        ],
    )

    errors = _validate(composed)

    assert any("Output name `bad-name`" in e for e in errors)
    assert any("Duplicate output name `ref`" in e for e in errors)


def test_validation_requires_at_least_one_resource():
    assert _validate(ComposedComponent(resources=[])) == [
        "Composition must select at least one provider resource."
    ]


def test_render_provider_resources_indents_multiline_blocks():
    rendered = render_provider_resources(
        [
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={"engine": '"postgres"'},
                blocks=['settings {\n  tier = "small"\n}'],
            )
        ]
    )

    assert 'resource "customcloud_database" "db" {' in rendered
    assert '\n  settings {\n    tier = "small"\n  }\n' in rendered


class FakeSpecComposer:
    def __init__(self, composed: ComposedComponent | None = None, error: Exception | None = None):
        self.composed = composed
        self.error = error
        self.kwargs: dict | None = None

    def compose(self, **kwargs) -> ComposedComponent:
        self.kwargs = kwargs
        if self.error:
            raise self.error
        assert self.composed is not None
        return self.composed


def _patch_resolver(monkeypatch, contracts=CONTRACTS):
    monkeypatch.setattr(
        "iac_smith.provider_schema.build_schema_resolver",
        lambda files, **kwargs: ContractResolver(provider_contracts=contracts),
    )


def test_generator_renders_composed_resources(monkeypatch):
    _patch_resolver(monkeypatch)
    composer = FakeSpecComposer(composed=ComposedComponent.model_validate(_VALID_COMPOSITION))

    files = SpecRendererGenerator(composer=composer).generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    main_tf = files["modules/database-platform/main.tf"]
    assert 'resource "customcloud_network" "this"' in main_tf
    assert 'resource "customcloud_database" "this"' in main_tf
    assert files.structure_only is False
    assert 'output "database_ref"' in files["modules/database-platform/outputs.tf"]
    assert "structure only" not in files["README.md"]
    assert composer.kwargs["allowed_inputs"] == ["aws_region", "environment"]


def test_generator_escapes_composed_output_descriptions(monkeypatch):
    _patch_resolver(monkeypatch)
    composed = ComposedComponent.model_validate(_VALID_COMPOSITION)
    composed.outputs[0] = OutputSpec(
        name="database_ref",
        description='Identifier "quoted"\nwith ${var.environment} template',
        value="customcloud_database.this.id",
    )

    files = SpecRendererGenerator(composer=FakeSpecComposer(composed=composed)).generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    outputs_tf = files["modules/database-platform/outputs.tf"]
    assert (
        'description = "Identifier \\"quoted\\"\\nwith $${var.environment} template"' in outputs_tf
    )


def test_generator_falls_back_to_structure_only_when_composition_fails(monkeypatch):
    _patch_resolver(monkeypatch)
    composer = FakeSpecComposer(error=SpecCompositionError("no valid types"))

    files = SpecRendererGenerator(composer=composer).generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert files.structure_only is True
    assert "Spec composition failed" in files["README.md"]
    assert "No provider resources were selected" in files["modules/database-platform/main.tf"]


def test_generator_falls_back_when_schema_harvest_unavailable(monkeypatch):
    _patch_resolver(monkeypatch, contracts={})
    composer = FakeSpecComposer()

    files = SpecRendererGenerator(composer=composer).generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert files.structure_only is True
    assert composer.kwargs is None
    assert "Provider schema harvest was unavailable" in files["README.md"]


def test_generator_skips_composition_without_model(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)

    files = SpecRendererGenerator().generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert files.structure_only is True
