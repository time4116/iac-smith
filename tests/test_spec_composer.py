import json

import hcl2
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
from iac_smith.spec_renderer import (
    SpecRendererGenerator,
    StructureOnlyBlocked,
    render_hcl_value,
    render_provider_resources,
)

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
        allowed_arguments=["engine", "name", "network_ref", "port", "public", "settings", "tags"],
        required_arguments=["engine"],
        block_names=["settings"],
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

    def __init__(self, payloads: list[dict | str], stop_reason: str = "end_turn"):
        # A str payload is replayed verbatim (malformed-response scenarios);
        # dicts are serialized to JSON.
        self.payloads = [
            payload if isinstance(payload, str) else json.dumps(payload) for payload in payloads
        ]
        self.stop_reason = stop_reason
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
                            {"type": "message_delta", "delta": {"stop_reason": self.stop_reason}}
                        ).encode()
                    }
                },
            ]
        }


def _composer(
    payloads: list[dict | str], stop_reason: str = "end_turn", **kwargs
) -> tuple[SpecComposer, FakeStreamRuntime]:
    runtime = FakeStreamRuntime(payloads, stop_reason=stop_reason)
    composer = SpecComposer(model_id="fixture-model", bedrock_runtime=runtime, **kwargs)
    return composer, runtime


_VALID_SELECTION = {"resource_types": ["customcloud_network", "customcloud_database"]}
_VALID_COMPOSITION = {
    "resources": [
        {
            "type": "customcloud_network",
            "name": "this",
            "arguments": {"cidr_block": "10.0.0.0/16", "name": "${var.environment}"},
        },
        {
            "type": "customcloud_database",
            "name": "this",
            "arguments": {
                "engine": "postgres",
                "network_ref": "${customcloud_network.this.id}",
            },
            "nested_blocks": {"settings": [{"tier": "small"}]},
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
                "arguments": {"engine": "postgres", "publicly_visible": "true"},
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


def test_compose_accepts_native_json_argument_values():
    composition = {
        "resources": [
            {
                "type": "customcloud_database",
                "name": "db",
                "arguments": {
                    "engine": "postgres",
                    "port": 5432,
                    "public": False,
                    "tags": {"Environment": "${var.environment}", "ManagedBy": "IaC Smith"},
                },
            }
        ],
        "outputs": [],
        "assumptions": [],
    }
    composer, _ = _composer([{"resource_types": ["customcloud_database"]}, composition])

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    rendered = render_provider_resources(composed.resources)
    assert 'engine = "postgres"' in rendered
    assert "port = 5432" in rendered
    assert "public = false" in rendered
    # A string that is exactly one interpolation renders as the bare expression.
    assert "Environment = var.environment" in rendered
    assert 'ManagedBy = "IaC Smith"' in rendered
    hcl2.loads(rendered)


def test_compose_repairs_invalid_response_shape():
    missing_name = {"resources": [{"type": "customcloud_database"}], "outputs": []}
    valid = {
        "resources": [
            {"type": "customcloud_database", "name": "db", "arguments": {"engine": "postgres"}}
        ],
        "outputs": [],
        "assumptions": [],
    }
    composer, runtime = _composer(
        [{"resource_types": ["customcloud_database"]}, missing_name, valid]
    )

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    assert len(composed.resources) == 1
    assert "Response field `resources.0.name`" in runtime.prompts[2]


def test_validation_flags_undeclared_variable_inside_nested_value():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={"engine": "postgres", "tags": {"Vpc": "${var.vpc_id}"}},
            )
        ]
    )

    errors = _validate(composed)

    assert any("undeclared variable `var.vpc_id`" in e for e in errors)


def test_render_hcl_value_renders_nested_structures():
    rendered = render_hcl_value(
        {
            "kubernetes.io/cluster": "owned",
            "ports": [5432, 5433],
            "nested": {"enabled": True, "ratio": 1.5},
        }
    )

    assert '"kubernetes.io/cluster" = "owned"' in rendered
    assert "ports = [\n      5432,\n      5433\n    ]" in rendered
    assert "enabled = true" in rendered
    assert "ratio = 1.5" in rendered


def test_render_hcl_value_escapes_keys_and_plain_text_to_parseable_hcl():
    rendered = (
        'resource "customcloud_database" "db" {\n  tags = '
        + render_hcl_value({'bad"key': "value", "multi\nline": "x", "ManagedBy": "IaC Smith"})
        + "\n}\n"
    )

    assert '"bad\\"key" = "value"' in rendered
    assert '"multi\\nline" = "x"' in rendered
    assert 'ManagedBy = "IaC Smith"' in rendered
    hcl2.loads(rendered)


def test_validation_rejects_unparseable_rendered_hcl():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={"engine": "postgres"},
            )
        ],
        outputs=[OutputSpec(name="broken", description="Bad.", value=")))")],
    )

    errors = _validate(composed)

    assert any("output broken do not parse as valid HCL" in e for e in errors)


