import json

import pytest

from iac_smith.adherence import (
    AdherenceReview,
    AdherenceReviewer,
    ConstraintVerdict,
    default_adherence_reviewer,
    unpinned_image_findings,
)
from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.spec_composer import SpecCompositionError


class FakeStreamRuntime:
    """Bedrock runtime double replaying canned JSON payloads over the stream shape."""

    def __init__(self, payloads: list[dict | str], stop_reason: str = "end_turn"):
        self.payloads = [
            payload if isinstance(payload, str) else json.dumps(payload) for payload in payloads
        ]
        self.stop_reason = stop_reason
        self.prompts: list[str] = []

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


def _intent(**overrides) -> InfrastructureIntent:
    payload = dict(
        raw_request="Run an nginx service on Fargate with tasks in the public subnets.",
        resource_type="ecs_fargate_service",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
        constraints=["tasks run in the public subnets with public IPs assigned", "no NAT gateway"],
    )
    payload.update(overrides)
    return InfrastructureIntent(**payload)


def _reviewer(payloads, stop_reason="end_turn", **kwargs):
    runtime = FakeStreamRuntime(payloads, stop_reason=stop_reason)
    reviewer = AdherenceReviewer(model_id="fixture-model", bedrock_runtime=runtime, **kwargs)
    return reviewer, runtime


# --- The showcase ECS regression fixture (acceptance criterion) ---


def test_private_subnet_composition_violating_public_subnet_constraint_is_a_finding():
    # The exact live failure: the issue required public subnets + public IPs, the
    # composition put the service in private subnets with assign_public_ip = false.
    document = {
        "resources": [
            {
                "type": "aws_ecs_service",
                "name": "this",
                "arguments": {"name": "nginx"},
                "nested_blocks": {
                    "network_configuration": [
                        {
                            "subnets": ["${aws_subnet.private.id}"],
                            "assign_public_ip": False,
                        }
                    ]
                },
            }
        ],
        "outputs": [],
    }
    review_response = {
        "constraints": [
            {
                "constraint": "tasks run in the public subnets with public IPs assigned",
                "verdict": "violated",
                "evidence": "network_configuration uses aws_subnet.private, assign_public_ip=false",
            },
            {
                "constraint": "no NAT gateway",
                "verdict": "satisfied",
                "evidence": "no aws_nat_gateway resource is composed",
            },
        ],
        "viability_findings": [
            "Fargate tasks in private subnets with no NAT and no public IP cannot pull the "
            "nginx image or reach CloudWatch Logs."
        ],
    }
    reviewer, runtime = _reviewer([review_response])

    review = reviewer.review(intent=_intent(), component_name="ecs-service", document=document)

    violations = review.violation_findings()
    assert len(violations) == 1
    assert "public subnets with public IPs" in violations[0]
    assert "assign_public_ip=false" in violations[0]
    # The composed JSON is put in front of the reviewer verbatim.
    assert "assign_public_ip" in runtime.prompts[0]
    assert "tasks run in the public subnets" in runtime.prompts[0]
    # Runtime viability is advisory, carried separately from blocking violations.
    viability = review.viability_repair_findings()
    assert viability
    assert all(f.startswith("Runtime-viability concern:") for f in viability)


def test_satisfied_constraints_produce_no_violations():
    document = {"resources": [{"type": "aws_s3_bucket", "name": "this", "arguments": {}}]}
    response = {
        "constraints": [
            {"constraint": "versioning enabled", "verdict": "satisfied", "evidence": "block"},
        ],
        "viability_findings": [],
    }
    reviewer, _ = _reviewer([response])

    review = reviewer.review(
        intent=_intent(constraints=["versioning enabled"]),
        component_name="bucket",
        document=document,
    )

    assert review.violation_findings() == []
    assert review.viability_repair_findings() == []


def test_review_works_for_registry_module_document():
    document = {
        "registry_module": {
            "source": "terraform-aws-modules/ecs/aws",
            "inputs": {"assign_public_ip": True},
            "outputs": ["cluster_arn"],
        }
    }
    response = {
        "constraints": [
            {"constraint": "public IPs assigned", "verdict": "satisfied", "evidence": "ip true"},
        ],
        "viability_findings": [],
    }
    reviewer, runtime = _reviewer([response])

    review = reviewer.review(
        intent=_intent(constraints=["public IPs assigned"]),
        component_name="ecs",
        document=document,
    )

    assert review.violation_findings() == []
    assert "terraform-aws-modules/ecs/aws" in runtime.prompts[0]


def test_no_constraints_returns_empty_verdicts_without_requiring_alignment():
    reviewer, _ = _reviewer([{"constraints": [], "viability_findings": []}])

    review = reviewer.review(
        intent=_intent(constraints=[]),
        component_name="thing",
        document={"resources": []},
    )

    assert review.constraints == []
    assert review.violation_findings() == []


def test_review_repairs_verdict_count_mismatch_then_blocks():
    two_constraints = _intent()  # two stated constraints
    short = {"constraints": [{"constraint": "x", "verdict": "satisfied"}], "viability_findings": []}
    reviewer, runtime = _reviewer([short, short])

    with pytest.raises(SpecCompositionError, match="one verdict per stated constraint"):
        reviewer.review(intent=two_constraints, component_name="ecs", document={"resources": []})

    assert "exactly one verdict per stated constraint" in runtime.prompts[1]


