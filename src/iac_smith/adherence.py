"""Constraint-adherence gate: verify composed specs against stated requirements.

The live showcase ECS run produced a stack that was schema-valid, plan-clean,
and apply-clean — and dead at runtime: the issue required tasks in public
subnets with public IPs, the composition used private subnets with
``assign_public_ip`` disabled, and Fargate had no egress path. Every
deterministic gate was satisfied because the failure was a violated *stated
requirement*, which no schema or plan can see.

This module reviews a schema-valid composition against the constraints the
intent parser lifted from the issue text. It stays generic per the
no-golden-paths principle: constraints are request-derived strings, the
judgment lives in the model (preferring ``BEDROCK_ESCALATION_MODEL_ID`` —
checking is cheaper than generating, and a separate pass breaks the composer's
blind spot), and the only deterministic check here is provider-agnostic string
hygiene (unpinned container-image references).
"""

import json
import os
import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from iac_smith.dynamic_terraform import BedrockRuntime, _int_env
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.spec_composer import SpecCompositionError, invoke_json_document

VIOLATION_PREFIX = "Stated constraint violated"
VIABILITY_PREFIX = "Runtime-viability concern"


class ConstraintVerdict(BaseModel):
    constraint: str
    verdict: Literal["satisfied", "violated", "not_applicable"]
    evidence: str = ""


class AdherenceReview(BaseModel):
    constraints: list[ConstraintVerdict] = Field(default_factory=list)
    viability_findings: list[str] = Field(default_factory=list)

    def violation_findings(self) -> list[str]:
        return [
            f'{VIOLATION_PREFIX}: "{item.constraint}" — '
            + (item.evidence or "the composed implementation contradicts it")
            + ". Change the composition so this constraint holds."
            for item in self.constraints
            if item.verdict == "violated"
        ]

    def viability_repair_findings(self) -> list[str]:
        return [f"{VIABILITY_PREFIX}: {finding}" for finding in self.viability_findings]


def _shape_findings(exc: ValidationError) -> list[str]:
    return [
        f"Response field `{'.'.join(str(part) for part in err['loc'])}`: {err['msg']}. "
        "Match the required JSON shape exactly."
        for err in exc.errors()[:12]
    ]


def _normalized(text: str) -> str:
    return " ".join(text.split()).lower()