def test_compose_tolerates_literal_newlines_inside_json_strings():
    raw = (
        '{"resources": [{"type": "customcloud_database", "name": "db", '
        '"arguments": {"engine": "postgres", "name": "primary\n  replica"}}], '
        '"outputs": [], "assumptions": []}'
    )
    composer, _ = _composer([{"resource_types": ["customcloud_database"]}, raw])

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    assert composed.resources[0].arguments["name"] == "primary\n  replica"


def test_selection_repairs_unparseable_response():
    composer, runtime = _composer(
        [
            "Here are the resources you should use for this platform.",
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
    assert "could not be parsed" in runtime.prompts[1]
    assert "Response began: 'Here are the resources" in runtime.prompts[1]


def test_selection_raises_when_both_responses_unparseable():
    composer, _ = _composer(["not json", "still not json"])

    with pytest.raises(SpecCompositionError, match="not parseable JSON"):
        composer.compose(
            intent=_intent(),
            component_name="database-platform",
            allowed_inputs=ALLOWED_INPUTS,
            environments=["non-prod"],
            provider_contracts=CONTRACTS,
        )


def test_compose_repairs_unparseable_response():
    composer, runtime = _composer(
        [
            _VALID_SELECTION,
            "I could not produce the configuration you asked for.",
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
    assert "could not be parsed" in runtime.prompts[2]
    assert "Response began: 'I could not produce" in runtime.prompts[2]


def test_compose_raises_clear_error_when_response_hits_token_cap():
    composer, _ = _composer([_VALID_SELECTION], stop_reason="max_tokens")

    with pytest.raises(SpecCompositionError, match="truncated at the output token cap"):
        composer.compose(
            intent=_intent(),
            component_name="database-platform",
            allowed_inputs=ALLOWED_INPUTS,
            environments=["non-prod"],
            provider_contracts=CONTRACTS,
        )


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
                arguments={"engine": "postgres"},
                nested_blocks={"replication": [{"copies": 2}]},
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
                    "engine": "postgres",
                    "name": "${var.vpc_id}",
                    "network_ref": "${customcloud_network.missing.id}",
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


def test_validation_rejects_bare_references_in_argument_strings():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_network", name="net", arguments={"cidr_block": "10.0.0.0/16"}
            ),
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={
                    "engine": "postgres",
                    "network_ref": "customcloud_network.net.id",
                    "name": "var.environment",
                },
            ),
        ]
    )

    errors = _validate(composed)

    assert any(
        "bare Terraform reference" in e and "customcloud_network.net.id" in e for e in errors
    )
    assert any("bare Terraform reference" in e and "var.environment" in e for e in errors)


def test_validation_accepts_interpolated_references_in_argument_strings():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_network", name="net", arguments={"cidr_block": "10.0.0.0/16"}
            ),
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={
                    "engine": "postgres",
                    "network_ref": "${customcloud_network.net.id}",
                    "name": "${var.environment}",
                    "tags": {"Network": "${customcloud_network.net.id}", "Team": "data"},
                },
            ),
        ]
    )

    assert _validate(composed) == []


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
                arguments={"engine": "postgres"},
                nested_blocks={"settings": [{"tier": "small"}]},
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

    files = SpecRendererGenerator(composer=composer, allow_structure_only=True).generate_files(
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

    files = SpecRendererGenerator(composer=composer, allow_structure_only=True).generate_files(
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

    files = SpecRendererGenerator(allow_structure_only=True).generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    assert files.structure_only is True


# --- Issue #108 regressions: fail closed, nested blocks, staged rejection ---


def test_generator_blocks_composition_failure_by_default(monkeypatch):
    _patch_resolver(monkeypatch)
    monkeypatch.delenv("IAC_SMITH_ALLOW_STRUCTURE_ONLY", raising=False)
    composer = FakeSpecComposer(error=SpecCompositionError("no valid types"))

    with pytest.raises(SpecCompositionError, match="structure-only fallback is disabled"):
        SpecRendererGenerator(composer=composer).generate_files(
            intent=_intent(),
            change_plan=_plan(),
            repo_patterns=RepoPatterns(),
            target_repo="time4116/iac-smith-demo-infra",
        )


def test_generator_blocks_structure_only_without_model_by_default(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("IAC_SMITH_ALLOW_STRUCTURE_ONLY", raising=False)

    with pytest.raises(StructureOnlyBlocked, match="IAC_SMITH_ALLOW_STRUCTURE_ONLY"):
        SpecRendererGenerator().generate_files(
            intent=_intent(),
            change_plan=_plan(),
            repo_patterns=RepoPatterns(),
            target_repo="time4116/iac-smith-demo-infra",
        )


def test_generator_blocks_when_schema_harvest_unavailable_by_default(monkeypatch):
    _patch_resolver(monkeypatch, contracts={})
    monkeypatch.delenv("IAC_SMITH_ALLOW_STRUCTURE_ONLY", raising=False)

    with pytest.raises(StructureOnlyBlocked, match="schema harvest"):
        SpecRendererGenerator(composer=FakeSpecComposer()).generate_files(
            intent=_intent(),
            change_plan=_plan(),
            repo_patterns=RepoPatterns(),
            target_repo="time4116/iac-smith-demo-infra",
        )


def test_nested_blocks_render_structured_hcl_blocks():
    composition = {
        "resources": [
            {
                "type": "customcloud_database",
                "name": "db",
                "arguments": {"engine": "postgres"},
                "nested_blocks": {
                    "settings": [
                        {"tier": "small", "port": 5432},
                        {"tier": "large", "public": False},
                    ]
                },
            }
        ],
        "outputs": [],
        "assumptions": [],
    }
    composer, _ = _composer([{"resource_types": ["customcloud_database"]}, composition])

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    rendered = render_provider_resources(composed.resources)
    assert rendered.count("settings {") == 2
    assert 'tier = "small"' in rendered
    assert "port = 5432" in rendered
    assert "public = false" in rendered
    assert "blocks" not in rendered
    hcl2.loads(rendered)


def test_unsupported_nested_block_gets_actionable_finding():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={"engine": "postgres"},
                nested_blocks={"parameter_group": [{"name": "force_ssl", "value": "1"}]},
            )
        ]
    )

    errors = validate_composed_component(
        composed,
        provider_contracts=CONTRACTS,
        known_resource_types=set(CONTRACTS),
        allowed_inputs=ALLOWED_INPUTS,
        component_name="database-platform",
    )

    assert any(
        "unsupported nested block `parameter_group`" in error and "provider-declared names" in error
        for error in errors
    )


