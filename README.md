# IaC Smith

IaC Smith is an AWS-focused agentic IaC workflow that turns freeform GitHub issues into validated Terraform/Terragrunt pull requests.

## Current status

IaC Smith's issue-to-PR MVP path is implemented for narrow AWS Terraform/Terragrunt request families. It can fetch a labeled GitHub issue, use Bedrock to parse infrastructure intent, scan the target repository for existing patterns and representative Terraform snippets, dynamically generate bounded Terraform/Terragrunt files, run static guardrails, commit to a target-repo branch, and open a pull request.

Implemented pieces include the Python project structure, LangGraph routing, ruleset loading, mandatory Bedrock intent parsing, target repo pattern scanning, change planning, Bedrock-backed Terraform/Terragrunt generation, static review guardrails, PR summary generation, target repo branch/commit/PR creation, hardened GitHub Actions, setup documentation, and regression tests.

`BEDROCK_MODEL_ID` is supplied by GitHub Actions secret or environment configuration. IaC Smith does not hardcode AWS account IDs, account-specific Bedrock ARNs, or model IDs.

It uses AWS Bedrock and LangGraph to infer infrastructure intent, apply an opinionated ruleset, generate Terraform/Terragrunt projects from issue text plus repo-discovered conventions, validate the output, and open a reviewable PR against a target infrastructure repository.

The goal is not to blindly apply infrastructure. The goal is to turn natural-language infrastructure requests into clear, supportable, validated IaC changes that can be reviewed, merged, and applied through normal GitOps-style workflows.

## MVP boundary

IaC Smith v1 is intentionally narrow. It supports AWS infrastructure generation for a small set of safe request families and refuses unsupported or risky requests rather than hallucinating Terraform.

Supported MVP request families:

1. Baseline Terraform/Terragrunt repo with backend bootstrap
2. VPC foundation
3. EKS Fargate foundation
4. ECS Fargate foundation
5. Private RDS PostgreSQL with encrypted storage and AWS-managed master password

IaC Smith never applies infrastructure from the controller repo.
