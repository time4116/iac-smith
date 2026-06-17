# IaC Smith

IaC Smith turns freeform GitHub issues into validated Terraform/Terragrunt pull requests using AWS Bedrock and LangGraph.

When a GitHub issue is labeled `iac-smith`, a GitHub Actions workflow runs the LangGraph-based agent. The agent reads the issue, infers the AWS infrastructure intent, scans the target infrastructure repository for existing conventions, generates a complete Terraform/Terragrunt change, validates it with static checks and a runtime plan, and opens a reviewable PR.

The goal is not to blindly apply infrastructure. The goal is to turn natural-language infrastructure requests into clear, supportable, validated IaC that can be reviewed, merged, and applied through normal GitOps workflows.

## Prerequisites

- **Two GitHub repositories**: a controller repo (this one) and a target infrastructure repo (starts empty)
- **AWS account** with Bedrock enabled in your chosen region and model access granted
- **AWS IAM OIDC role** trusted by GitHub Actions — see [docs/SETUP.md](docs/SETUP.md)
- **Python 3.12+** and [uv](https://docs.astral.sh/uv/) (for local development only)

## Quickstart

1. Fork this repo and create an empty target infrastructure repo
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
| `BEDROCK_MODEL_ID` | Secret | Bedrock model ID or inference profile ARN (e.g. `anthropic.claude-haiku-4-5-20251001`) |
| `AWS_REGION` | Variable | AWS region (default: `us-west-2`) |

See [docs/SETUP.md](docs/SETUP.md) for full setup instructions including the IAM trust policy shape and fine-grained PAT scope.

## Supported request families

1. Baseline Terraform/Terragrunt repo with backend bootstrap
2. VPC foundation
3. EKS Fargate
4. ECS Fargate
5. Private RDS PostgreSQL

IaC Smith refuses unsupported or risky requests rather than hallucinating Terraform.

## Documentation

- [docs/SETUP.md](docs/SETUP.md) — full setup guide
- [AGENT_REFERENCE.md](AGENT_REFERENCE.md) — architecture and implementation reference

## License

Apache 2.0 — see [LICENSE](LICENSE).