def test_nested_block_string_values_reject_bare_references():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={"engine": "postgres"},
                nested_blocks={"settings": [{"tier": "customcloud_database.db.id"}]},
            )
        ]
    )

    errors = validate_composed_component(
        composed,
        provider_contracts=CONTRACTS,
        known_resource_types=set(CONTRACTS),
        allowed_inputs=ALLOWED_INPUTS,
        component_name="database-platform",
    )

    assert any("bare Terraform reference" in error for error in errors)


def test_nested_block_entry_keys_must_be_identifiers():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={"engine": "postgres"},
                nested_blocks={"settings": [{"bad-key": "x"}]},
            )
        ]
    )

    errors = validate_composed_component(
        composed,
        provider_contracts=CONTRACTS,
        known_resource_types=set(CONTRACTS),
        allowed_inputs=ALLOWED_INPUTS,
        component_name="database-platform",
    )

    assert any("not a valid identifier" in error for error in errors)


def test_compose_prompt_requests_nested_blocks_not_raw_hcl():
    composer, runtime = _composer([_VALID_SELECTION, _VALID_COMPOSITION])

    composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    assert "nested_blocks" in runtime.prompts[1]
    assert "never raw HCL" in runtime.prompts[1]
    assert '"blocks": ["<nested block HCL>"]' not in runtime.prompts[1]


def test_compose_carries_findings_as_negative_patterns_across_rounds():
    bad = {
        "resources": [
            {
                "type": "customcloud_database",
                "name": "this",
                "arguments": {"engine": "postgres", "publicly_visible": "true"},
            }
        ],
        "outputs": [],
        "assumptions": [],
    }
    composer, runtime = _composer([_VALID_SELECTION, bad, _VALID_COMPOSITION])

    composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    repair_prompt = runtime.prompts[2]
    assert "Known invalid patterns from earlier validation of this run" in repair_prompt
    assert "Correction" in repair_prompt
    assert "publicly_visible" in repair_prompt


def test_compose_rejects_oversized_selection_with_staged_plan(monkeypatch):
    monkeypatch.delenv("IAC_SMITH_MAX_RESOURCE_TYPES", raising=False)
    contracts = {
        f"customcloud_svc{i}_thing": TerraformContract(
            kind="provider_resource",
            name=f"customcloud_svc{i}_thing",
            allowed_arguments=["name"],
            source="fixture schema",
        )
        for i in range(13)
    }
    selection = {"resource_types": sorted(contracts)}
    composer, _ = _composer([selection])

    with pytest.raises(SpecCompositionError) as excinfo:
        composer.compose(
            intent=_intent(),
            component_name="database-platform",
            allowed_inputs=ALLOWED_INPUTS,
            environments=["non-prod"],
            provider_contracts=contracts,
        )

    message = str(excinfo.value)
    assert "too broad" in message
    assert "stage 1:" in message
    assert "IAC_SMITH_MAX_RESOURCE_TYPES" in message


