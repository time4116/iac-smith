# IaC Smith Project Brief

## Purpose

IaC Smith turns natural-language AWS infrastructure requests into validated, reviewable Terraform/Terragrunt pull requests.

The portfolio value is not autonomous cloud engineering or push-button app hosting. The value is AI-assisted IaC PR generation with deterministic safety gates: a reviewer can describe infrastructure in a GitHub issue and receive a supportable PR whose rendered resources, assumptions, validation results, and scope boundaries are explicit.

IaC Smith never applies infrastructure. The controller creates PRs only; human review, merge, and the target repository's GitOps workflow remain the deployment boundary.

## Current workflow

1. A user creates an issue in `time4116/iac-smith` and applies the `iac-smith` label.
2. The owner-gated GitHub Actions controller workflow starts.
3. The controller validates that the requested target repository exactly matches `IAC_SMITH_ALLOWED_TARGET_REPO`.
4. The workflow assumes AWS credentials through GitHub Actions OIDC and calls Bedrock using the configured `BEDROCK_MODEL_ID`.
5. IaC Smith parses the issue into infrastructure intent, scans the target repo for Terraform/Terragrunt conventions, and builds a deterministic change plan.
6. The default typed-spec composer proposes schema-validated resource selections or community-module calls as JSON, not freeform HCL text.
7. Deterministic renderers write repository structure, backend bootstrap, Terragrunt envelopes, workflows, module variables, outputs, and dependency wiring.
8. Static review, provider-schema validation, runtime Terraform/Terragrunt validation, and optional local-state plan checks gate the result.
9. Bounded repair loops feed exact validation findings back into the composer or generator.
10. If the final legitimacy gate confirms real requested resources were rendered, IaC Smith commits the target branch and opens or updates a PR.
11. If legitimacy or validation cannot be proven, IaC Smith blocks instead of opening a misleading PR.

## Current scope

In scope:

- AWS-focused Terraform/Terragrunt generation
- GitHub issue to target-repo PR workflow
- Existing and greenfield target repositories
- Repository convention scanning before generation
- Typed resource specs and deterministic rendering by default
- Terraform Registry community-module candidates, especially `terraform-aws-modules`
- Backend bootstrap for S3 state and DynamoDB locking
- `non-prod` and `prod` environment model
- Generated target-repo PR check and post-merge apply workflows
- Static review for safety and structural drift
- Backend-free Terraform validation
- Optional local-state Terragrunt planning with mocked dependencies
- Bounded self-repair with exact validation findings
- PR bodies generated from the rendered resource inventory, including Scope Monitoring

Out of scope:

- Applying infrastructure from the controller repo
- Generating application source code, Dockerfiles, or app build pipelines
- Kubernetes workload manifests
- Database migrations
- DNS/TLS automation unless explicitly represented as infrastructure resources
- Cost estimation
- Multi-cloud support
- GitHub App authentication
- Auto-merge or auto-apply after PR creation

## Generation model

The default generation mode is the typed-spec compiler.

IaC Smith builds an `InfrastructureSpec` from the parsed issue intent and deterministic change plan. The model proposes typed components, provider resources, argument values, nested blocks, references, and community-module calls as structured JSON. Those proposals are validated against harvested provider and module schemas before HCL is rendered.

Freeform Bedrock Terraform generation remains available only as an explicit escape hatch with `IAC_SMITH_GENERATION_MODE=freeform`.

## Safety model

IaC Smith fails closed by default.

If the composer is unavailable, schema harvest fails, generated resources are missing, or final resource inventory does not match the requested infrastructure class, the run blocks. `IAC_SMITH_ALLOW_STRUCTURE_ONLY=1` is reserved for explicit offline/eval scenarios and produces a PR warning when used.

The final PR body is derived from rendered files, not from the original intent. This keeps summaries, resource counts, backend details, validation output, warnings, and Scope Monitoring aligned with what the branch actually contains.

## Target repository boundary

IaC Smith is designed for a fixed allowlisted target repository per controller workflow run.

The public demo target can change over time, so docs should avoid hardcoding one historical demo repo. Configure:

```text
IAC_SMITH_TARGET_REPO=<owner>/<target-infra-repo>
IAC_SMITH_ALLOWED_TARGET_REPO=<owner>/<target-infra-repo>
```

The values must match exactly. The target repo PAT should be fine-grained and scoped only to that repository with Contents and Pull requests write permissions.

## Terraform/Terragrunt layout

The generated layout uses:

```text
bootstrap/backend/<env>/
environments/<env>/root.hcl
environments/<env>/<stack>/terragrunt.hcl
modules/<stack>/*.tf
.github/workflows/terraform-pr-check.yml
.github/workflows/terraform-apply.yml
```

IaC Smith does not generate a new `foundation` networking module as a default behavior. If a target repository already contains a foundation stack and module, new workload stacks can be wired to it. If no foundation exists, orphan foundation dependencies are stripped so generated stacks do not depend on infrastructure that is not present.

## Portfolio positioning

Use this phrasing when describing the project:

> IaC Smith is an AI-assisted IaC PR generator for AWS Terraform/Terragrunt. It converts GitHub issues into reviewable infrastructure PRs, but correctness is enforced by deterministic schema validation, runtime checks, bounded repair, and fail-closed legitimacy gates rather than blind trust in model output.

Avoid claiming that it is a fully autonomous cloud engineer, that it can apply infrastructure, or that every generated PR is automatically production-ready without human review.

## Historical planning notes

The original long-form MVP planning brief is preserved at [docs/historical/PROJECT_BRIEF.md](historical/PROJECT_BRIEF.md). Treat it as historical context, not current behavior documentation.