def test_review_maps_verdicts_positionally_and_uses_stated_wording():
    # The model paraphrases the constraint text; the stated wording must win so
    # downstream findings quote the issue, not the paraphrase.
    response = {
        "constraints": [
            {"constraint": "put tasks in public subnets", "verdict": "violated", "evidence": "x"},
            {"constraint": "avoid NAT", "verdict": "satisfied", "evidence": "none"},
        ],
        "viability_findings": [],
    }
    reviewer, _ = _reviewer([response])

    review = reviewer.review(intent=_intent(), component_name="ecs", document={"resources": []})

    assert [c.constraint for c in review.constraints] == [
        "tasks run in the public subnets with public IPs assigned",
        "no NAT gateway",
    ]


def test_review_repairs_unparseable_response():
    good = {"constraints": [], "viability_findings": []}
    reviewer, runtime = _reviewer(["not json at all", good])

    review = reviewer.review(
        intent=_intent(constraints=[]), component_name="x", document={"resources": []}
    )

    assert review.constraints == []
    assert "could not be parsed" in runtime.prompts[1]


def test_review_raises_on_token_cap_truncation():
    reviewer, _ = _reviewer([{"constraints": []}], stop_reason="max_tokens")

    with pytest.raises(SpecCompositionError, match="truncated"):
        reviewer.review(intent=_intent(), component_name="x", document={"resources": []})


def test_adherence_review_model_helpers():
    review = AdherenceReview(
        constraints=[
            ConstraintVerdict(constraint="a", verdict="violated", evidence="because reasons"),
            ConstraintVerdict(constraint="b", verdict="satisfied"),
            ConstraintVerdict(constraint="c", verdict="not_applicable"),
        ],
        viability_findings=["egress path missing"],
    )

    violations = review.violation_findings()
    assert len(violations) == 1
    assert violations[0].startswith('Stated constraint violated: "a"')
    assert "because reasons" in violations[0]
    assert review.viability_repair_findings() == ["Runtime-viability concern: egress path missing"]


# --- Deterministic unpinned-image lint (provider-agnostic string hygiene) ---


def test_unpinned_image_findings_flags_tagless_and_latest():
    document = {
        "resources": [
            {"type": "aws_ecs_task_definition", "name": "a", "arguments": {"image": "nginx"}},
            {"type": "aws_ecs_task_def", "name": "b", "arguments": {"web_image": "n:latest"}},
        ]
    }

    findings = unpinned_image_findings(document)

    assert len(findings) == 2
    assert any("`nginx`" in f for f in findings)
    assert any("`n:latest`" in f for f in findings)


def test_unpinned_image_findings_accepts_pinned_references():
    document = {
        "resources": [
            {"type": "x", "name": "a", "arguments": {"image": "nginx:1.27.4"}},
            {"type": "x", "name": "b", "arguments": {"image": "nginx@sha256:abc123"}},
            {"type": "x", "name": "c", "arguments": {"image": "${var.image}"}},
        ]
    }

    assert unpinned_image_findings(document) == []


def test_unpinned_image_findings_reads_jsonencoded_container_definitions():
    document = {
        "resources": [
            {
                "type": "aws_ecs_task_definition",
                "name": "this",
                "arguments": {
                    "container_definitions": '${jsonencode([{"name":"web","image":"nginx"}])}'
                },
            }
        ]
    }

    findings = unpinned_image_findings(document)

    assert len(findings) == 1
    assert "`nginx`" in findings[0]


def test_unpinned_image_findings_ignores_non_image_keys():
    document = {"resources": [{"type": "x", "name": "a", "arguments": {"image_id": "ami-123"}}]}

    # image_id is not an image reference key (does not end in a bare `image`).
    assert unpinned_image_findings(document) == []


# --- Factory / offline behaviour ---


def test_default_reviewer_disabled_without_model(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("BEDROCK_ESCALATION_MODEL_ID", raising=False)

    assert default_adherence_reviewer() is None


def test_default_reviewer_opt_out(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "some-model")
    monkeypatch.setenv("IAC_SMITH_ADHERENCE_GATE", "0")

    assert default_adherence_reviewer() is None


def test_default_reviewer_prefers_escalation_model(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "primary-model")
    monkeypatch.setenv("BEDROCK_ESCALATION_MODEL_ID", "escalation-model")
    monkeypatch.delenv("IAC_SMITH_ADHERENCE_GATE", raising=False)

    reviewer = default_adherence_reviewer()

    assert reviewer is not None
    assert reviewer.model_id == "escalation-model"


def test_default_reviewer_falls_back_to_primary_model(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "primary-model")
    monkeypatch.delenv("BEDROCK_ESCALATION_MODEL_ID", raising=False)
    monkeypatch.delenv("IAC_SMITH_ADHERENCE_GATE", raising=False)

    reviewer = default_adherence_reviewer()

    assert reviewer is not None
    assert reviewer.model_id == "primary-model"