def test_compose_rejects_legacy_raw_blocks_channel_with_actionable_finding():
    legacy = {
        "resources": [
            {
                "type": "customcloud_database",
                "name": "db",
                "arguments": {"engine": "postgres"},
                "blocks": ['settings {\n  tier = "small"\n}'],
            }
        ],
        "outputs": [],
        "assumptions": [],
    }
    composer, runtime = _composer([_VALID_SELECTION, legacy, _VALID_COMPOSITION])

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    assert len(composed.resources) == 2
    repair_prompt = runtime.prompts[2]
    assert "raw-HCL `blocks` channel does not exist" in repair_prompt
    assert "nested_blocks" in repair_prompt


def test_resource_spec_has_no_raw_blocks_field():
    with pytest.raises(Exception, match="[Ee]xtra"):
        ResourceSpec.model_validate(
            {"type": "customcloud_database", "name": "db", "blocks": ["settings {}"]}
        )


# --- Live-run regression (issue #66): fenced/prose-wrapped JSON responses ---


def test_extract_json_object_tolerates_markdown_fence_and_trailing_prose():
    from iac_smith.dynamic_terraform import _extract_json_object

    document = {"resources": [{"type": "aws_vpc", "name": "foundation"}]}
    fenced = f"```json\n{json.dumps(document)}\n```"
    fenced_with_tail = (
        f"{fenced}\n\nThe composition above uses this pattern:\n"
        '```hcl\nresource "aws_vpc" "foundation" {}\n```'
    )
    preamble = f"Here is the JSON (shape: {{...}}):\n{fenced}"
    trailing_junk = f"{json.dumps(document)}\nNote: braces like }} are fine."

    assert _extract_json_object(fenced) == document
    assert _extract_json_object(fenced_with_tail) == document
    assert _extract_json_object(preamble) == document
    assert _extract_json_object(trailing_junk) == document


def test_extract_json_object_still_rejects_garbage():
    import pytest as _pytest

    from iac_smith.dynamic_terraform import _extract_json_object

    with _pytest.raises(ValueError):
        _extract_json_object("no json here at all")
    with _pytest.raises(ValueError):
        _extract_json_object('```json\n{"unclosed": [\n```')


def test_compose_accepts_fenced_response_like_live_issue_66_run():
    # The live stage-1 run failed all composition rounds because the model
    # wrapped its (otherwise valid) document in a ```json fence with content
    # after it. The extractor must recover the document on round 1.
    fenced = (
        "```json\n"
        + json.dumps(_VALID_COMPOSITION)
        + "\n```\n\nThis composition follows the module's allowed inputs."
    )
    composer, runtime = _composer([_VALID_SELECTION, fenced])

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
    assert len(runtime.prompts) == 2  # no repair round was needed


def test_unparseable_error_reports_head_and_tail_of_response():
    body = "x" * 400
    garbage = f"prose start {body} prose end, no json"
    composer, _ = _composer([garbage, garbage])

    with pytest.raises(SpecCompositionError) as excinfo:
        composer.compose(
            intent=_intent(),
            component_name="database-platform",
            allowed_inputs=ALLOWED_INPUTS,
            environments=["non-prod"],
            provider_contracts=CONTRACTS,
        )

    message = str(excinfo.value)
    assert "Response began:" in message
    assert "and ended:" in message


def test_extract_json_object_error_pinpoints_decode_position():
    from iac_smith.dynamic_terraform import _extract_json_object

    # A defect *inside* the document (invalid escape) defeats every extraction
    # candidate; the error must carry the decode position and surrounding text.
    broken = '```json\n{"resources": [{"name": "bad \\x escape here"}]}\n```'

    with pytest.raises(ValueError) as excinfo:
        _extract_json_object(broken)

    message = str(excinfo.value)
    assert "at character" in message
    assert "near:" in message
    assert "escape" in message.lower()


def test_composer_logs_full_response_when_unparseable():
    logs: list[str] = []
    garbage = '{"resources": [{"name": "bad \\x escape"}]}'
    composer, _ = _composer([garbage, garbage], logger=logs.append)

    with pytest.raises(SpecCompositionError):
        composer.compose(
            intent=_intent(),
            component_name="database-platform",
            allowed_inputs=ALLOWED_INPUTS,
            environments=["non-prod"],
            provider_contracts=CONTRACTS,
        )

    dumps = [line for line in logs if "unparseable model response" in line]
    assert dumps
    assert "bad \\x escape" in dumps[0]