class AdherenceReviewer:
    """One post-composition review call: constraint verdicts plus viability.

    The reviewer never rewrites the composition; it returns per-constraint
    ``satisfied | violated | not_applicable`` verdicts with evidence, and a
    best-effort answer to "will these resources function at runtime?". The
    composer turns violations into blocking repair findings and viability
    concerns into a single advisory repair round.
    """

    def __init__(
        self,
        model_id: str | None = None,
        bedrock_runtime: BedrockRuntime | None = None,
        *,
        read_timeout_seconds: int = 180,
        max_attempts: int = 2,
        max_tokens: int = 4096,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.model_id = model_id or _default_adherence_model_id()
        if not self.model_id:
            raise ValueError(
                "BEDROCK_ESCALATION_MODEL_ID or BEDROCK_MODEL_ID must be set "
                "to review constraint adherence."
            )
        self._bedrock_runtime = bedrock_runtime
        self.read_timeout_seconds = _int_env("IAC_SMITH_BEDROCK_READ_TIMEOUT", read_timeout_seconds)
        self.max_attempts = _int_env("IAC_SMITH_BEDROCK_MAX_ATTEMPTS", max_attempts)
        self.max_tokens = _int_env("IAC_SMITH_ADHERENCE_MAX_TOKENS", max_tokens)
        self.logger = logger

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    @property
    def bedrock_runtime(self) -> BedrockRuntime:
        if self._bedrock_runtime is None:
            import boto3
            from botocore.config import Config

            region = os.getenv("AWS_REGION", "us-west-2")
            self._bedrock_runtime = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=Config(
                    connect_timeout=10,
                    read_timeout=self.read_timeout_seconds,
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
        return self._bedrock_runtime

    def _prompt(
        self,
        *,
        intent: InfrastructureIntent,
        component_name: str,
        document: dict,
        repair_hints: list[str],
    ) -> str:
        lines = [
            "You are IaC Smith's constraint-adherence reviewer. A separate composer",
            "selected the typed Terraform implementation below for a repository issue.",
            "Judge only what the JSON contains — never rewrite it, never add prose.",
            "",
            "Request from the repository issue:",
            intent.raw_request,
            "",
            "Deployment context:",
            f"- Region: {intent.region}",
            f"- Terraform module: modules/{component_name}",
        ]
        if intent.constraints:
            lines += [
                "",
                "Stated constraints extracted from the issue:",
                *(f"{i}. {constraint}" for i, constraint in enumerate(intent.constraints, 1)),
            ]
        lines += [
            "",
            "Composed implementation (JSON):",
            json.dumps(document, indent=2, sort_keys=True),
            "",
            "Return ONLY JSON:",
            '{"constraints": [{"constraint": "<the constraint verbatim>",',
            '  "verdict": "satisfied" | "violated" | "not_applicable",',
            '  "evidence": "<the argument or value that satisfies or contradicts it>"},',
            '  ...], "viability_findings": ["<concrete runtime concern>", ...]}',
            "",
            "Rules:",
            "- Return exactly one verdict per stated constraint, in order, quoting each",
            "  constraint verbatim. Return [] for `constraints` when none are stated.",
            "- `satisfied` and `violated` must cite the concrete argument or value as",
            "  evidence; use `not_applicable` only when the constraint concerns",
            "  something this composition cannot express.",
            "- `viability_findings` answers: will these resources function at runtime?",
            "  Consider network reachability for image pulls, log delivery, and service",
            "  dependencies. List each concrete concern; return [] when none.",
        ]
        if repair_hints:
            lines += [
                "",
                "Your previous response was invalid. Fix every point below:",
                *(f"- {hint}" for hint in repair_hints),
            ]
        return "\n".join(lines)

    def review(
        self, *, intent: InfrastructureIntent, component_name: str, document: dict
    ) -> AdherenceReview:
        repair_hints: list[str] = []
        for attempt in range(2):
            prompt = self._prompt(
                intent=intent,
                component_name=component_name,
                document=document,
                repair_hints=repair_hints,
            )
            try:
                payload = invoke_json_document(
                    self.bedrock_runtime,
                    prompt,
                    model_id=self.model_id,
                    max_tokens=self.max_tokens,
                    max_attempts=self.max_attempts,
                    truncation_message=(
                        "Adherence review response was truncated at the output token "
                        "cap; raise IAC_SMITH_ADHERENCE_MAX_TOKENS."
                    ),
                    logger=self.logger,
                    log_label="adherence review",
                )
            except ValueError as exc:
                if attempt == 0:
                    repair_hints = [
                        f"Your previous response could not be parsed: {exc} Return "
                        "exactly one JSON object with no prose and no markdown fences."
                    ]
                    continue
                raise SpecCompositionError(
                    f"Adherence review response was not parseable JSON: {exc}"
                ) from exc
            try:
                review = AdherenceReview.model_validate(payload)
            except ValidationError as exc:
                if attempt == 0:
                    repair_hints = _shape_findings(exc)
                    continue
                raise SpecCompositionError(
                    "Adherence review response shape was invalid: "
                    + "; ".join(_shape_findings(exc))
                ) from exc
            aligned = self._align_verdicts(intent.constraints, review)
            if aligned is None:
                if attempt == 0:
                    repair_hints = [
                        f"The issue states {len(intent.constraints)} constraint(s) but the "
                        f"response contained {len(review.constraints)} verdict(s). Return "
                        "exactly one verdict per stated constraint, in order."
                    ]
                    continue
                raise SpecCompositionError(
                    "Adherence review did not return one verdict per stated constraint "
                    f"({len(review.constraints)} verdict(s) for "
                    f"{len(intent.constraints)} constraint(s)); blocking rather than "
                    "treating unreviewed constraints as satisfied."
                )
            return aligned
        raise AssertionError("unreachable")  # pragma: no cover

    def _align_verdicts(
        self, constraints: list[str], review: AdherenceReview
    ) -> AdherenceReview | None:
        """Map returned verdicts onto the stated constraints, or None on mismatch.

        The prompt demands verbatim quoting in order, but a paraphrased echo must
        not brick a run: with matching counts the verdicts map positionally and
        the stated wording wins, so downstream findings always quote the issue.
        """
        if not constraints:
            return review.model_copy(update={"constraints": []})
        if len(review.constraints) != len(constraints):
            return None
        verdicts = [
            item
            if _normalized(item.constraint) == _normalized(stated)
            else item.model_copy(update={"constraint": stated})
            for stated, item in zip(constraints, review.constraints, strict=True)
        ]
        return review.model_copy(update={"constraints": verdicts})


def _default_adherence_model_id() -> str:
    return (os.getenv("BEDROCK_ESCALATION_MODEL_ID") or "").strip() or os.getenv(
        "BEDROCK_MODEL_ID", ""
    )


def default_adherence_reviewer(logger=None) -> AdherenceReviewer | None:
    """Reviewer used when none is injected; None disables the adherence pass.

    Needs a model (``BEDROCK_ESCALATION_MODEL_ID`` preferred — checking is
    cheaper than generating — else ``BEDROCK_MODEL_ID``) and can be turned off
    with ``IAC_SMITH_ADHERENCE_GATE=0``. No model means no adherence pass;
    every existing gate still applies.
    """
    if os.getenv("IAC_SMITH_ADHERENCE_GATE") == "0" or not _default_adherence_model_id():
        return None
    return AdherenceReviewer(logger=logger)


# Keys that name a container image directly; matched at any depth of the
# composed JSON. String-level and provider-agnostic — never keyed to a service.
_IMAGE_KEY_RE = re.compile(r"(?:^|_)image$")
# Container definitions frequently ride inside jsonencode'd string values
# (e.g. ``${jsonencode([{"image": "nginx"}])}``); the capture is the image
# reference itself, so the check applies to it directly.
_EMBEDDED_IMAGE_RE = re.compile(r'"image"\s*:\s*"([^"]+)"')


def _unpinned_image(reference: str) -> bool:
    candidate = reference.strip()
    if not candidate or "${" in candidate or any(char.isspace() for char in candidate):
        return False
    if "@" in candidate:
        return False
    tail = candidate.rsplit("/", 1)[-1]
    if ":" not in tail:
        return True
    return tail.rsplit(":", 1)[-1].lower() == "latest"


def _image_finding(reference: str, path: str) -> str:
    return (
        f"Container image `{reference}` at `{path}` is not pinned to a specific "
        "version (tag-less or `:latest`); use an explicit version tag or digest."
    )


def unpinned_image_findings(document) -> list[str]:
    """Advisory findings for tag-less or ``:latest`` container-image strings.

    Walks the composed JSON (provider resources or a module call alike) for
    keys named ``image``/``*_image`` and for image references embedded in
    JSON-encoded string values.
    """
    findings: list[str] = []

    def walk(value, path: str) -> None:
        if isinstance(value, dict):
            for key, entry in value.items():
                child = f"{path}.{key}" if path else str(key)
                if (
                    isinstance(entry, str)
                    and _IMAGE_KEY_RE.search(str(key))
                    and _unpinned_image(entry)
                ):
                    findings.append(_image_finding(entry, child))
                walk(entry, child)
        elif isinstance(value, list):
            for index, entry in enumerate(value):
                walk(entry, f"{path}[{index}]")
        elif isinstance(value, str):
            for match in _EMBEDDED_IMAGE_RE.finditer(value):
                if _unpinned_image(match.group(1)):
                    findings.append(_image_finding(match.group(1), path))

    walk(document, "")
    return list(dict.fromkeys(findings))
