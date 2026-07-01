# IaC Smith

IaC Smith turns freeform GitHub issues into validated Terraform/Terragrunt pull requests using AWS Bedrock and LangGraph.

When a GitHub issue is labeled `iac-smith`, a GitHub Actions workflow runs the LangGraph-based agent. The agent reads the issue, infers the AWS infrastructure intent, scans the target infrastructure repository for existing conventions, generates a Terraform/Terragrunt change, validates it with static and runtime checks, and opens a reviewable PR.

The goal is not to blindly apply infrastructure. The goal is to turn natural-language infrastructure requests into clear, supportable, validated IaC that can be reviewed, merged, and applied through normal GitOps workflows.

## Prerequisites

- **Two GitHub repositories**: a controller repo (this one) and a target infrastructure repo (new or existing Terraform)
- **AWS account** with Bedrock enabled in your chosen region and model access granted
- **AWS IAM OIDC role** trusted by GitHub Actions — see [docs/SETUP.md](docs/SETUP.md)
- **Python 3.12+** and [uv](https://docs.astral.sh/uv/) (for local development only)

## Quickstart

1. Fork this repo and point it at a target infrastructure repo (empty or existing)
2. Configure the required GitHub Actions secrets and variables (see table below)
3. Create a GitHub issue describing the AWS infrastructure you want
4. Apply the `iac-smith` label to the issue
5. Watch the workflow run and open a PR in the target repo

The more detail you provide in the issue, the better the generated Terraform PR will be.

## Required secrets and variables

Configure these in the controller repo under **Settings → Secrets and variables → Actions**:

| Name | Type | Description |
|---|---|---|
| `AWS_ROLE_ARN_NON_PROD` | Secret | IAM role ARN trusted by GitHub Actions OIDC for Bedrock access and non-prod validation |
| `AWS_ROLE_ARN_PROD` | Secret | IAM role ARN for generated prod validation workflows |
| `IAC_SMITH_TARGET_REPO_PAT` | Secret | Fine-grained PAT scoped only to the target repo with Contents and Pull requests write |
| `BEDROCK_MODEL_ID` | Secret | Bedrock model ID or inference profile ARN (e.g. `anthropic.claude-haiku-4-5-20251001`; verify in the Bedrock console before use) |
| `AWS_REGION` | Variable | AWS region (default: `us-west-2`) |

See [docs/SETUP.md](docs/SETUP.md) for full setup instructions including the IAM trust policy shape and fine-grained PAT scope.

## What IaC Smith can handle

IaC Smith is designed to support more than a fixed catalog of infrastructure types. The agent reads the target repo's existing conventions, module layout, and Terragrunt stacks before generating anything, so it produces changes that fit the repo rather than starting from scratch every time.

It supports both greenfield repos, where the first issue creates the backend bootstrap and repo structure, and iterative additions to existing repos, such as new modules, new stacks, or changes to existing resources.

**IaC Smith generates infrastructure, not application code.** It produces Terraform/Terragrunt, backend bootstrap, CI/apply workflows, and docs — it does not write application source (e.g. a `Program.cs`, a Dockerfile, or a build pipeline), and it is not designed to. A request like "deploy my .NET web app" yields the *infrastructure* to host it (for Elastic Beanstalk, the application and environment; for ECS, the cluster/service/task definition), but the application artifact itself — and the build/deploy step that ships it — is expected to come from a separate application pipeline. Treat the generated infrastructure as the landing zone your app deploys *into*, not as the app.

Before opening a PR, IaC Smith runs:

- **Static review (security tier)**: blocking checks for issues real Terraform may not catch — secret patterns, unsafe file paths, hardcoded Terragrunt state keys, unsafe workflow triggers, and workflow privilege problems
- **Static review (structural tier)**: advisory checks for generated IaC consistency — duplicate declarations, undeclared references, missing required inputs, and dependency-output mismatches. These are surfaced for review and fed into the bounded repair loop, but do not block — the real validator is the gate
- **Runtime validation (correctness gate)**: Terraform formatting, Terragrunt HCL formatting, and backend-free module-level `terraform init` / `terraform validate` — the authoritative check that generated IaC is valid. When optional runtime planning is enabled, IaC Smith also runs plan-only Terragrunt checks against a local-state copy with mocked dependencies (never `apply`), so apply-path errors are caught before a PR is opened
- **Bounded self-repair**: when validation surfaces issues, IaC Smith feeds the exact errors back into the generator, rewrites the affected files, and retries before blocking

Runtime validation is intentionally conservative. IaC Smith never runs `terraform apply`. For new infrastructure where remote state or dependency outputs may not exist yet, validation focuses on formatting, backend-free initialization, and module-level Terraform validation rather than pretending every generated stack can be fully planned.

IaC Smith will refuse requests that are genuinely destructive or risky rather than generating an unsafe or misleading implementation.

## Generation modes and local evals

The default generation mode remains the existing Bedrock free-form Terraform generator. To exercise the typed-spec compiler path, set `IAC_SMITH_GENERATION_MODE=spec_renderer`. In this mode IaC Smith builds an `InfrastructureSpec` from parsed intent and the deterministic change plan, then renders repository structure, Terragrunt envelopes, backend bootstrap, workflows, module variables, outputs, and cross-stack dependency wiring from that spec.

The spec renderer intentionally emits deterministic structure only until provider-schema or Terraform Registry module selection is added. That keeps the generic/no-golden-path boundary while moving global consistency out of per-file LLM text generation.

Use the local eval harness before changing generation behavior:

```bash
uv run python -m iac_smith.eval path/to/fixture.yaml --runs 10
```

The report tracks intent variants, plan variants, rendered-file hash variants, static-review pass count, and failure clusters so repeated issue runs can be measured without dispatching the full GitHub Actions workflow.

## Architecture and security model

IaC Smith is split into a controller repository and a target infrastructure repository. The controller repository runs the GitHub Actions workflow, reads the source issue, calls Bedrock, scans the target repository, validates generated Terraform/Terragrunt, and opens a pull request. The target infrastructure repository owns the generated IaC and its normal post-merge apply workflow.

The controller does not apply infrastructure. Its durable safety boundary is PR creation only: generated changes must pass static and runtime checks, then human PR review remains the approval gate before anything is merged or applied.

IaC Smith does not bypass GitOps. It turns an infrastructure request into a bounded, validated pull request with assumptions, warnings, validation results, and an explicit no-apply confirmation in the PR body.

The default public-demo workflow is intentionally narrow. The issue trigger is owner-gated, the target repository is fixed by an allowlist, AWS access is via GitHub Actions OIDC, and target-repo writes use a fine-grained PAT scoped to the target repository rather than broad account credentials.

## Self-healing, not self-applying

IaC Smith is designed to repair generated IaC before it reaches reviewers, not to apply infrastructure automatically.

The controller uses bounded repair loops at multiple stages:

1. **Generation repair**: security/safety violations and structural issues found by static review are regenerated with the exact errors. A module and its Terragrunt stack are repaired together so their variable contract converges rather than the two files repeatedly undoing each other's changes; if repairs stop making progress, a guard returns best-effort output instead of crashing.
2. **Graph-level repair**: the LangGraph controller can route security-blocking validation back through generation with accumulated context.
3. **Runtime repair**: formatting, backend-free Terraform/Terragrunt validation, and (when `IAC_SMITH_RUNTIME_PLAN` is set) real `terragrunt plan` errors against local state — the authoritative correctness gate — are sent back to the generator for targeted file repair. If the plan cannot be made to pass within the repair budget, IaC Smith blocks rather than opening a PR.

Each repair loop has a retry limit. If a security/safety check or the real Terraform/Terragrunt validation cannot be satisfied, IaC Smith blocks instead of opening a misleading PR. Advisory structural findings that remain are surfaced in the PR body for human review rather than blocking.

## Security checks

IaC Smith runs deterministic checks around the model-generated output before it opens a pull request:

1. **Workflow privilege checks**: controller workflows use least-privilege permissions, pinned third-party actions, locked dependency installs, and an owner-gated workflow trigger before secrets or OIDC credentials are available.
2. **Target boundary checks**: the repository allowlist in `IAC_SMITH_ALLOWED_TARGET_REPO` fails closed so the agent cannot be redirected to an arbitrary repository.
3. **Generated file path checks**: generated paths are resolved under the target repository root before writing, blocking path traversal outside the checkout.
4. **Secret-pattern scan**: generated non-Markdown files are scanned for AWS access keys, private key headers, `aws_access_key_id`, `aws_secret_access_key`, and quoted password/token/secret assignments.
5. **Terraform safety checks**: static review *blocks* on security/safety issues that real Terraform cannot catch — hardcoded Terragrunt state keys, unsafe apply workflow triggers, and workflow privilege problems. Structural findings (duplicate declarations, undeclared references, missing required inputs) are surfaced for review and fed into the repair loop, not blocked. It also flags dangerous public ingress on sensitive ports for reviewer attention.
6. **Terraform/Terragrunt validation**: before committing changes, IaC Smith runs Terraform formatting, Terragrunt HCL formatting, and backend-free module-level Terraform validation. With optional runtime planning enabled, it also runs plan-only Terragrunt checks against local state with mocked dependencies. This is the authoritative correctness gate: failures trigger a bounded repair loop and otherwise block PR creation.
7. **PR disclosure**: generated PR bodies include assumptions, warnings, validation results, backend resources, and an explicit no-apply confirmation.

## Why this exists

Infrastructure teams receive repeatable requests through tickets, Slack, and GitHub issues: create a service, add a database, wire a queue, expose a private endpoint, update an environment, or scaffold a new stack.

IaC Smith turns those requests into reviewable pull requests while preserving platform rules, repository conventions, validation, and human approval.

## Documentation

- [docs/SETUP.md](docs/SETUP.md): full setup guide
- [docs/LAYOUT.md](docs/LAYOUT.md): Terraform/Terragrunt directory layout for greenfield projects
- [AGENT_REFERENCE.md](AGENT_REFERENCE.md): architecture and implementation reference
- [docs/ARCHITECTURE_FLOW.md](docs/ARCHITECTURE_FLOW.md): Mermaid architecture flow

## License

Apache 2.0 — see [LICENSE](LICENSE).