def test_extract_json_object_repairs_unquoted_bare_expression_values():
    from iac_smith.dynamic_terraform import _extract_json_object

    # The exact live issue #66 defect: output values emitted as unquoted JSON
    # tokens, inside a fenced document.
    broken = (
        "```json\n"
        '{"resources":[{"type":"aws_vpc","name":"foundation",'
        '"arguments":{"cidr_block":"10.0.0.0/16"}}],'
        '"outputs":[{"name":"vpc_id","description":"VPC id",'
        '"value":aws_vpc.foundation.id},'
        '{"name":"vpc_cidr","description":"CIDR",'
        '"value":aws_vpc.foundation.cidr_block}],'
        '"assumptions":[]}\n```'
    )

    document = _extract_json_object(broken)

    assert document["outputs"][0]["value"] == "aws_vpc.foundation.id"
    assert document["outputs"][1]["value"] == "aws_vpc.foundation.cidr_block"


def test_extract_json_object_does_not_quote_non_reference_tokens():
    import pytest as _pytest

    from iac_smith.dynamic_terraform import _extract_json_object

    # A dotless bare token is not reference-shaped; it must stay a parse error
    # rather than being silently quoted into a string.
    with _pytest.raises(ValueError):
        _extract_json_object('{"value": bogus}')


def test_compose_recovers_live_issue_66_unquoted_output_payload():
    fenced_with_bare_outputs = (
        "```json\n"
        '{"resources":[{"type":"customcloud_database","name":"db",'
        '"arguments":{"engine":"postgres"}}],'
        '"outputs":[{"name":"database_ref","description":"Ref",'
        '"value":customcloud_database.db.id}],"assumptions":[]}\n'
        "```"
    )
    composer, runtime = _composer(
        [{"resource_types": ["customcloud_database"]}, fenced_with_bare_outputs]
    )

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    assert composed.outputs[0].value == "customcloud_database.db.id"
    assert len(runtime.prompts) == 2  # parsed on round 1, no repair round


def test_compose_prompt_forbids_unquoted_output_tokens():
    composer, runtime = _composer([_VALID_SELECTION, _VALID_COMPOSITION])

    composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    assert "never emit an unquoted token" in runtime.prompts[1]
    assert "not a JSON template" not in runtime.prompts[1]


# --- Live showcase-run regressions (issues #68/#69/#70) ---


def test_spec_renderer_repair_files_recomposes_with_runtime_findings(monkeypatch):
    _patch_resolver(monkeypatch)

    class RecordingComposer:
        def __init__(self):
            self.negative_patterns_seen: list[list[str]] = []

        def compose(self, **kwargs):
            self.negative_patterns_seen.append(list(kwargs.get("negative_patterns") or []))
            return ComposedComponent.model_validate(_VALID_COMPOSITION)

    composer = RecordingComposer()
    generator = SpecRendererGenerator(composer=composer)
    ansi_error = (
        "terraform validate failed:\n"
        "\x1b[31m│\x1b[0m Error: Unsupported argument\n"
        '\x1b[31m│\x1b[0m An argument named "index_name" is not expected here.'
    )

    files = generator.repair_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
        generated_files={},
        repair_errors=[ansi_error],
    )

    assert 'resource "customcloud_network" "this"' in files["modules/database-platform/main.tf"]
    carried = composer.negative_patterns_seen[-1]
    assert any("index_name" in pattern for pattern in carried)
    # ANSI escapes must not reach the prompt.
    assert all("\x1b" not in pattern for pattern in carried)

    # A second repair round must still remember the first round's findings.
    generator.repair_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
        generated_files={},
        repair_errors=["second failure"],
    )
    carried_second = composer.negative_patterns_seen[-1]
    assert any("index_name" in pattern for pattern in carried_second)
    assert any("second failure" in pattern for pattern in carried_second)


def test_compose_prompt_forbids_minified_json_and_data_sources():
    composer, runtime = _composer([_VALID_SELECTION, _VALID_COMPOSITION])

    composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
    )

    prompt = runtime.prompts[1]
    assert "do NOT" in prompt and "minify" in prompt
    assert "Data sources do not exist here" in prompt
    assert "omit its optional name argument" in prompt


def test_data_reference_finding_names_the_unique_name_alternative():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={
                    "engine": "postgres",
                    "name": "app-${data.customcloud_caller.account_id}",
                },
            )
        ]
    )

    errors = _validate(composed)

    assert any(
        "`data` values do not exist" in error and "omit the optional name argument" in error
        for error in errors
    )


# --- Second showcase round (runs on issues #68/#69/#70): renderer semantics ---


