# IaC Smith

IaC Smith turns freeform GitHub issues into validated Terraform/Terragrunt pull requests using AWS Bedrock and LangGraph.

When a GitHub issue is labeled `iac-smith`, a GitHub Actions workflow runs the LangGraph-based agent. The agent reads the issue, infers the AWS infrastructure intent, scans the target infrastructure repository for existing conventions, generates a complete Terraform/Terragrunt change, validates it with static checks and runtime validation, and opens a reviewable PR.

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

IaC Smith is not limited to a fixed set of infrastructure types. The agent reads the target repo's existing conventions, module layout, and Terragrunt stacks before generating anything, so it produces changes that fit the repo rather than starting from scratch every time.

It handles greenfield repos (first issue creates the backend bootstrap and repo structure) and iterative additions to existing repos (new module, new stack, changes to existing resources) equally.

Before opening a PR, IaC Smith runs:

- **Static review**: pattern-based checks across all generated files (hardcoded state keys, missing required blocks, undeclared variables, and more)
- **Runtime validation**: `terraform validate` and `terragrunt plan` against real AWS state
- **Repair loop**: if checks fail, the agent retries generation with the error context up to three times before blocking

IaC Smith will refuse requests that are genuinely destructive or risky rather than hallucinating a broken implementation.

## Architecture and security model

IaC Smith is split into a controller repository and a target infrastructure repository. The controller repository runs the GitHub Actions workflow, reads the source issue, calls Bedrock, scans the target repository, validates generated Terraform/Terragrunt, and opens a pull request. The target infrastructure repository owns the generated IaC and its normal post-merge apply workflow.

The controller does not apply infrastructure. Its durable safety boundary is PR creation only: generated changes must pass static and runtime checks, then Human PR review remains the approval gate before anything is merged or applied.

The default public-demo workflow is intentionally narrow. The issue trigger is owner-gated, the target repository is fixed by an allowlist, AWS access is via GitHub Actions OIDC, and target-repo writes use a fine-grained PAT scoped to the target repository rather than broad account credentials.

## Security checks

IaC Smith runs deterministic checks around the model-generated output before it opens a pull request:

1. **Workflow privilege checks**: controller workflows use least-privilege permissions, pinned third-party actions, locked dependency installs, and an owner-gated workflow trigger before secrets or OIDC credentials are available.
2. **Target boundary checks**: the repository allowlist in `IAC_SMITH_ALLOWED_TARGET_REPO` fails closed so the agent cannot be redirected to an arbitrary repository.
3. **Generated file path checks**: generated paths are resolved under the target repository root before writing, blocking path traversal outside the checkout.
4. **Secret-pattern scan**: generated non-Markdown files are scanned for AWS access keys, private key headers, `aws_access_key_id`, `aws_secret_access_key`, and quoted password/token/secret assignments.
5. **Terraform safety checks**: static review blocks hardcoded Terragrunt state keys, duplicate variable/output/provider declarations, undeclared module and variable references, unsafe apply workflow triggers, and dangerous public ingress on sensitive ports.
6. **Terraform/Terragrunt validation**: before commit, IaC Smith runs Terraform/Terragrunt validation and plan checks where backend state and credentials allow; failures trigger a bounded repair loop and otherwise block PR creation.
7. **PR disclosure**: generated PR bodies include assumptions, warnings, validation results, backend resources, and an explicit no-apply confirmation.

## Documentation

- [docs/SETUP.md](docs/SETUP.md): full setup guide
- [AGENT_REFERENCE.md](AGENT_REFERENCE.md): architecture and implementation reference

## License

Apache 2.0 — see [LICENSE](LICENSE).