def test_block_typed_arguments_are_canonicalized_and_rendered_as_blocks():
    from iac_smith.spec_composer import normalize_composed_blocks

    contracts = {
        "customcloud_pipeline": TerraformContract(
            kind="provider_resource",
            name="customcloud_pipeline",
            allowed_arguments=["name", "delivery", "tags"],
            required_arguments=["name"],
            block_names=["delivery", "logging"],
            source="fixture schema",
        )
    }
    # Terraform JSON semantics: the model expresses blocks as "name": [{...}]
    # inside arguments — including an inner block ("logging") nested deeper.
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_pipeline",
                name="this",
                arguments={
                    "name": "pipeline",
                    "delivery": [
                        {
                            "destination": "bucket",
                            "logging": [{"enabled": True, "group": "logs"}],
                        }
                    ],
                    "tags": {"Environment": "${var.environment}"},
                },
            )
        ]
    )

    normalized = normalize_composed_blocks(composed, contracts)
    resource = normalized.resources[0]
    assert "delivery" not in resource.arguments
    assert resource.nested_blocks["delivery"][0]["destination"] == "bucket"
    # `tags` is a plain map attribute, not a block — it must stay an argument.
    assert "tags" in resource.arguments

    rendered = render_provider_resources(
        normalized.resources, {"customcloud_pipeline": ["delivery", "logging"]}
    )
    assert "delivery {" in rendered
    assert "logging {" in rendered
    assert "delivery = [" not in rendered
    assert "logging = [" not in rendered
    assert "tags = {" in rendered
    hcl2.loads(rendered)


def test_sole_interpolation_jsonencode_renders_as_bare_expression():
    from iac_smith.spec_renderer import _sole_interpolation_expression

    policy = '${jsonencode({"Version":"2012-10-17","Statement":[{"Effect":"Allow"}]})}'
    assert (
        _sole_interpolation_expression(policy)
        == 'jsonencode({"Version":"2012-10-17","Statement":[{"Effect":"Allow"}]})'
    )
    # Mixed templates keep template semantics.
    assert _sole_interpolation_expression("prefix-${var.environment}") is None
    assert _sole_interpolation_expression("${var.a}-${var.b}") is None

    rendered = render_provider_resources(
        [
            ResourceSpec(
                type="customcloud_database",
                name="db",
                arguments={"engine": "postgres", "name": policy},
            )
        ]
    )
    assert 'name = jsonencode({"Version":"2012-10-17"' in rendered
    assert "\\" not in rendered
    hcl2.loads(rendered)


def test_contracts_from_provider_schema_collects_nested_block_names():
    from iac_smith.blackboard import contracts_from_provider_schema

    schema = {
        "provider_schemas": {
            "registry.terraform.io/hashicorp/customcloud": {
                "resource_schemas": {
                    "customcloud_bucket_encryption": {
                        "block": {
                            "attributes": {"bucket": {"type": "string", "required": True}},
                            "block_types": {
                                "rule": {
                                    "block": {
                                        "block_types": {
                                            "apply_server_side_encryption_by_default": {
                                                "block": {
                                                    "attributes": {
                                                        "sse_algorithm": {"type": "string"}
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            },
                        }
                    }
                }
            }
        }
    }

    contracts = contracts_from_provider_schema(schema)
    contract = contracts["customcloud_bucket_encryption"]

    assert contract.block_names == ["apply_server_side_encryption_by_default", "rule"]
    assert "rule" in contract.allowed_arguments


def test_compose_carries_block_names_for_the_renderer():
    contracts = {
        "customcloud_database": TerraformContract(
            kind="provider_resource",
            name="customcloud_database",
            allowed_arguments=[
                "engine",
                "name",
                "network_ref",
                "port",
                "public",
                "settings",
                "tags",
            ],
            required_arguments=["engine"],
            block_names=["settings"],
            source="fixture schema",
        )
    }
    composition = {
        "resources": [
            {
                "type": "customcloud_database",
                "name": "db",
                # Block expressed Terraform-JSON style inside arguments.
                "arguments": {"engine": "postgres", "settings": [{"tier": "small"}]},
            }
        ],
        "outputs": [],
        "assumptions": [],
    }
    composer, _ = _composer([{"resource_types": ["customcloud_database"]}, composition])

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=contracts,
    )

    assert composed.block_names == {"customcloud_database": ["settings"]}
    assert composed.resources[0].nested_blocks["settings"] == [{"tier": "small"}]
    rendered = render_provider_resources(composed.resources, composed.block_names)
    assert "settings {" in rendered
    assert 'tier = "small"' in rendered


# --- Third showcase round (issue #70): bidirectional canonicalization, inner requirements ---


_ALARM_CONTRACTS = {
    "customcloud_alarm": TerraformContract(
        kind="provider_resource",
        name="customcloud_alarm",
        allowed_arguments=["name", "dimensions", "index"],
        required_arguments=["name"],
        block_names=["index"],
        block_required_arguments={"index": ["projection_type"]},
        source="fixture schema",
    )
}


def test_attribute_placed_in_nested_blocks_moves_back_to_arguments():
    from iac_smith.spec_composer import normalize_composed_blocks

    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_alarm",
                name="throttle",
                arguments={"name": "throttle"},
                # `dimensions` is a plain map attribute; the live #70 run put it
                # in nested_blocks and Terraform rejected the rendered block.
                nested_blocks={"dimensions": [{"TableName": "sessions"}]},
            )
        ]
    )

    normalized = normalize_composed_blocks(composed, _ALARM_CONTRACTS)
    resource = normalized.resources[0]

    assert "dimensions" not in resource.nested_blocks
    assert resource.arguments["dimensions"] == {"TableName": "sessions"}
    rendered = render_provider_resources(normalized.resources, {"customcloud_alarm": ["index"]})
    assert "dimensions = {" in rendered
    assert "dimensions {" not in rendered
    hcl2.loads(rendered)


def test_missing_required_argument_inside_nested_block_is_a_finding():
    composed = ComposedComponent(
        resources=[
            ResourceSpec(
                type="customcloud_alarm",
                name="throttle",
                arguments={"name": "throttle"},
                # The live #70 GSI failure shape: block present, required inner
                # argument absent.
                nested_blocks={"index": [{"name": "by_entity"}]},
            )
        ]
    )

    errors = validate_composed_component(
        composed,
        provider_contracts=_ALARM_CONTRACTS,
        known_resource_types=set(_ALARM_CONTRACTS),
        allowed_inputs=ALLOWED_INPUTS,
        component_name="session-store",
    )

    assert any(
        "nested block `index` entry 1 is missing required argument `projection_type`" in error
        for error in errors
    )


def test_repair_composes_iteratively_against_previous_composition():
    previous = ComposedComponent.model_validate(_VALID_COMPOSITION)
    composer, runtime = _composer([_VALID_COMPOSITION])

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
        previous=previous,
        runtime_findings=["Error: all attributes must be indexed. Unused attributes: [x]"],
    )

    # No type-selection call happens on repair: one prompt total.
    assert len(runtime.prompts) == 1
    prompt = runtime.prompts[0]
    assert "Your previous composition is below" in prompt
    assert "SMALLEST change" in prompt
    assert "all attributes must be indexed" in prompt
    assert '"customcloud_network"' in prompt
    assert len(composed.resources) == 2


def test_spec_renderer_repair_hands_previous_composition_to_composer(monkeypatch):
    _patch_resolver(monkeypatch)

    class RecordingComposer:
        def __init__(self):
            self.calls: list[dict] = []

        def compose(self, **kwargs):
            self.calls.append(kwargs)
            return ComposedComponent.model_validate(_VALID_COMPOSITION)

    composer = RecordingComposer()
    generator = SpecRendererGenerator(composer=composer)

    generator.generate_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )
    generator.repair_files(
        intent=_intent(),
        change_plan=_plan(),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
        generated_files={},
        repair_errors=["terraform plan failed: all attributes must be indexed"],
    )

    first, second = composer.calls
    assert first["previous"] is None
    assert second["previous"] is not None
    assert second["previous"].resources[0].type == "customcloud_network"
    assert any("all attributes must be indexed" in f for f in second["runtime_findings"])


_CLOUDFRONT_CONTRACT_KW = dict(
    source="terraform-aws-modules/cloudfront/aws",
    version="5.0.1",
    description="CloudFront distribution module",
    inputs={
        "comment": {"name": "comment", "type": "string", "required": False},
        "origin": {"name": "origin", "type": "any", "required": True},
        "enabled": {"name": "enabled", "type": "bool", "required": False},
    },
    outputs=["cloudfront_distribution_id", "cloudfront_distribution_arn"],
)


def _registry_candidates():
    from iac_smith.registry_modules import RegistryModuleContract

    return [RegistryModuleContract(**_CLOUDFRONT_CONTRACT_KW)]


_VALID_MODULE_SELECTION = {
    "registry_module": {
        "source": "terraform-aws-modules/cloudfront/aws",
        "inputs": {
            "comment": "Site for ${var.environment}",
            "origin": {"s3": {"domain_name": "example.s3.amazonaws.com"}},
        },
        "outputs": ["cloudfront_distribution_id"],
    }
}


def test_compose_selects_registry_module_and_pins_contract_version():
    composer, runtime = _composer([_VALID_MODULE_SELECTION])

    composed = composer.compose(
        intent=_intent(),
        component_name="static-site",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
        registry_candidates=_registry_candidates(),
    )

    module = composed.registry_module
    assert module is not None
    assert module.source == "terraform-aws-modules/cloudfront/aws"
    assert module.version == "5.0.1"
    assert composed.resources == []
    assert [output.value for output in composed.outputs] == [
        "module.this.cloudfront_distribution_id"
    ]
    assert "required inputs: origin" in runtime.prompts[0]
    assert any("community module" in a.lower() for a in composed.assumptions)


def test_compose_falls_back_to_resources_when_model_declines_module():
    composer, _runtime = _composer(
        [{"registry_module": None}, _VALID_SELECTION, _VALID_COMPOSITION]
    )

    composed = composer.compose(
        intent=_intent(),
        component_name="database-platform",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
        registry_candidates=_registry_candidates(),
    )

    assert composed.registry_module is None
    assert [r.type for r in composed.resources] == [
        "customcloud_network",
        "customcloud_database",
    ]


def test_registry_module_selection_is_validated_and_repaired():
    invalid = {
        "registry_module": {
            "source": "terraform-aws-modules/cloudfront/aws",
            "inputs": {"orign": {"s3": {}}, "comment": "${data.aws_caller_identity.current.id}"},
            "outputs": ["nonexistent_output"],
        }
    }
    composer, runtime = _composer([invalid, _VALID_MODULE_SELECTION])

    composed = composer.compose(
        intent=_intent(),
        component_name="static-site",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
        registry_candidates=_registry_candidates(),
    )

    assert composed.registry_module is not None
    repair_prompt = runtime.prompts[1]
    assert "does not define input(s): `orign`" in repair_prompt
    assert "missing required input(s): `origin`" in repair_prompt
    assert "does not define output(s): `nonexistent_output`" in repair_prompt
    assert "no sibling resources" in repair_prompt


def test_registry_module_hallucinated_source_is_rejected():
    hallucinated = {
        "registry_module": {"source": "terraform-aws-modules/made-up/aws", "inputs": {}}
    }
    composer, runtime = _composer([hallucinated, _VALID_MODULE_SELECTION])

    composed = composer.compose(
        intent=_intent(),
        component_name="static-site",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
        registry_candidates=_registry_candidates(),
    )

    assert composed.registry_module is not None
    assert "is not one of the offered community modules" in runtime.prompts[1]


def test_registry_module_repair_edits_previous_call():
    previous = ComposedComponent.model_validate(
        {
            "registry_module": {
                "source": "terraform-aws-modules/cloudfront/aws",
                "version": "5.0.1",
                "inputs": {"origin": {"s3": {}}, "enabled": "yes"},
                "outputs": ["cloudfront_distribution_id"],
            }
        }
    )
    composer, runtime = _composer([_VALID_MODULE_SELECTION])

    composed = composer.compose(
        intent=_intent(),
        component_name="static-site",
        allowed_inputs=ALLOWED_INPUTS,
        environments=["non-prod"],
        provider_contracts=CONTRACTS,
        previous=previous,
        runtime_findings=['expected bool, got "yes"'],
        registry_candidates=_registry_candidates(),
    )

    assert composed.registry_module is not None
    assert "SMALLEST change" in runtime.prompts[0]
    assert "expected bool" in runtime.prompts[0]


def test_registry_module_repair_refuses_to_abandon_module():
    previous = ComposedComponent.model_validate(
        {
            "registry_module": {
                "source": "terraform-aws-modules/cloudfront/aws",
                "version": "5.0.1",
                "inputs": {"origin": {"s3": {}}},
            }
        }
    )
    composer, _runtime = _composer([{"registry_module": None}])

    with pytest.raises(SpecCompositionError, match="abandoned"):
        composer.compose(
            intent=_intent(),
            component_name="static-site",
            allowed_inputs=ALLOWED_INPUTS,
            environments=["non-prod"],
            provider_contracts=CONTRACTS,
            previous=previous,
            runtime_findings=["some plan error"],
            registry_candidates=_registry_candidates(),
        )


def test_registry_module_renders_pinned_module_call(monkeypatch):
    _patch_resolver(monkeypatch)
    composer, _runtime = _composer([_VALID_MODULE_SELECTION])
    generator = SpecRendererGenerator(composer=composer)
    monkeypatch.setattr(
        "iac_smith.registry_modules.discover_registry_candidates",
        lambda intent, logger=None: _registry_candidates(),
    )
    monkeypatch.setenv("IAC_SMITH_REGISTRY_MODULES", "1")

    files = generator.generate_files(
        intent=_intent(),
        change_plan=_plan("static-site"),
        repo_patterns=RepoPatterns(),
        target_repo="time4116/iac-smith-demo-infra",
    )

    main = files["modules/static-site/main.tf"]
    assert 'module "this" {' in main
    assert 'source  = "terraform-aws-modules/cloudfront/aws"' in main
    assert 'version = "5.0.1"' in main
    assert 'comment = "Site for ${var.environment}"' in main
    assert hcl2.loads(main)
    outputs = files["modules/static-site/outputs.tf"]
    assert "module.this.cloudfront_distribution_id" in outputs
    from iac_smith.legitimacy import check_pr_legitimacy, workload_module_calls

    assert workload_module_calls(files) == ["module.this (terraform-aws-modules/cloudfront/aws)"]
    assert (
        check_pr_legitimacy(
            generated_files=files,
            change_plan=_plan("static-site"),
            intent=_intent().model_copy(update={"resource_type": "cloudfront_distribution"}),
        )
        == []
    )
